"""Approval workflow and period locking (Requirement 15).

With location verification out of scope, manager approval is the principal control over attendance
accuracy — so this module carries more weight than its size suggests. It owns three things, and holds
the single authority for the status ladder that everything downstream (payroll, billing) reads from:

* **The status ladder (Requirement 15.1, 15.2).** Every entry is Draft, Review, Approved or Locked,
  and status advances only one rung up the ladder at a time. Any other move is refused, except an
  *administrator's* single-rung reversal, which is the one exception the requirement carves out and
  which must carry a reason recorded in the audit log (Requirement 15.6). `validate_transition` is
  the one place that decides what is allowed; the bulk endpoint and the lock both go through it.

* **Bulk status change (Requirement 15.3).** A manager moves a set of their sites' entries from Draft
  to Review and from Review to Approved. The move is scoped to the caller's sites in the query, not
  after the fetch, the same shape the hours view uses; an entry at a site outside the caller's scope
  is simply not in the set the update touches.

* **Locking a month (Requirement 15.4, 15.5, 15.7).** An administrator locks a calendar month, which
  sets every Approved entry in it to Locked. If the month still holds entries that are not Approved,
  the lock warns and lists them first (Requirement 15.7); the administrator may then force past them.
  Unlocking reopens the month with a mandatory reason (Requirement 15.6). The period-lock guard that
  refuses writes into a locked month lives in `app.services.scan` (`is_period_locked`), which this
  module writes the rows for; the admin override that lets an administrator write into a locked month
  is `app.services.scan.assert_period_open` — see there.

Like every other service, nothing here commits: the router owns the unit of work, so a status change
or a lock and its audit rows land together or not at all (Requirement 13.2). Role and scope are the
router's concern — the ladder's reversal exception is checked here (only an administrator may reverse)
because it is a rule about the transition itself, while *which* sites a manager may touch is applied
as a query scope the router hands in.
"""

from __future__ import annotations

import calendar
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.period_lock import PeriodLock
from app.models.time_entry import TimeEntry, TimeEntryStatus
from app.services.audit import AuditContext, record_change, record_model_changes, snapshot

# --------------------------------------------------------------------------- the ladder

#: The status ladder, in order (Requirement 15.1, 15.2). Status advances one rung at a time along
#: this sequence; a reversal moves one rung back and is an administrator-only exception.
_LADDER: tuple[TimeEntryStatus, ...] = (
    TimeEntryStatus.DRAFT,
    TimeEntryStatus.REVIEW,
    TimeEntryStatus.APPROVED,
    TimeEntryStatus.LOCKED,
)

_RANK: dict[TimeEntryStatus, int] = {status: index for index, status in enumerate(_LADDER)}


# --------------------------------------------------------------------------- errors
# Domain errors, not HTTP errors; each carries a machine `code` the router lifts into the error
# envelope and the front end translates, the same convention the scan and manual-entry services use.


class PeriodError(Exception):
    """Base for a refused approval or locking action."""

    code = "period_error"


class InvalidTransition(PeriodError):
    """A status move that is not a single rung up the ladder, and not an allowed reversal (Req 15.2).

    Carries the from/to statuses so the router can name them in the envelope. A forward jump of more
    than one rung (Draft straight to Approved), a move to the same status, or a step off the ends is
    always refused; a single step back is refused for everyone but an administrator.
    """

    code = "invalid_transition"

    def __init__(self, *, from_status: TimeEntryStatus, to_status: TimeEntryStatus) -> None:
        super().__init__(f"cannot move {from_status} to {to_status}")
        self.from_status = from_status
        self.to_status = to_status


class ReversalRequiresAdmin(PeriodError):
    """A step back down the ladder was attempted by a non-administrator (Requirement 15.2).

    The ladder runs one way for everyone but an administrator; only an administrator may reverse a
    status, and only then with a stated reason. A manager trying to undo an approval is told the move
    is not theirs to make rather than that the move is impossible.
    """

    code = "reversal_requires_admin"


class ReversalRequiresReason(PeriodError):
    """An administrator's reversal carried no reason (Requirement 15.6).

    A reversal is the one status move the requirement wants explained in the audit log, so a reversal
    without a reason is refused before it is written.
    """

    code = "reversal_requires_reason"


