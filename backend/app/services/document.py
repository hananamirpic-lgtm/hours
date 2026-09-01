"""Employee documents and the daily expiry sweep (Requirement 4).

Two responsibilities live here, and they share the model but not much else.

**The document lifecycle.** Upload is two steps so the file never crosses the API (Requirement 4.3):
`begin_upload` issues a constrained presigned URL, the client uploads straight to private storage,
and `complete_upload` verifies the stored bytes server-side (Requirement 4.2) before recording the
row. Reads exclude soft-deleted rows; delete is soft, keeping the row for the audit trail while the
bytes are removed from storage. Download hands back a short-lived signed URL, and authorization is
the router's to check *before* it calls — a signed URL is a capability, and minting one is the grant.

**The expiry sweep.** `run_document_expiry_sweep` is the daily job of Requirements 4.4–4.6. For each
non-deleted document with an expiry date it raises, through the notification service:

* a *warning* to administrators when the expiry falls inside the configurable window (default 30
  days) and has not yet passed;
* an *escalation* when the expiry has passed.

Idempotency is not this function's concern to solve — it delegates to the notification service's
`dedupe_key`, computed from the document, its expiry date and which window it is. Running the sweep
twice on the same day computes the same keys the second time, so the second run raises nothing new.
That is the property the test pins: two runs, one set of notifications.

Nothing here commits. The service adds to the caller's session; the router commits a single document
change, and the sweep's caller commits once at the end so a failure part-way leaves nothing behind.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core import storage as storage_module
from app.core.storage import ObjectStorage
from app.models.document import Document
from app.models.notification import NotificationSeverity
from app.models.user import User, UserRole
from app.schemas.document import DocumentUploadComplete, DocumentUploadInit
from app.services import notification as notification_service
from app.services import settings as settings_service
from app.services.audit import AuditContext, record_change, record_model_changes, snapshot

# --------------------------------------------------------------------------- constants

#: Settings key for the warning window, seeded to 30 (Requirement 4.4).
WARNING_WINDOW_SETTING = "document_expiry_warning_days"
DEFAULT_WARNING_WINDOW_DAYS = 30

#: Notification types the sweep raises. The window word in the dedupe key comes from these, so the
#: warning and the escalation for one document never collide and never suppress each other.
NOTIFICATION_TYPE_WARNING = "document_expiry_warning"
NOTIFICATION_TYPE_ESCALATION = "document_expiry_escalation"

#: Translation keys the front end and the email renderer resolve into the recipient's language.
TITLE_KEY_WARNING = "notifications.document_expiry_warning"
TITLE_KEY_ESCALATION = "notifications.document_expiry_escalation"


# --------------------------------------------------------------------------- errors


class DocumentError(Exception):
    """Base for document-service failures. `code` is what the router lifts into the error envelope."""

    code = "document_error"


class DocumentNotFound(DocumentError):
    code = "document_not_found"

    def __init__(self, document_id: uuid.UUID) -> None:
        super().__init__(f"no document {document_id}")
        self.document_id = document_id


# --------------------------------------------------------------------------- reads


def _live_documents_for(employee_id: uuid.UUID) -> Select[tuple[Document]]:
    """Non-deleted documents for one employee, newest first."""
    return (
        select(Document)
        .where(Document.employee_id == employee_id)
        .where(Document.deleted_at.is_(None))
        .order_by(Document.created_at.desc(), Document.id)
    )


def list_documents(session: Session, employee_id: uuid.UUID) -> Sequence[Document]:
    """Every non-deleted document held against an employee (Requirement 4.1)."""
    return list(session.scalars(_live_documents_for(employee_id)))


def get_document(session: Session, document_id: uuid.UUID) -> Document:
    """One non-deleted document, or raise `DocumentNotFound`.

    A soft-deleted document reads as absent: its bytes are gone from storage and it must not be
    downloadable, so treating it as not-found is the correct answer to a read as well as a download.
    """
    document = session.scalars(
        select(Document).where(Document.id == document_id).where(Document.deleted_at.is_(None))
    ).one_or_none()
    if document is None:
        raise DocumentNotFound(document_id)
    return document


def has_expired_document(session: Session, employee_id: uuid.UUID, *, on_date: date) -> bool:
    """Whether the employee has any non-deleted document past its expiry (Requirement 4.5).

    The flag the employee card exposes. Computed by query so it is cheap on a card that does not load
    the documents themselves, and evaluated against `on_date` so "expired" means the same thing here
    as it does in the sweep.
    """
    statement = (
        select(func.count())
        .select_from(Document)
        .where(Document.employee_id == employee_id)
        .where(Document.deleted_at.is_(None))
        .where(Document.expiry_date.is_not(None))
        .where(Document.expiry_date < on_date)
    )
    return (session.scalar(statement) or 0) > 0


# --------------------------------------------------------------------------- upload


def begin_upload(
    session: Session,
    employee_id: uuid.UUID,
    payload: DocumentUploadInit,
    *,
    storage: ObjectStorage,
) -> storage_module.PresignedUpload:
    """Issue a constrained presigned upload URL for a new document (Requirement 4.2, 4.3).

    The declared type is checked against the allowed set inside the presign, so an unsupported type
    is refused before a URL is minted. The size is declared here and capped in the schema; the real
    size is enforced again server-side at completion, because a presigned PUT cannot bound the body
    by itself. `employee_id` is validated by the router's existence check before this is called.
    """
    file_key = storage.new_key(employee_id, payload.file_name)
    return storage.presign_upload(file_key, payload.mime_type)


def complete_upload(
    session: Session,
    employee_id: uuid.UUID,
    payload: DocumentUploadComplete,
    *,
    storage: ObjectStorage,
    context: AuditContext,
    uploaded_by_user_id: uuid.UUID | None,
) -> Document:
    """Verify an uploaded object and record the document row (Requirement 4.1, 4.2).

    The verification is the point: `storage.verify_upload` reads the stored bytes back and confirms
    the object exists, is within the 10 MB cap, and really is its declared type by magic number. Only
    if all three hold is a row written. A verification failure raises a storage error the router maps
    to a 4xx, and no row is created, so a failed or oversize upload leaves no dangling metadata.
    """
    verified = storage.verify_upload(payload.file_key, payload.mime_type)

    document = Document(
        employee_id=employee_id,
        type=payload.type,
        file_key=verified.file_key,
        file_name=payload.file_name,
        mime_type=verified.mime_type,
        size_bytes=verified.size_bytes,
        expiry_date=payload.expiry_date,
        uploaded_by_user_id=uploaded_by_user_id,
    )
    session.add(document)
    session.flush()

    empty = dict.fromkeys(snapshot(document), None)
    record_model_changes(session, document, empty, context=context, reason="document_uploaded")
    return document


# --------------------------------------------------------------------------- download


def build_download_url(session: Session, document: Document, *, storage: ObjectStorage) -> str:
    """A short-lived signed URL for an already-authorized document (Requirement 4.3).

    Authorization must have been checked by the caller before this is reached — this function grants
    access by minting the capability, so a check afterwards would be too late.
    """
    return storage.presign_download(document.file_key, file_name=document.file_name)


# --------------------------------------------------------------------------- delete


def delete_document(
    session: Session, document: Document, *, context: AuditContext, storage: ObjectStorage
) -> Document:
    """Soft-delete a document and remove its bytes from storage (Requirement 12.7 pattern).

    The row is kept with `deleted_at` set, so an expired-permit dispute can still show the document
    existed and when it went; the bytes are removed from storage because keeping a file nobody can
    reach is only a liability. Best-effort on the storage delete: the row is the record of truth, and
    a storage hiccup must not block the removal.
    """
    before = snapshot(document, fields=["deleted_at"])
    document.deleted_at = datetime.now(UTC)
    session.flush()
    record_model_changes(
        session, document, before, context=context, reason="document_deleted", fields=["deleted_at"]
    )

    # The soft delete stands even if the object is already gone: the row is the record of truth.
    with contextlib.suppress(Exception):
        storage.delete(document.file_key)

    return document


# --------------------------------------------------------------------------- expiry sweep


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one run of the expiry sweep did, so a job log and a test can see the outcome.

    `warnings_raised` and `escalations_raised` count *new* notifications, not documents examined — a
    second run in the same window finds every key already taken and reports zero of each, which is the
    idempotency the requirement asks for made visible.
    """

    documents_examined: int
    warnings_raised: int
    escalations_raised: int


