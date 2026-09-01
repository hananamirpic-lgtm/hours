"""The transition and the duplicate-work invariant, exercised through the service on real PostgreSQL.

The scan service's logic is covered on SQLite in `tests/test_scan_service.py`, which is fast and
proves the branching. What SQLite cannot prove is the part of Requirement 11 that lives in the
database and nowhere else:

* the one-open-entry partial unique index actually rejects a second open shift — the write two
  concurrent check-ins at different sites both attempt (Requirement 11.3);
* the GiST exclusion constraint actually rejects an overlapping completed entry (Requirement 11.7);
* `tstzrange` is half-open, so a transition whose close touches the next open at one instant is
  accepted — the seam has no gap and no overlap (Requirement 11.5).

These run the real `app.services.scan` code against a migrated PostgreSQL, so they prove the service
sits correctly in front of the constraints rather than re-testing the constraints in isolation (that
is `test_time_entries_constraints.py`). Everything is written inside the `db` transaction the fixture
rolls back, so nothing survives the test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.qr_token import mint
from app.models.employee import Employee
from app.models.site import Site
from app.models.time_entry import TimeEntry, TimeEntrySource
from app.schemas.scan import ScanAction, ScanRequest
from app.services import scan as scan_service
from app.services.audit import AuditContext

WORK_DATE = date(2026, 8, 30)


def _context() -> AuditContext:
    return AuditContext(request_id="req-transition")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(WORK_DATE.year, WORK_DATE.month, WORK_DATE.day, hour, minute, tzinfo=UTC)


def _request(site: Site) -> ScanRequest:
    return ScanRequest(qr_token=mint(site.id, site.qr_token_version))


@pytest.fixture
def scan_session(db: sa.Connection) -> Session:
    """A session bound to the rolled-back connection, so service writes never escape the test."""
    return Session(bind=db, expire_on_commit=False)


@pytest.fixture
def employee(scan_session: Session) -> Employee:
    passport = f"P{uuid.uuid4().hex[:10]}"
    row = Employee(
        full_name="Worker",
        full_name_en="Worker",
        passport_number=passport,
        passport_number_hash=passport,
        phone="+972500000000",
        country="IL",
        emergency_contact_name="Contact",
        emergency_contact_phone="+972500000001",
        start_date=date(2025, 1, 1),
    )
    scan_session.add(row)
    scan_session.flush()
    return row


@pytest.fixture
def sites(scan_session: Session) -> tuple[Site, Site, Site]:
    """Three sites for one client — a multi-site day needs somewhere to move to."""
    from app.models.client import Client

    client = Client(name="Kibbutz Rosh Tzurim")
    scan_session.add(client)
    scan_session.flush()
    created: list[Site] = []
    for name, number in (("Site A", "S-A"), ("Site B", "S-B"), ("Site C", "S-C")):
        site = Site(
            name=name,
            site_number=number,
            client_id=client.id,
            qr_token=f"qr-{number}",
            qr_token_version=1,
        )
        scan_session.add(site)
        created.append(site)
    scan_session.flush()
    return created[0], created[1], created[2]


def _check_in(scan_session: Session, employee: Employee, site: Site, at: datetime) -> None:
    scan_service.resolve_scan(
        scan_session, employee_id=employee.id, request=_request(site), context=_context(), now=at
    )
    scan_session.flush()


def test_two_check_ins_at_different_sites_leave_exactly_one_open_entry(
    scan_session: Session, employee: Employee, sites: tuple[Site, Site, Site]
) -> None:
    """Requirement 11.3: two concurrent check-ins at different sites produce exactly one open entry.

    This is the race two concurrent scans run: each loads no open entry and tries to insert an open
    one. The one-open-entry index is what makes exactly one win — application code cannot, because
    both check first and both see nothing. The first check-in goes through the service; the second,
    modelling the racing scan that skipped the open_shift_elsewhere guard, inserts its open row
    directly and is refused by the index. A savepoint isolates the rejected write so the rest of the
    transaction survives, and exactly one open entry remains.
    """
    site_a, site_b, _ = sites
    _check_in(scan_session, employee, site_a, _at(7))

    with pytest.raises(sa.exc.IntegrityError) as error, scan_session.begin_nested():
        scan_session.add(
            TimeEntry(
                employee_id=employee.id,
                site_id=site_b.id,
                work_date=WORK_DATE,
                check_in_at=_at(9),
                source=TimeEntrySource.QR_SCAN,
                flags=[],
            )
        )
        scan_session.flush()
    assert "one_open_entry_per_employee" in str(error.value.orig)

    open_entries = scan_session.scalars(
        sa.select(TimeEntry).where(
            TimeEntry.employee_id == employee.id, TimeEntry.check_out_at.is_(None)
        )
    ).all()
    assert len(open_entries) == 1
    assert open_entries[0].site_id == site_a.id


def test_the_transition_is_atomic_with_no_gap_and_no_overlap(
    scan_session: Session, employee: Employee, sites: tuple[Site, Site, Site]
) -> None:
    """Requirement 11.5, 11.8: the close and the open share the transaction and meet at one instant."""
    site_a, site_b, _ = sites
    _check_in(scan_session, employee, site_a, _at(7))

    outcome = scan_service.transition(
        scan_session,
        employee_id=employee.id,
        request=_request(site_b),
        context=_context(),
        now=_at(9),
    )
    scan_session.flush()

    assert outcome.action is ScanAction.CHECK_IN
    entries = scan_session.scalars(
        sa.select(TimeEntry)
        .where(TimeEntry.employee_id == employee.id)
        .order_by(TimeEntry.check_in_at)
    ).all()
    assert len(entries) == 2
    closed, opened = entries
    # The close touches the open at 09:00 — half-open ranges, so the exclusion constraint accepts it.
    assert closed.site_id == site_a.id
    assert closed.check_out_at == _at(9)
    assert closed.source is TimeEntrySource.SYSTEM_TRANSITION
    assert opened.site_id == site_b.id
    assert opened.check_in_at == _at(9)
    assert opened.check_out_at is None
    # Exactly one open entry after the move, and no overlap survived the real constraint.
    assert sum(1 for e in entries if e.check_out_at is None) == 1


def test_a_three_site_day_totals_correctly_through_the_service(
    scan_session: Session, employee: Employee, sites: tuple[Site, Site, Site]
) -> None:
    """Requirement 11.1, 11.2: two transitions across three sites sum to the day's real minutes.

    Site A 07:00–09:00 (120), Site B 09:00–12:00 (180), Site C 12:00–17:00 (300): 600 minutes, split
    across three sites with no gap and no overlap, all accepted by the real exclusion constraint.
    """
    site_a, site_b, site_c = sites
    _check_in(scan_session, employee, site_a, _at(7))

    scan_service.transition(
        scan_session, employee_id=employee.id, request=_request(site_b), context=_context(), now=_at(9)
    )
    scan_session.flush()
    scan_service.transition(
        scan_session, employee_id=employee.id, request=_request(site_c), context=_context(), now=_at(12)
    )
    scan_session.flush()
    scan_service.check_out(scan_session, employee_id=employee.id, context=_context(), now=_at(17))
    scan_session.flush()

    entries = scan_session.scalars(
        sa.select(TimeEntry)
        .where(TimeEntry.employee_id == employee.id)
        .order_by(TimeEntry.check_in_at)
    ).all()
    assert len(entries) == 3
    assert [e.total_minutes for e in entries] == [120, 180, 300]
    assert sum(e.total_minutes for e in entries) == 600
    assert all(e.check_out_at is not None for e in entries)
    assert entries[0].check_out_at == entries[1].check_in_at
    assert entries[1].check_out_at == entries[2].check_in_at


def test_an_overlapping_close_is_rejected_by_the_constraint(
    scan_session: Session, employee: Employee, sites: tuple[Site, Site, Site]
) -> None:
    """Requirement 11.7: a completed entry that would overlap another is refused, naming the conflict.

    A completed entry sits 10:00–12:00; a second shift opened at 09:00 and closed at 11:00 would
    overlap it. The GiST exclusion constraint refuses the write, and the service names the conflict.
    """
    site_a, site_b, _ = sites
    existing = TimeEntry(
        employee_id=employee.id,
        site_id=site_b.id,
        work_date=WORK_DATE,
        check_in_at=_at(10),
        check_out_at=_at(12),
        total_minutes=120,
        source=TimeEntrySource.QR_SCAN,
        flags=[],
    )
    scan_session.add(existing)
    scan_session.flush()

    open_entry = TimeEntry(
        employee_id=employee.id,
        site_id=site_a.id,
        work_date=WORK_DATE,
        check_in_at=_at(9),
        source=TimeEntrySource.QR_SCAN,
        flags=[],
    )
    scan_session.add(open_entry)
    scan_session.flush()

    with pytest.raises(scan_service.OverlapRejected) as raised:
        scan_service._check_out(
            scan_session, entry=open_entry, moment=_at(11), context=_context()
        )
    assert raised.value.conflicting_entry is not None
    assert raised.value.conflicting_entry.id == existing.id