class PeriodNotLocked(PeriodError):
    """An unlock was attempted on a month that is not currently locked (Requirement 15.6).

    Unlocking an open month is a no-op that would still write an audit row implying a lock was lifted,
    so it is refused: there is nothing to reopen.
    """

    code = "period_not_locked"


class PeriodAlreadyLocked(PeriodError):
    """A lock was attempted on a month that is already locked (Requirement 15.4).

    Locking a locked month again would re-freeze nothing and muddy the audit trail with a second lock
    that lifted nothing; the caller is told it is already locked.
    """

    code = "period_already_locked"


# --------------------------------------------------------------------------- transition validation


def is_reversal(from_status: TimeEntryStatus, to_status: TimeEntryStatus) -> bool:
    """Whether `to_status` is exactly one rung *below* `from_status` on the ladder."""
    return _RANK[to_status] == _RANK[from_status] - 1


def is_forward_step(from_status: TimeEntryStatus, to_status: TimeEntryStatus) -> bool:
    """Whether `to_status` is exactly one rung *above* `from_status` on the ladder."""
    return _RANK[to_status] == _RANK[from_status] + 1


def validate_transition(
    from_status: TimeEntryStatus,
    to_status: TimeEntryStatus,
    *,
    is_admin: bool,
    reason: str | None,
) -> None:
    """Refuse a status move that the ladder does not allow (Requirement 15.2, 15.6).

    The single authority for what a status change may do. A move is permitted only if it is one rung
    up the ladder (anyone with the right role and scope) or one rung down (an administrator, with a
    reason). Everything else — a jump of more than one rung, a move to the same status, a step off the
    ends — raises `InvalidTransition`. A reversal by a non-administrator raises `ReversalRequiresAdmin`;
    a reversal by an administrator with no reason raises `ReversalRequiresReason`. Roles and site
    scope are the router's concern; this decides only whether the *transition itself* is legal.
    """
    if is_forward_step(from_status, to_status):
        return
    if is_reversal(from_status, to_status):
        if not is_admin:
            raise ReversalRequiresAdmin(f"only an administrator may reverse {from_status} to {to_status}")
        if reason is None:
            raise ReversalRequiresReason("an administrator's reversal requires a reason")
        return
    raise InvalidTransition(from_status=from_status, to_status=to_status)


def apply_status(
    session: Session,
    entry: TimeEntry,
    to_status: TimeEntryStatus,
    *,
    is_admin: bool,
    reason: str | None,
    context: AuditContext,
) -> bool:
    """Move one entry to `to_status` if the transition is legal, auditing the change (Requirement 15.2).

    Returns True when the entry moved, False when it was already at `to_status` and there is nothing
    to do (which a bulk change treats as a no-op rather than an error, so re-approving an
    already-approved set is idempotent). An illegal move raises. The change is audited under the
    reason — mandatory for a reversal — in the caller's transaction (Requirement 13.2).
    """
    if entry.status == to_status:
        return False
    validate_transition(entry.status, to_status, is_admin=is_admin, reason=reason)
    before = snapshot(entry)
    entry.status = to_status
    session.flush()
    record_model_changes(session, entry, before, context=context, reason=reason)
    return True


# --------------------------------------------------------------------------- bulk status change


@dataclass(frozen=True, slots=True)
class BulkStatusOutcome:
    """What a bulk status change moved: the ids that changed and the status they moved to."""

    updated_ids: list[uuid.UUID]
    target_status: TimeEntryStatus


def bulk_change_status(
    session: Session,
    *,
    entry_ids: Sequence[uuid.UUID],
    target_status: TimeEntryStatus,
    scope_statement: Select[tuple[TimeEntry]],
    is_admin: bool,
    reason: str | None,
    context: AuditContext,
) -> BulkStatusOutcome:
    """Move a set of entries to `target_status`, scoped to the caller's sites (Requirement 15.3).

    `scope_statement` is a `SELECT TimeEntry` the router has already narrowed to the caller's sites
    (Requirement 2.3), so an entry at a site outside the caller's scope is never in the set this
    touches — the scope is a `WHERE` clause, not a post-fetch filter. Only the ids in `entry_ids`
    among the scoped, live entries are loaded; a requested id that is out of scope, deleted, or does
    not exist is silently absent from the update rather than an error, so a manager selecting a page
    that happens to include one foreign row is not blocked by it.

    Every entry that is not already at the target is moved through `apply_status`, which validates the
    transition per entry: if any entry's current status cannot legally reach the target the whole call
    raises and the router rolls back, so a bulk change is all-or-nothing rather than leaving half a
    page advanced. Entries already at the target are skipped, making a repeat idempotent.
    """
    if not entry_ids:
        return BulkStatusOutcome(updated_ids=[], target_status=target_status)

    statement = scope_statement.where(
        TimeEntry.id.in_(entry_ids), TimeEntry.deleted_at.is_(None)
    )
    entries = list(session.scalars(statement))

    updated: list[uuid.UUID] = []
    for entry in entries:
        if apply_status(
            session, entry, target_status, is_admin=is_admin, reason=reason, context=context
        ):
            updated.append(entry.id)
    return BulkStatusOutcome(updated_ids=updated, target_status=target_status)