def run_document_expiry_sweep(
    session: Session,
    *,
    today: date,
    context: AuditContext | None = None,
) -> SweepResult:
    """Raise warnings and escalations for expiring and expired documents (Requirements 4.4–4.6).

    Idempotent by construction: every notification is created under a `dedupe_key` that names the
    document, its expiry date and the window, so a re-run in the same window collides on the key and
    raises nothing new. The window boundary is strict — a document expiring today is a warning, one
    that expired yesterday is an escalation — so the two windows partition the timeline with no gap
    and no document counted in both.

    Notifications go to every administrator, which is who Requirement 4.4 names. The recipient set is
    resolved once per run rather than per document.
    """
    context = context or AuditContext(reason="document_expiry_sweep")
    warning_window_days = settings_service.get_int_or(
        session, WARNING_WINDOW_SETTING, DEFAULT_WARNING_WINDOW_DAYS
    )
    admins = _administrators(session)

    documents = list(
        session.scalars(
            select(Document)
            .where(Document.deleted_at.is_(None))
            .where(Document.expiry_date.is_not(None))
        )
    )

    warnings_raised = 0
    escalations_raised = 0
    for document in documents:
        assert document.expiry_date is not None  # noqa: S101 - guaranteed by the query above
        days_until_expiry = (document.expiry_date - today).days

        if days_until_expiry < 0:
            escalations_raised += _raise_for_all(
                session,
                admins,
                document=document,
                notification_type=NOTIFICATION_TYPE_ESCALATION,
                title_key=TITLE_KEY_ESCALATION,
                severity=NotificationSeverity.CRITICAL,
            )
        elif days_until_expiry <= warning_window_days:
            warnings_raised += _raise_for_all(
                session,
                admins,
                document=document,
                notification_type=NOTIFICATION_TYPE_WARNING,
                title_key=TITLE_KEY_WARNING,
                severity=NotificationSeverity.WARNING,
            )

    # A single audit line recording the sweep ran and what it raised, attributed to the system
    # (Requirement 13.5: scheduled actions are recorded like any other).
    record_change(
        session,
        entity_type="documents",
        entity_id=_SWEEP_MARKER_ID,
        field="expiry_sweep",
        old_value=None,
        new_value=f"warnings={warnings_raised} escalations={escalations_raised}",
        context=context,
        reason="document_expiry_sweep",
    )

    return SweepResult(
        documents_examined=len(documents),
        warnings_raised=warnings_raised,
        escalations_raised=escalations_raised,
    )


