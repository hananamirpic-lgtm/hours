"""Document service rules that do not need HTTP (Requirement 4).

The claims pinned here are the ones the task calls out:

* an oversize upload and a wrong-type upload are both rejected server-side, by the verification step
  that reads the stored bytes back rather than trusting the declared header (Requirement 4.2);
* the daily expiry sweep is idempotent — two runs on the same day produce one set of notifications,
  not two (Requirement 4.6);
* the sweep raises a warning inside the window and an escalation after expiry, and the employee's
  expired-document flag reflects a passed expiry (Requirements 4.4, 4.5).

These run against the in-memory SQLite session the suite provides. The `dedupe_key` unique constraint
that makes idempotency work is enforced on SQLite as well as PostgreSQL, so the idempotency claim is
tested on the same mechanism production relies on. Object storage is the in-memory fake, which
enforces the same size and magic-number rules as the real client.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.storage import (
    MAX_FILE_BYTES,
    FileTooLarge,
    UnsupportedFileType,
    UploadNotFound,
    UploadTypeMismatch,
)
from app.models.document import DocumentType
from app.models.notification import Notification, NotificationSeverity
from app.models.setting import Setting, SettingValueType
from app.models.user import User, UserRole
from app.schemas.document import DocumentUploadComplete, DocumentUploadInit
from app.schemas.employee import EmployeeCreate
from app.services import document as document_service
from app.services import employee as employee_service
from app.services.audit import AuditContext
from document_support import JPEG_BYTES, PDF_BYTES, PNG_BYTES, FakeObjectStorage


def _context(actor: uuid.UUID | None = None) -> AuditContext:
    return AuditContext(actor_user_id=actor)


def _make_employee(session: Session) -> uuid.UUID:
    payload = EmployeeCreate(
        full_name="Ahmed Khalil",
        full_name_en="Ahmed Khalil",
        passport_number=f"A{uuid.uuid4().hex[:8].upper()}",
        phone="+972500000000",
        country="Jordan",
        emergency_contact_name="Layla Khalil",
        emergency_contact_phone="+972500000001",
        start_date=date(2025, 1, 1),
    )
    employee = employee_service.create_employee(session, payload, context=_context())
    session.commit()
    return employee.id


def _seed_admin(session: Session, username: str) -> User:
    admin = User(username=username, password_hash="x", role=UserRole.ADMIN, is_active=True)
    session.add(admin)
    session.commit()
    return admin


def _seed_warning_window(session: Session, days: int = 30) -> None:
    session.add(
        Setting(
            key=document_service.WARNING_WINDOW_SETTING,
            value=str(days),
            value_type=SettingValueType.INTEGER,
        )
    )
    session.commit()


def _upload(
    session: Session,
    employee_id: uuid.UUID,
    storage: FakeObjectStorage,
    *,
    declared_mime: str,
    data: bytes,
    expiry_date: date | None = None,
    file_name: str = "permit.pdf",
    doc_type: DocumentType = DocumentType.WORK_PERMIT,
):
    """Drive the two-step upload with the fake: presign, seed the bytes, complete."""
    init = DocumentUploadInit(
        type=doc_type,
        file_name=file_name,
        mime_type=declared_mime,
        size_bytes=max(len(data), 1),
        expiry_date=expiry_date,
    )
    presigned = document_service.begin_upload(session, employee_id, init, storage=storage)
    storage.put(presigned.file_key, data)
    complete = DocumentUploadComplete(
        file_key=presigned.file_key,
        type=doc_type,
        file_name=file_name,
        mime_type=declared_mime,
        expiry_date=expiry_date,
    )
    document = document_service.complete_upload(
        session,
        employee_id,
        complete,
        storage=storage,
        context=_context(),
        uploaded_by_user_id=None,
    )
    session.commit()
    return document


# --------------------------------------------------------------------------- type verification


def test_a_supported_upload_of_the_right_bytes_is_recorded(session: Session):
    """A PDF declared and stored as a PDF is verified and recorded (Requirement 4.1, 4.2)."""
    employee_id = _make_employee(session)
    storage = FakeObjectStorage()

    document = _upload(session, employee_id, storage, declared_mime="application/pdf", data=PDF_BYTES)

    assert document.mime_type == "application/pdf"
    assert document.size_bytes == len(PDF_BYTES)
    assert document.type is DocumentType.WORK_PERMIT


@pytest.mark.parametrize("declared", ["image/jpeg", "image/png"])
def test_supported_image_types_are_accepted(session: Session, declared: str):
    """JPEG and PNG round-trip as well as PDF (Requirement 4.2 lists all three)."""
    employee_id = _make_employee(session)
    storage = FakeObjectStorage()
    data = JPEG_BYTES if declared == "image/jpeg" else PNG_BYTES

    document = _upload(session, employee_id, storage, declared_mime=declared, data=data)

    assert document.mime_type == declared


def test_an_unsupported_declared_type_is_rejected_before_a_url_is_issued(session: Session):
    """Requirement 4.2: only PDF/JPEG/PNG. A declared GIF never gets an upload URL."""
    employee_id = _make_employee(session)
    storage = FakeObjectStorage()
    init = DocumentUploadInit(
        type=DocumentType.OTHER, file_name="x.gif", mime_type="image/gif", size_bytes=10
    )

    with pytest.raises(UnsupportedFileType):
        document_service.begin_upload(session, employee_id, init, storage=storage)


def test_a_wrong_type_upload_is_rejected_by_server_side_verification(session: Session):
    """Requirement 4.2. A PNG stored under an `application/pdf` claim fails the magic-number check,
    and no document row is written — the header is not trusted."""
    employee_id = _make_employee(session)
    storage = FakeObjectStorage()

    init = DocumentUploadInit(
        type=DocumentType.OTHER, file_name="fake.pdf", mime_type="application/pdf", size_bytes=64
    )
    presigned = document_service.begin_upload(session, employee_id, init, storage=storage)
    # A PNG's bytes stored under a PDF claim.
    storage.put(presigned.file_key, PNG_BYTES)
    complete = DocumentUploadComplete(
        file_key=presigned.file_key,
        type=DocumentType.OTHER,
        file_name="fake.pdf",
        mime_type="application/pdf",
    )

    with pytest.raises(UploadTypeMismatch):
        document_service.complete_upload(
            session,
            employee_id,
            complete,
            storage=storage,
            context=_context(),
            uploaded_by_user_id=None,
        )

    assert document_service.list_documents(session, employee_id) == []


def test_an_oversize_upload_is_rejected_by_server_side_verification(session: Session):
    """Requirement 4.2: 10 MB cap. An object over the cap fails verification and records nothing."""
    employee_id = _make_employee(session)
    storage = FakeObjectStorage()

    init = DocumentUploadInit(
        type=DocumentType.OTHER, file_name="big.pdf", mime_type="application/pdf", size_bytes=64
    )
    presigned = document_service.begin_upload(session, employee_id, init, storage=storage)
    # A valid-typed but oversize object: the client can beat the presign size hint, so the
    # server-side check is what actually enforces the cap.
    storage.put(presigned.file_key, PDF_BYTES + b"0" * (MAX_FILE_BYTES + 1))
    complete = DocumentUploadComplete(
        file_key=presigned.file_key,
        type=DocumentType.OTHER,
        file_name="big.pdf",
        mime_type="application/pdf",
    )

    with pytest.raises(FileTooLarge):
        document_service.complete_upload(
            session,
            employee_id,
            complete,
            storage=storage,
            context=_context(),
            uploaded_by_user_id=None,
        )

    assert document_service.list_documents(session, employee_id) == []


def test_completing_with_no_uploaded_object_is_a_not_found(session: Session):
    """Reporting an upload that never landed is refused, not silently recorded."""
    employee_id = _make_employee(session)
    storage = FakeObjectStorage()
    complete = DocumentUploadComplete(
        file_key="employees/x/never/uploaded.pdf",
        type=DocumentType.OTHER,
        file_name="uploaded.pdf",
        mime_type="application/pdf",
    )

    with pytest.raises(UploadNotFound):
        document_service.complete_upload(
            session,
            employee_id,
            complete,
            storage=storage,
            context=_context(),
            uploaded_by_user_id=None,
        )


# --------------------------------------------------------------------------- expired flag


def test_the_employee_expired_flag_reflects_a_passed_expiry(session: Session):
    """Requirement 4.5. A document whose expiry has passed flags the employee as expired; a
    document expiring in the future does not."""
    employee_id = _make_employee(session)
    storage = FakeObjectStorage()
    today = date(2025, 6, 1)

    _upload(
        session,
        employee_id,
        storage,
        declared_mime="application/pdf",
        data=PDF_BYTES,
        expiry_date=today - timedelta(days=1),
    )
    assert document_service.has_expired_document(session, employee_id, on_date=today) is True

    other_id = _make_employee(session)
    _upload(
        session,
        other_id,
        storage,
        declared_mime="application/pdf",
        data=PDF_BYTES,
        expiry_date=today + timedelta(days=10),
    )
    assert document_service.has_expired_document(session, other_id, on_date=today) is False


def test_a_deleted_document_does_not_flag_the_employee(session: Session):
    """A soft-deleted expired document must not keep flagging the employee (Requirement 4.5)."""
    employee_id = _make_employee(session)
    storage = FakeObjectStorage()
    today = date(2025, 6, 1)
    document = _upload(
        session,
        employee_id,
        storage,
        declared_mime="application/pdf",
        data=PDF_BYTES,
        expiry_date=today - timedelta(days=1),
    )

    document_service.delete_document(session, document, context=_context(), storage=storage)
    session.commit()

    assert document.file_key in storage.deleted_keys
    assert document_service.has_expired_document(session, employee_id, on_date=today) is False


# --------------------------------------------------------------------------- expiry sweep


def test_the_sweep_raises_a_warning_inside_the_window(session: Session):
    """Requirement 4.4. A document expiring inside the 30-day window warns each administrator."""
    _seed_warning_window(session, days=30)
    admin_a = _seed_admin(session, "admin_a")
    admin_b = _seed_admin(session, "admin_b")
    employee_id = _make_employee(session)
    storage = FakeObjectStorage()
    today = date(2025, 6, 1)
    _upload(
        session,
        employee_id,
        storage,
        declared_mime="application/pdf",
        data=PDF_BYTES,
        expiry_date=today + timedelta(days=10),
    )

    result = document_service.run_document_expiry_sweep(session, today=today)
    session.commit()

    assert result.warnings_raised == 2  # one per administrator
    assert result.escalations_raised == 0
    warnings = session.scalars(
        select(Notification).where(Notification.type == document_service.NOTIFICATION_TYPE_WARNING)
    ).all()
    assert {w.recipient_user_id for w in warnings} == {admin_a.id, admin_b.id}
    assert all(w.severity is NotificationSeverity.WARNING for w in warnings)


def test_the_sweep_escalates_after_expiry(session: Session):
    """Requirement 4.5. A document past its expiry escalates rather than merely warning."""
    _seed_warning_window(session, days=30)
    _seed_admin(session, "admin_a")
    employee_id = _make_employee(session)
    storage = FakeObjectStorage()
    today = date(2025, 6, 1)
    _upload(
        session,
        employee_id,
        storage,
        declared_mime="application/pdf",
        data=PDF_BYTES,
        expiry_date=today - timedelta(days=1),
    )

    result = document_service.run_document_expiry_sweep(session, today=today)
    session.commit()

    assert result.escalations_raised == 1
    assert result.warnings_raised == 0
    escalation = session.scalars(
        select(Notification).where(Notification.type == document_service.NOTIFICATION_TYPE_ESCALATION)
    ).one()
    assert escalation.severity is NotificationSeverity.CRITICAL


def test_a_document_outside_the_window_raises_nothing(session: Session):
    """A document expiring well beyond the window is not alerted yet (Requirement 4.4)."""
    _seed_warning_window(session, days=30)
    _seed_admin(session, "admin_a")
    employee_id = _make_employee(session)
    storage = FakeObjectStorage()
    today = date(2025, 6, 1)
    _upload(
        session,
        employee_id,
        storage,
        declared_mime="application/pdf",
        data=PDF_BYTES,
        expiry_date=today + timedelta(days=90),
    )

    result = document_service.run_document_expiry_sweep(session, today=today)
    session.commit()

    assert result.warnings_raised == 0
    assert result.escalations_raised == 0


def test_the_sweep_is_idempotent_across_two_runs_on_the_same_day(session: Session):
    """Requirement 4.6. Running the sweep twice on the same day produces one set of notifications,
    not two: the second run collides on the dedupe key and raises nothing new."""
    _seed_warning_window(session, days=30)
    _seed_admin(session, "admin_a")
    _seed_admin(session, "admin_b")
    employee_id = _make_employee(session)
    storage = FakeObjectStorage()
    today = date(2025, 6, 1)
    # One expiring (warning) and one expired (escalation), so both windows are exercised.
    _upload(
        session,
        employee_id,
        storage,
        declared_mime="application/pdf",
        data=PDF_BYTES,
        expiry_date=today + timedelta(days=5),
        file_name="soon.pdf",
    )
    _upload(
        session,
        employee_id,
        storage,
        declared_mime="image/png",
        data=PNG_BYTES,
        expiry_date=today - timedelta(days=2),
        file_name="gone.png",
    )

    first = document_service.run_document_expiry_sweep(session, today=today)
    session.commit()
    second = document_service.run_document_expiry_sweep(session, today=today)
    session.commit()

    # First run raises: 2 admins × 1 warning + 2 admins × 1 escalation.
    assert (first.warnings_raised, first.escalations_raised) == (2, 2)
    # Second run raises nothing new.
    assert (second.warnings_raised, second.escalations_raised) == (0, 0)

    # And the table holds exactly one notification per (admin, document, window): 4 in total.
    total = session.scalar(select(func.count()).select_from(Notification)) or 0
    assert total == 4