# --------------------------------------------------------------------------- month boundaries


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """The first and last calendar dates of a month, inclusive.

    Entries are grouped by `work_date`, the local date of check-in, so a month's entries are those
    whose work date falls between these two dates. `calendar.monthrange` gives the last day, which
    handles February and the 30/31-day months without a lookup table.
    """
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def entries_in_month(year: int, month: int) -> Select[tuple[TimeEntry]]:
    """A `SELECT` of the live time entries whose work date falls in the given month."""
    first, last = month_bounds(year, month)
    return select(TimeEntry).where(
        TimeEntry.work_date >= first,
        TimeEntry.work_date <= last,
        TimeEntry.deleted_at.is_(None),
    )


# --------------------------------------------------------------------------- lock state


def get_period(session: Session, year: int, month: int) -> PeriodLock | None:
    """The `period_locks` row for a month, or `None` if the month was never touched."""
    return session.scalars(
        select(PeriodLock).where(PeriodLock.year == year, PeriodLock.month == month)
    ).one_or_none()


def list_periods(session: Session) -> list[PeriodLock]:
    """Every month the workflow has touched, most recent first (Requirement 15.4)."""
    return list(
        session.scalars(
            select(PeriodLock).order_by(PeriodLock.year.desc(), PeriodLock.month.desc())
        )
    )


def unapproved_entries(session: Session, year: int, month: int) -> list[TimeEntry]:
    """The month's live entries that are not Approved and not already Locked (Requirement 15.7).

    These are what stand in the way of a clean lock: a lock freezes Approved entries, so anything
    still Draft or Review is left behind and must be surfaced as a warning before the administrator
    decides to lock past it. Ordered by employee then work date so the warning list reads the way the
    approval view groups.
    """
    statement = (
        entries_in_month(year, month)
        .where(TimeEntry.status.in_((TimeEntryStatus.DRAFT, TimeEntryStatus.REVIEW)))
        .order_by(TimeEntry.employee_id, TimeEntry.work_date, TimeEntry.check_in_at)
    )
    return list(session.scalars(statement))


# --------------------------------------------------------------------------- lock


@dataclass(frozen=True, slots=True)
class LockOutcome:
    """The result of a lock attempt.

    `locked` is whether the month was frozen. When it was, `locked_count` is how many Approved entries
    moved to Locked and `unapproved` lists any entries left behind because the lock was forced past
    them. When it was not — the month held unapproved entries and the call did not force — `locked` is
    false and `unapproved` is the warning list (Requirement 15.7). `period` is the row after the
    attempt, or `None` when nothing was written.
    """

    locked: bool
    locked_count: int
    unapproved: list[TimeEntry]
    period: PeriodLock | None