#: A fixed, non-entity id used for the sweep's summary audit row. The sweep is not a change to one
#: document, so it is recorded against a stable marker rather than pretending to edit a row.
_SWEEP_MARKER_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _administrators(session: Session) -> Sequence[User]:
    """Active administrators, who receive document-expiry alerts (Requirement 4.4)."""
    return list(
        session.scalars(
            select(User).where(User.role == UserRole.ADMIN).where(User.is_active.is_(True))
        )
    )


def _raise_for_all(
    session: Session,
    recipients: Sequence[User],
    *,
    document: Document,
    notification_type: str,
    title_key: str,
    severity: NotificationSeverity,
) -> int:
    """Raise one deduplicated notification per recipient; return how many were newly created.

    The dedupe key includes the recipient, the document, its expiry date and the window word, so two
    administrators each get their own notification, a re-run raises none of them again, and a document
    whose expiry date is later corrected produces a fresh notification rather than being silenced by
    the old key.
    """
    created = 0
    for recipient in recipients:
        dedupe_key = _dedupe_key(
            notification_type=notification_type,
            recipient_user_id=recipient.id,
            document=document,
        )
        _notification, was_created = notification_service.create_deduplicated(
            session,
            recipient_user_id=recipient.id,
            type=notification_type,
            dedupe_key=dedupe_key,
            title_key=title_key,
            severity=severity,
            body_params={
                "employee_id": str(document.employee_id),
                "document_id": str(document.id),
                "document_type": document.type.value,
                "expiry_date": document.expiry_date.isoformat() if document.expiry_date else None,
            },
            related_entity_type="documents",
            related_entity_id=document.id,
        )
        if was_created:
            created += 1
    return created


def _dedupe_key(*, notification_type: str, recipient_user_id: uuid.UUID, document: Document) -> str:
    """The key that makes a re-run a no-op (Requirement 4.6).

    Built from the notification type (which encodes the window), the recipient, the document, and the
    expiry date. Including the expiry date means a corrected date is a genuinely new condition and is
    alerted afresh; leaving it out would let a stale warning suppress a real one.
    """
    expiry = document.expiry_date.isoformat() if document.expiry_date else "none"
    return f"{notification_type}:{document.id}:{expiry}:{recipient_user_id}"
