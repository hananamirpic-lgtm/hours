"""Notifications, and the deduplication that makes every generator idempotent.

Requirement 14.6 (and 4.6 for documents) asks that a given condition produce at most one
unacknowledged notification, however many times the job that detects it runs. The mechanism is the
unique `dedupe_key` column: a generator computes a key that identifies the condition *and its window*
— a document, its expiry date, and which of "warning" or "escalation" this is — and asks this service
to create the notification under that key. Running the job a second time in the same window computes
the same key and the create is a no-op.

The service does not check-then-insert. "Is there already one? no — insert" is two steps, and two
runs can both pass the check before either inserts, so both insert. Instead it inserts and lets the
unique constraint decide: a fresh key succeeds, a repeat raises the integrity error the database
guarantees, and the service reads that as "already sent" and returns the existing row. This is the
one correct shape, and it is correct on both PostgreSQL and the SQLite the tests run on because both
enforce the unique constraint.

Nothing here commits. Like every other service, it adds to the caller's session and lets the caller's
unit of work decide the outcome — the daily sweep commits once at the end, so a failure part-way
leaves neither the document changes nor the notifications behind.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.notification import Notification, NotificationSeverity


class NotificationError(Exception):
    """Base for notification-service failures. `code` is what the router lifts into the envelope."""

    code = "notification_error"


class NotificationNotFound(NotificationError):
    """No notification with that id belongs to the caller.

    A notification that exists but belongs to somebody else reads as not-found rather than forbidden,
    on purpose: a caller may not learn that another user has a notification with a given id, so the
    two cases collapse to one answer (Requirement 14.7 scopes every read and the read action to the
    recipient).
    """

    code = "notification_not_found"

    def __init__(self, notification_id: uuid.UUID) -> None:
        super().__init__(f"no notification {notification_id} for this recipient")
        self.notification_id = notification_id


def create_deduplicated(
    session: Session,
    *,
    recipient_user_id: uuid.UUID,
    type: str,
    dedupe_key: str,
    title_key: str,
    severity: NotificationSeverity = NotificationSeverity.INFO,
    body_params: dict | None = None,
    related_entity_type: str | None = None,
    related_entity_id: uuid.UUID | None = None,
) -> tuple[Notification, bool]:
    """Create a notification under `dedupe_key`, or return the existing one if the key is taken.

    Returns `(notification, created)`. `created` is True when a new row was written, False when a
    notification for this key already existed — which is exactly the signal a job needs to count how
    much genuinely new work it raised on a run.

    The insert is attempted inside a nested transaction (a SAVEPOINT), so a unique-constraint
    collision rolls back only the failed insert and not the caller's whole unit of work. Without the
    savepoint, the integrity error would poison the outer transaction and take the rest of the
    sweep's writes down with it.
    """
    existing = _find_by_dedupe_key(session, dedupe_key)
    if existing is not None:
        return existing, False

    notification = Notification(
        recipient_user_id=recipient_user_id,
        type=type,
        severity=severity,
        title_key=title_key,
        body_params=body_params or {},
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
        dedupe_key=dedupe_key,
        # The in-app notification is delivered the moment it is written: it is the record of truth
        # (Requirement 14.8), and marking the channel here lets the email sender find the ones that
        # still need pushing out on the second channel without re-sending the in-app one.
        delivered_channels=["in_app"],
    )
    session.add(notification)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        # Another writer inserted the same key between the check and the flush. The database held the
        # line; read back what is now there and report it as not-created.
        session.expunge(notification)
        found = _find_by_dedupe_key(session, dedupe_key)
        if found is None:  # pragma: no cover - the constraint guarantees a row exists
            raise
        return found, False
    return notification, True


def _find_by_dedupe_key(session: Session, dedupe_key: str) -> Notification | None:
    return session.scalars(
        select(Notification).where(Notification.dedupe_key == dedupe_key)
    ).one_or_none()


# --------------------------------------------------------------------------- in-app reads (14.7)


@dataclass(frozen=True, slots=True)
class NotificationPage:
    """One page of a recipient's notifications, plus the counts a list view needs.

    `total` is how many the recipient has in all, `unread` how many are unread — both across the whole
    list, not the page — so a badge and a "showing N of M" line are correct without a second query.
    """

    rows: Sequence[Notification]
    total: int
    unread: int


def list_for_recipient(
    session: Session,
    *,
    recipient_user_id: uuid.UUID,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> NotificationPage:
    """The notifications belonging to one recipient, newest first (Requirement 14.7).

    Scoped to the recipient in the `WHERE` clause, not filtered after the fetch: a caller only ever
    sees their own notifications, and enforcing that in the query means a paging offset can never walk
    into another user's rows. `unread_only` narrows to the unread ones for a "what needs my attention"
    view; the counts are always computed over the recipient's whole list so a badge stays correct when
    the caller is looking at a filtered page.
    """
    base = select(Notification).where(Notification.recipient_user_id == recipient_user_id)

    total = session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.recipient_user_id == recipient_user_id)
    ) or 0
    unread = session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.recipient_user_id == recipient_user_id)
        .where(Notification.is_read.is_(False))
    ) or 0

    listing = base
    if unread_only:
        listing = listing.where(Notification.is_read.is_(False))
    listing = listing.order_by(Notification.created_at.desc(), Notification.id).limit(limit).offset(offset)

    rows = list(session.scalars(listing))
    return NotificationPage(rows=rows, total=int(total), unread=int(unread))


def mark_read(
    session: Session, *, notification_id: uuid.UUID, recipient_user_id: uuid.UUID
) -> Notification:
    """Mark one of the recipient's own notifications read, and return it (Requirement 14.7).

    The recipient is part of the lookup, not a check applied after: a notification that is not this
    recipient's does not resolve at all and raises `NotificationNotFound`, so one user can never mark
    another's notification read. Idempotent — marking an already-read notification read again is a
    no-op that returns the same row, so a double click or a retry does nothing surprising and never
    moves `read_at`.
    """
    notification = session.scalars(
        select(Notification)
        .where(Notification.id == notification_id)
        .where(Notification.recipient_user_id == recipient_user_id)
    ).one_or_none()
    if notification is None:
        raise NotificationNotFound(notification_id)

    if not notification.is_read:
        notification.is_read = True
        notification.read_at = datetime.now(UTC)
        session.flush()
    return notification
