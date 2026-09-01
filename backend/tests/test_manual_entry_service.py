"""Manual time entry and correction — the write service (Requirement 12).

The read service is covered in `test_time_entry_service.py`; here the subject is the write side that
Task 21 adds: creating a shift by hand (Requirement 12.1), correcting one (12.2), and soft-deleting
one (12.7). The claims worth a unit test are that the same rules a scanned entry obeys apply to a
manual one (12.5) — an overlap is refused naming the conflict, a locked month is refused, an
implausible duration is flagged — that every hand-touched entry is marked manual (12.4), that a
correction with no time to change is refused, and that a soft delete retains the row. Role and scope
are the router's concern and are pinned in `test_manual_entry_api.py`.

The reason is mandatory and non-blank at the schema; that this holds is asserted at the API layer,
where a request body reaches the validator, and in `test_manual_entry_schema.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.employee import Employee
from app.models.period_lock import PeriodLock
from app.models.site import Site
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.schemas.time_entry import TimeEntryCreate, TimeEntryUpdate
from app.services import scan as scan_service
from app.services import time_entry as time_entry_service
from app.services.audit import AuditContext

_PASSPORT = iter(f"P{n:07d}" for n in range(1, 100000))


def _context() -> AuditContext:
    return AuditContext(actor_user_id=uuid.uuid4(), reason="manual")


def _make_employee(session: Session) -> Employee:
    passport = next(_PASSPORT)
    employee = Employee(
        full_name="Worker",
        full_name_en="Worker",
        passport_number=passport,
        passport_number_hash=passport,
        phone="+972500000000",
        country="Israel",
        emergency_contact_name="Contact",
        emergency_contact_phone="+972500000001",
        start_date=date(2025, 1, 1),
    )
    session.add(employee)
    session.flush()
    return employee


def _make_site(session: Session, *, number: str = "S-1") -> Site:
    client = Client(name="Acme")
    session.add(client)
    session.flush()
    site = Site(
        name=f"Site {number}",
        site_number=number,
        client_id=client.id,
        qr_token=f"placeholder-{uuid.uuid4().hex}",
        qr_token_version=1,
    )
    session.add(site)
    session.flush()
    return site


def _at(hour: int, minute: int = 0, day: int = 30) -> datetime:
    return datetime(2025, 8, day, hour, minute, tzinfo=UTC)


def _create_payload(employee: Employee, site: Site, *, check_in: datetime, check_out: datetime):
    return TimeEntryCreate(
        employee_id=employee.id,
        site_id=site.id,
        check_in_at=check_in,
        check_out_at=check_out,
        reason="scan device was offline",
    )


# --------------------------------------------------------------------------- create


def test_create_marks_the_entry_manual_and_stores_the_reason(session: Session):
    """Requirement 12.1, 12.4: a hand-created entry is source=manual, is_manual, with its reason."""
    employee = _make_employee(session)
    site = _make_site(session)

    entry = time_entry_service.create_manual_entry(
        session,
        _create_payload(employee, site, check_in=_at(8), check_out=_at(12)),
        context=_context(),
    )

    assert entry.source is TimeEntrySource.MANUAL
    assert entry.is_manual is True
    assert entry.manual_reason == "scan device was offline"
    assert entry.status is TimeEntryStatus.DRAFT


def test_create_computes_the_total_in_whole_minutes(session: Session):
    """Requirement 10.2, 12.5: the stored total is the whole minutes between the two instants."""
    employee = _make_employee(session)
    site = _make_site(session)

    entry = time_entry_service.create_manual_entry(
        session,
        _create_payload(employee, site, check_in=_at(8, 0), check_out=_at(12, 29)),
        context=_context(),
    )

    assert entry.total_minutes == 269  # 4h29m


def test_create_attributes_the_work_date_from_the_check_in(session: Session):
    """Requirement 10.4: with no explicit work date, the entry is attributed to the check-in's day."""
    employee = _make_employee(session)
    site = _make_site(session)

    # 23:30 UTC on the 30th is past midnight in Asia/Jerusalem (03:30 on the 31st), so the local
    # work date is the 31st — the same attribution a scan makes.
    entry = time_entry_service.create_manual_entry(
        session,
        _create_payload(employee, site, check_in=_at(23, 30), check_out=datetime(2025, 8, 31, 1, tzinfo=UTC)),
        context=_context(),
    )

    assert entry.work_date == date(2025, 8, 31)


def test_create_rejects_a_check_out_not_after_check_in(session: Session):
    """Requirement 10.3: a check-out at or before the check-in is refused."""
    employee = _make_employee(session)
    site = _make_site(session)

    with pytest.raises(time_entry_service.CheckOutNotAfterCheckIn):
        time_entry_service.create_manual_entry(
            session,
            _create_payload(employee, site, check_in=_at(12), check_out=_at(12)),
            context=_context(),
        )