def lock_period(
    session: Session,
    *,
    year: int,
    month: int,
    force: bool,
    context: AuditContext,
) -> LockOutcome:
    """Lock a calendar month, freezing its Approved entries (Requirement 15.4, 15.7).

    The steps, in the order that leaves no partial state on a refusal:

    1. Refuse if the month is already locked — a second lock freezes nothing and muddies the trail.
    2. Find the entries that are not yet Approved. If any exist and the caller did not force, return a
       *warning* without writing anything: `locked=False` and the list of offending entries, so the
       administrator can approve them or choose to lock past them (Requirement 15.7).
    3. Move every Approved entry in the month to Locked, one legal forward step up the ladder, each
       audited. Unapproved entries are left as they are when the lock is forced.
    4. Write (or update) the `period_locks` row: `locked_at` to now and `locked_by_user_id` to the
       actor, clearing any prior unlock so the month reads as freshly locked. Audit the lock action.

    Nothing is committed here; the router commits so the status changes, the lock row and their audit
    rows share one transaction (Requirement 13.2).
    """
    period = get_period(session, year, month)
    if period is not None and period.is_locked:
        raise PeriodAlreadyLocked(f"{year}-{month:02d} is already locked")

    pending = unapproved_entries(session, year, month)
    if pending and not force:
        return LockOutcome(locked=False, locked_count=0, unapproved=pending, period=period)

    approved = list(
        session.scalars(
            entries_in_month(year, month).where(TimeEntry.status == TimeEntryStatus.APPROVED)
        )
    )
    for entry in approved:
        # Approved → Locked is a single forward step, legal for anyone; here it is the system acting
        # on the administrator's lock, so it needs no reversal reason.
        apply_status(
            session, entry, TimeEntryStatus.LOCKED, is_admin=True, reason=None, context=context
        )

    period = _write_lock(session, period, year=year, month=month, context=context)
    return LockOutcome(
        locked=True, locked_count=len(approved), unapproved=pending if force else [], period=period
    )


def _write_lock(
    session: Session,
    period: PeriodLock | None,
    *,
    year: int,
    month: int,
    context: AuditContext,
) -> PeriodLock:
    """Create or refresh the `period_locks` row so the month reads as locked, auditing the action.

    A month locked for the first time gets a new row; a month locked again after an unlock reuses its
    row, with `locked_at` reset to now and the prior `unlocked_at`/`unlocked_by`/reason cleared, so
    `PeriodLock.is_locked` (locked and not since unlocked) is true again. The lock is audited against
    the `period_locks` entity so the audit view can show when a month was frozen and by whom
    (Requirement 13.5).
    """
    now = datetime.now(UTC)
    if period is None:
        period = PeriodLock(year=year, month=month)
        session.add(period)
        session.flush()
        before = dict.fromkeys(snapshot(period), None)
    else:
        before = snapshot(period)

    period.locked_at = now
    period.locked_by_user_id = context.actor_user_id
    period.unlocked_at = None
    period.unlocked_by_user_id = None
    period.unlock_reason = None
    session.flush()
    record_model_changes(session, period, before, context=context, reason="period_locked")
    return period


# --------------------------------------------------------------------------- unlock


def unlock_period(
    session: Session,
    *,
    year: int,
    month: int,
    reason: str,
    context: AuditContext,
) -> PeriodLock:
    """Reopen a locked month, recording the reason in the audit log (Requirement 15.6).

    Refused if the month is not currently locked — there is nothing to reopen. When it is, `unlocked_at`
    and `unlocked_by_user_id` are set and the reason is stored on the row, so `PeriodLock.is_locked`
    becomes false and writes into the month are allowed again. Locked *entries* are left Locked: an
    unlock reopens the month to writes, it does not walk every entry back down the ladder — an
    administrator who needs a specific entry changed reverses it explicitly, with its own reason. The
    unlock is audited against the `period_locks` entity under the reason (Requirement 13.5).
    """
    period = get_period(session, year, month)
    if period is None or not period.is_locked:
        raise PeriodNotLocked(f"{year}-{month:02d} is not locked")

    before = snapshot(period)
    period.unlocked_at = datetime.now(UTC)
    period.unlocked_by_user_id = context.actor_user_id
    period.unlock_reason = reason
    session.flush()
    record_model_changes(session, period, before, context=context, reason=reason)
    return period


# --------------------------------------------------------------------------- admin override audit


def record_override(
    session: Session,
    *,
    year: int,
    month: int,
    action: str,
    reason: str,
    context: AuditContext,
) -> None:
    """Record an administrator's write into a locked month (Requirement 15.6).

    A locked month refuses writes for every role but an administrator performing an explicit override;
    when one does, the override itself must be recorded with a reason (Requirement 15.6). This writes
    that record against the `period_locks` entity, so the month's audit history shows every time it
    was written into while locked, what was done, and why. `action` names the write — a manual create,
    edit or delete — and `reason` is the override reason the administrator supplied.
    """
    period = get_period(session, year, month)
    entity_id = period.id if period is not None else uuid.uuid4()
    record_change(
        session,
        entity_type="period_locks",
        entity_id=entity_id,
        field="override",
        old_value=None,
        new_value=f"locked_period_override: {action}",
        context=context,
        reason=reason,
    )
