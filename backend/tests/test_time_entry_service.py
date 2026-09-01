"""The time-entry read service behind the hours view (Requirement 2.3, 18.1, 22.3).

The service owns the query: the filters, the site scope, the stable order and the joined labels. The
claims worth a unit test are that each filter narrows the set as asked, that a scope narrows it to the
caller's sites in the query (not after the fetch), that the order is the chronological per-employee
day the view lays out, and that a soft-deleted entry is left out. HTTP wiring and role guards are
pinned in `test_time_entry_api.py`; here the subject is the query.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.core.authz import SiteScope
from app.models.client import Client
from app.models.employee import Employee
from app.models.site import Site
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.services import time_entry as time_entry_service
from app.services.authz import apply_site_scope

_PASSPORT = iter(f"P{n:07d}" for n in range(1, 100000))


def _make_employee(session: Session, *, name: str = "Worker", name_en: str = "Worker") -> Employee:
    passport = next(_PASSPORT)
    employee = Employee(
        full_name=name,
        full_name_en=name_en,
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


def _make_site(session: Session, *, number: str = "S-1", name: str | None = None) -> Site:
    client = Client(name="Acme")
    session.add(client)
    session.flush()
    site = Site(
        name=name or f"Site {number}",
        site_number=number,
        client_id=client.id,
        qr_token=f"placeholder-{uuid.uuid4().hex}",
        qr_token_version=1,
    )
    session.add(site)
    session.flush()
    return site


def _make_entry(
    session: Session,
    *,
    employee: Employee,
    site: Site,
    work_date: date,
    check_in_hour: int = 8,
    total_minutes: int | None = 120,
    source: TimeEntrySource = TimeEntrySource.QR_SCAN,
    is_manual: bool = False,
    status: TimeEntryStatus = TimeEntryStatus.DRAFT,
    flags: list[str] | None = None,
    deleted: bool = False,
) -> TimeEntry:
    check_in = datetime(work_date.year, work_date.month, work_date.day, check_in_hour, tzinfo=UTC)
    check_out = (
        None
        if total_minutes is None
        else datetime(
            work_date.year, work_date.month, work_date.day, check_in_hour + 2, tzinfo=UTC
        )
    )
    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=work_date,
        check_in_at=check_in,
        check_out_at=check_out,
        total_minutes=total_minutes,
        source=source,
        is_manual=is_manual,
        status=status,
        flags=flags or [],
        deleted_at=datetime.now(UTC) if deleted else None,
        delete_reason="test" if deleted else None,
    )
    session.add(entry)
    session.flush()
    return entry


# --------------------------------------------------------------------------- labels and order


def test_a_row_carries_its_employee_and_site_names(session: Session):
    """Requirement 18.1: each row is labelled with the employee and site names for the view."""
    employee = _make_employee(session, name="דנה", name_en="Dana")
    site = _make_site(session, name="North Tower")
    _make_entry(session, employee=employee, site=site, work_date=date(2025, 8, 30))

    page = time_entry_service.list_time_entries(session)

    assert page.total == 1
    row = page.rows[0]
    assert row.employee_name == "דנה"
    assert row.employee_name_en == "Dana"
    assert row.site_name == "North Tower"


def test_entries_come_back_as_a_chronological_per_employee_day(session: Session):
    """Requirement 18.1: ordered by employee, then work date, then check-in time.

    One employee, one day, two sites: a morning entry then an afternoon one. The order lets the view
    render the day chronologically across sites without re-sorting.
    """
    employee = _make_employee(session, name="Aaron", name_en="Aaron")
    site_a = _make_site(session, number="S-A")
    site_b = _make_site(session, number="S-B")
    day = date(2025, 8, 30)

    afternoon = _make_entry(session, employee=employee, site=site_b, work_date=day, check_in_hour=12)
    morning = _make_entry(session, employee=employee, site=site_a, work_date=day, check_in_hour=7)

    page = time_entry_service.list_time_entries(session)

    ids = [row.entry.id for row in page.rows]
    assert ids == [morning.id, afternoon.id]


# --------------------------------------------------------------------------- filters


def test_the_date_range_bounds_work_date_inclusively(session: Session):
    """Requirement 22.3: a date-range filter keeps only entries whose work date is inside it."""
    employee = _make_employee(session)
    site = _make_site(session)
    inside = _make_entry(session, employee=employee, site=site, work_date=date(2025, 8, 15))
    _make_entry(session, employee=employee, site=site, work_date=date(2025, 7, 31))
    _make_entry(session, employee=employee, site=site, work_date=date(2025, 9, 1))

    page = time_entry_service.list_time_entries(
        session,
        filters=time_entry_service.TimeEntryFilters(
            date_from=date(2025, 8, 1), date_to=date(2025, 8, 31)
        ),
    )

    assert [row.entry.id for row in page.rows] == [inside.id]


def test_the_employee_and_site_filters_narrow_the_set(session: Session):
    """Requirement 22.3: filtering by employee and by site each keep only matching entries."""
    alice = _make_employee(session, name="Alice", name_en="Alice")
    bob = _make_employee(session, name="Bob", name_en="Bob")
    site_a = _make_site(session, number="S-A")
    site_b = _make_site(session, number="S-B")
    day = date(2025, 8, 30)

    target = _make_entry(session, employee=alice, site=site_a, work_date=day)
    _make_entry(session, employee=bob, site=site_a, work_date=day)
    _make_entry(session, employee=alice, site=site_b, work_date=day)

    by_employee = time_entry_service.list_time_entries(
        session, filters=time_entry_service.TimeEntryFilters(employee_id=alice.id)
    )
    assert {row.entry.employee_id for row in by_employee.rows} == {alice.id}

    by_both = time_entry_service.list_time_entries(
        session,
        filters=time_entry_service.TimeEntryFilters(employee_id=alice.id, site_id=site_a.id),
    )
    assert [row.entry.id for row in by_both.rows] == [target.id]


def test_the_status_filter_narrows_to_one_status(session: Session):
    """Requirement 22.3: a status filter keeps only entries in that status."""
    employee = _make_employee(session)
    site = _make_site(session)
    approved = _make_entry(
        session, employee=employee, site=site, work_date=date(2025, 8, 1),
        status=TimeEntryStatus.APPROVED,
    )
    _make_entry(
        session, employee=employee, site=site, work_date=date(2025, 8, 2),
        status=TimeEntryStatus.DRAFT,
    )

    page = time_entry_service.list_time_entries(
        session,
        filters=time_entry_service.TimeEntryFilters(status=TimeEntryStatus.APPROVED),
    )
    assert [row.entry.id for row in page.rows] == [approved.id]


def test_the_flag_filter_matches_any_of_the_requested_markers(session: Session):
    """Requirement 22.3: a flag filter keeps entries carrying at least one requested marker."""
    employee = _make_employee(session)
    site = _make_site(session)
    implausible = _make_entry(
        session, employee=employee, site=site, work_date=date(2025, 8, 1),
        flags=["implausible_duration"],
    )
    unassigned = _make_entry(
        session, employee=employee, site=site, work_date=date(2025, 8, 2),
        flags=["unassigned_site"],
    )
    _make_entry(session, employee=employee, site=site, work_date=date(2025, 8, 3), flags=[])

    page = time_entry_service.list_time_entries(
        session,
        filters=time_entry_service.TimeEntryFilters(
            flags=frozenset({"implausible_duration", "unassigned_site"})
        ),
    )
    assert {row.entry.id for row in page.rows} == {implausible.id, unassigned.id}


def test_manual_only_narrows_to_hand_touched_entries(session: Session):
    """Requirement 22.3, 12.4: a manual-only view keeps entries marked manual."""
    employee = _make_employee(session)
    site = _make_site(session)
    manual = _make_entry(
        session, employee=employee, site=site, work_date=date(2025, 8, 1),
        is_manual=True, source=TimeEntrySource.MANUAL,
    )
    _make_entry(session, employee=employee, site=site, work_date=date(2025, 8, 2), is_manual=False)

    page = time_entry_service.list_time_entries(
        session, filters=time_entry_service.TimeEntryFilters(manual_only=True)
    )
    assert [row.entry.id for row in page.rows] == [manual.id]


# --------------------------------------------------------------------------- scope


def test_a_limited_scope_keeps_only_entries_at_the_scoped_sites(session: Session):
    """Requirement 2.3: a site-scoped query returns only entries at the caller's sites."""
    employee = _make_employee(session)
    mine = _make_site(session, number="S-MINE")
    other = _make_site(session, number="S-OTHER")
    day = date(2025, 8, 30)

    at_mine = _make_entry(session, employee=employee, site=mine, work_date=day)
    _make_entry(session, employee=employee, site=other, work_date=day)

    scope = SiteScope.limited_to({mine.id})
    scoped = apply_site_scope(
        time_entry_service.base_select(), TimeEntry.site_id, scope
    )
    page = time_entry_service.list_time_entries(session, scope_statement=scoped)

    assert [row.entry.id for row in page.rows] == [at_mine.id]


def test_an_empty_scope_returns_nothing(session: Session):
    """Requirement 2.3: a manager with no assigned site sees no entry, not every entry."""
    employee = _make_employee(session)
    site = _make_site(session)
    _make_entry(session, employee=employee, site=site, work_date=date(2025, 8, 30))

    scoped = apply_site_scope(
        time_entry_service.base_select(), TimeEntry.site_id, SiteScope.nothing()
    )
    page = time_entry_service.list_time_entries(session, scope_statement=scoped)

    assert page.total == 0
    assert page.rows == []


# --------------------------------------------------------------------------- soft delete


def test_a_soft_deleted_entry_is_excluded(session: Session):
    """Requirement 12.7: a deleted entry is retained for audit but not part of the hours view."""
    employee = _make_employee(session)
    site = _make_site(session)
    kept = _make_entry(session, employee=employee, site=site, work_date=date(2025, 8, 1))
    _make_entry(session, employee=employee, site=site, work_date=date(2025, 8, 2), deleted=True)

    page = time_entry_service.list_time_entries(session)
    assert [row.entry.id for row in page.rows] == [kept.id]