def test_create_flags_an_implausible_duration(session: Session):
    """Requirement 10.5, 12.5: a shift at least the threshold long is flagged, not refused."""
    employee = _make_employee(session)
    site = _make_site(session)

    # Default threshold is 16 hours; a 17-hour shift is over it.
    entry = time_entry_service.create_manual_entry(
        session,
        _create_payload(employee, site, check_in=_at(6), check_out=_at(23)),
        context=_context(),
    )

    assert scan_service.FLAG_IMPLAUSIBLE_DURATION in entry.flags


def test_create_rejects_an_overlap_naming_the_conflict(session: Session):
    """Requirement 11.7, 12.5: a manual entry overlapping an existing one is refused, naming it."""
    employee = _make_employee(session)
    site = _make_site(session)
    existing = time_entry_service.create_manual_entry(
        session,
        _create_payload(employee, site, check_in=_at(8), check_out=_at(12)),
        context=_context(),
    )
    session.flush()

    with pytest.raises(scan_service.OverlapRejected) as excinfo:
        time_entry_service.create_manual_entry(
            session,
            _create_payload(employee, site, check_in=_at(11), check_out=_at(15)),
            context=_context(),
        )

    assert excinfo.value.conflicting_entry is not None
    assert excinfo.value.conflicting_entry.id == existing.id


def test_create_rejects_a_write_into_a_locked_month(session: Session):
    """Requirement 12.5, 15.5: a manual entry into a locked month is refused."""
    employee = _make_employee(session)
    site = _make_site(session)
    session.add(PeriodLock(year=2025, month=8, locked_at=datetime.now(UTC)))
    session.flush()

    with pytest.raises(scan_service.PeriodLocked):
        time_entry_service.create_manual_entry(
            session,
            _create_payload(employee, site, check_in=_at(8), check_out=_at(12)),
            context=_context(),
        )


def test_create_rejects_an_unknown_employee_or_site(session: Session):
    """A manual entry naming a nonexistent employee or site is a not-found."""
    employee = _make_employee(session)
    site = _make_site(session)

    with pytest.raises(time_entry_service.EmployeeNotFound):
        time_entry_service.create_manual_entry(
            session,
            TimeEntryCreate(
                employee_id=uuid.uuid4(), site_id=site.id,
                check_in_at=_at(8), check_out_at=_at(12), reason="x",
            ),
            context=_context(),
        )

    with pytest.raises(time_entry_service.SiteNotFound):
        time_entry_service.create_manual_entry(
            session,
            TimeEntryCreate(
                employee_id=employee.id, site_id=uuid.uuid4(),
                check_in_at=_at(8), check_out_at=_at(12), reason="x",
            ),
            context=_context(),
        )


# --------------------------------------------------------------------------- correct


def test_correct_edits_the_check_out_and_recomputes_the_total(session: Session):
    """Requirement 12.2: editing the check-out time recomputes the stored total."""
    employee = _make_employee(session)
    site = _make_site(session)
    entry = time_entry_service.create_manual_entry(
        session,
        _create_payload(employee, site, check_in=_at(8), check_out=_at(12)),
        context=_context(),
    )
    session.flush()

    corrected = time_entry_service.correct_manual_entry(
        session, entry.id,
        TimeEntryUpdate(check_out_at=_at(13), reason="worked an extra hour"),
        context=_context(),
    )

    assert corrected.check_out_at == _at(13)
    assert corrected.total_minutes == 300  # 5h


def test_correct_marks_a_scanned_entry_manual(session: Session):
    """Requirement 12.4: correcting a scanned entry marks it manual so the edit is disclosed."""
    employee = _make_employee(session)
    site = _make_site(session)
    scanned = TimeEntry(
        employee_id=employee.id, site_id=site.id, work_date=date(2025, 8, 30),
        check_in_at=_at(8), check_out_at=_at(12), total_minutes=240,
        source=TimeEntrySource.QR_SCAN, is_manual=False, status=TimeEntryStatus.DRAFT, flags=[],
    )
    session.add(scanned)
    session.flush()

    corrected = time_entry_service.correct_manual_entry(
        session, scanned.id,
        TimeEntryUpdate(check_in_at=_at(7, 30), reason="clocked in earlier"),
        context=_context(),
    )

    assert corrected.is_manual is True
    # The source is left as scanned: a corrected scan is still a scan by origin.
    assert corrected.source is TimeEntrySource.QR_SCAN


def test_correct_rejects_no_time_field(session: Session):
    """Requirement 12.2: a correction that changes no time is refused."""
    employee = _make_employee(session)
    site = _make_site(session)
    entry = time_entry_service.create_manual_entry(
        session,
        _create_payload(employee, site, check_in=_at(8), check_out=_at(12)),
        context=_context(),
    )
    session.flush()

    with pytest.raises(time_entry_service.NoTimeFieldToUpdate):
        time_entry_service.correct_manual_entry(
            session, entry.id, TimeEntryUpdate(reason="no change"), context=_context()
        )


def test_correct_rejects_times_out_of_order(session: Session):
    """Requirement 10.3: an edit that would put check-out before check-in is refused."""
    employee = _make_employee(session)
    site = _make_site(session)
    entry = time_entry_service.create_manual_entry(
        session,
        _create_payload(employee, site, check_in=_at(8), check_out=_at(12)),
        context=_context(),
    )
    session.flush()

    with pytest.raises(time_entry_service.CheckOutNotAfterCheckIn):
        time_entry_service.correct_manual_entry(
            session, entry.id, TimeEntryUpdate(check_in_at=_at(13), reason="typo"),
            context=_context(),
        )


def test_correct_rejects_an_overlap_naming_the_conflict(session: Session):
    """Requirement 11.7, 12.5: a correction that straddles another entry is refused, naming it."""
    employee = _make_employee(session)
    site = _make_site(session)
    first = time_entry_service.create_manual_entry(
        session,
        _create_payload(employee, site, check_in=_at(8), check_out=_at(10)),
        context=_context(),
    )
    second = time_entry_service.create_manual_entry(
        session,
        _create_payload(employee, site, check_in=_at(11), check_out=_at(13)),
        context=_context(),
    )
    session.flush()

    with pytest.raises(scan_service.OverlapRejected) as excinfo:
        time_entry_service.correct_manual_entry(
            session, second.id, TimeEntryUpdate(check_in_at=_at(9, 30), reason="fix"),
            context=_context(),
        )

    assert excinfo.value.conflicting_entry is not None
    assert excinfo.value.conflicting_entry.id == first.id


def test_correct_rejects_a_missing_entry(session: Session):
    """A correction of a nonexistent entry is a not-found."""
    with pytest.raises(time_entry_service.TimeEntryNotFound):
        time_entry_service.correct_manual_entry(
            session, uuid.uuid4(), TimeEntryUpdate(check_out_at=_at(13), reason="x"),
            context=_context(),
        )


# --------------------------------------------------------------------------- soft delete


def test_soft_delete_retains_the_row(session: Session):
    """Requirement 12.7: a deletion sets deleted_at + delete_reason and keeps the row for audit."""
    employee = _make_employee(session)
    site = _make_site(session)
    entry = time_entry_service.create_manual_entry(
        session,
        _create_payload(employee, site, check_in=_at(8), check_out=_at(12)),
        context=_context(),
    )
    session.flush()
    entry_id = entry.id

    deleted = time_entry_service.soft_delete_entry(
        session, entry_id, reason="duplicate of another entry", context=_context()
    )

    assert deleted.deleted_at is not None
    assert deleted.delete_reason == "duplicate of another entry"
    # The row is still there — read it back directly, bypassing the live-only filter.
    still_present = session.get(TimeEntry, entry_id)
    assert still_present is not None
    assert still_present.deleted_at is not None


def test_soft_delete_excludes_the_entry_from_get_live(session: Session):
    """A deleted entry is no longer a live record: get_live_entry raises not-found."""
    employee = _make_employee(session)
    site = _make_site(session)
    entry = time_entry_service.create_manual_entry(
        session,
        _create_payload(employee, site, check_in=_at(8), check_out=_at(12)),
        context=_context(),
    )
    session.flush()
    time_entry_service.soft_delete_entry(session, entry.id, reason="mistake", context=_context())

    with pytest.raises(time_entry_service.TimeEntryNotFound):
        time_entry_service.get_live_entry(session, entry.id)


def test_soft_delete_rejects_a_locked_month(session: Session):
    """Requirement 15.5: deleting an entry in a locked month is refused."""
    employee = _make_employee(session)
    site = _make_site(session)
    entry = time_entry_service.create_manual_entry(
        session,
        _create_payload(employee, site, check_in=_at(8), check_out=_at(12)),
        context=_context(),
    )
    session.flush()
    session.add(PeriodLock(year=2025, month=8, locked_at=datetime.now(UTC)))
    session.flush()

    with pytest.raises(scan_service.PeriodLocked):
        time_entry_service.soft_delete_entry(session, entry.id, reason="x", context=_context())


# --------------------------------------------------------------------------- audit


def test_a_manual_create_writes_audit_rows(session: Session):
    """Requirement 13.2: creating an entry emits audit rows in the caller's transaction."""
    from app.models.change_log import ChangeLog

    employee = _make_employee(session)
    site = _make_site(session)
    entry = time_entry_service.create_manual_entry(
        session,
        _create_payload(employee, site, check_in=_at(8), check_out=_at(12)),
        context=_context(),
    )
    session.flush()

    rows = session.query(ChangeLog).filter(ChangeLog.entity_id == entry.id).all()
    assert rows, "a manual creation must leave an audit trail"
    assert all(row.reason == "scan device was offline" for row in rows)
