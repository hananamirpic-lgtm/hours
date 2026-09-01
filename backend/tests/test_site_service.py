"""Site service rules that do not need HTTP (Requirement 6, 7).

The claims here are the ones the requirement pins to the service: that a site number is unique and
the conflict is named, that the billing-rate history is a non-overlapping chain resolved per date so
a past period keeps the rate it was billed at (6.6), that assigning a manager writes the `user_sites`
grant the authorization layer reads (6.7, 2.3), and that employee assignment is a many-to-many that
records expectation only and never touches where time can be recorded (7.1, 7.2).

These run against the in-memory SQLite session the suite provides. The database's unique constraint
on `site_number` and its GiST exclusion constraint on the rate periods are the backstops; what is
tested here is the application enforcement that must hold on both engines.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.change_log import ChangeLog
from app.models.client import Client
from app.models.employee import Employee
from app.models.site import EmployeeSite
from app.models.user import User, UserRole
from app.models.user_site import UserSite
from app.schemas.site import SiteCreate, SiteRateInput, SiteUpdate
from app.services import site as site_service
from app.services.audit import AuditContext


def _context() -> AuditContext:
    return AuditContext(actor_user_id=uuid.uuid4(), reason=None)


def _rate(amount: str, start: date, end: date | None = None) -> SiteRateInput:
    return SiteRateInput(billing_rate=Decimal(amount), effective_from=start, effective_to=end)


def _make_client(session: Session, name: str = "Acme") -> Client:
    client = Client(name=name)
    session.add(client)
    session.commit()
    return client


def _make_manager(session: Session) -> User:
    user = User(
        username=f"mgr-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role=UserRole.SITE_MANAGER,
    )
    session.add(user)
    session.commit()
    return user


def _make_employee(session: Session, passport: str = "P0000001") -> Employee:
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
    session.commit()
    return employee


def _create_payload(client_id: uuid.UUID, *, number: str = "S-1", **overrides) -> SiteCreate:
    fields: dict[str, object] = {"name": f"Site {number}", "site_number": number, "client_id": client_id}
    fields.update(overrides)
    return SiteCreate(**fields)


def _make_site(session: Session, client_id: uuid.UUID, **overrides):
    site = site_service.create_site(session, _create_payload(client_id, **overrides), context=_context())
    session.commit()
    return site


# --------------------------------------------------------------------------- site-number uniqueness


def test_a_duplicate_site_number_is_rejected_and_names_the_conflict(session: Session):
    """Requirement 6.3."""
    client = _make_client(session)
    first = _make_site(session, client.id, number="S-1")

    with pytest.raises(site_service.DuplicateSiteNumber) as raised:
        site_service.create_site(
            session, _create_payload(client.id, number="S-1"), context=_context()
        )
    assert raised.value.conflicting.id == first.id


def test_updating_a_site_number_to_one_in_use_is_rejected(session: Session):
    client = _make_client(session)
    first = _make_site(session, client.id, number="S-1")
    second = _make_site(session, client.id, number="S-2")

    with pytest.raises(site_service.DuplicateSiteNumber) as raised:
        site_service.update_site(session, second.id, SiteUpdate(site_number="S-1"), context=_context())
    assert raised.value.conflicting.id == first.id


def test_no_location_fields_exist_on_the_create_schema(session: Session):
    """Requirement 6.8: a request carrying a coordinate is rejected as an unknown field."""
    client = _make_client(session)
    with pytest.raises(Exception):  # noqa: B017,PT011 - pydantic ValidationError on extra field
        SiteCreate(name="Site", site_number="S-9", client_id=client.id, latitude=32.0)


# --------------------------------------------------------------------------- rate history and resolution


def test_the_rate_resolver_picks_the_row_covering_a_date(session: Session):
    """Requirement 17.1: the billing rate in force on a date is the row whose period covers it."""
    client = _make_client(session)
    site = _make_site(session, client.id)
    site_service.replace_rate_history(
        session,
        site.id,
        [
            _rate("60", date(2025, 8, 1), date(2025, 8, 15)),
            _rate("75", date(2025, 8, 16)),
        ],
        context=_context(),
    )
    session.commit()

    early = site_service.resolve_rate(site, date(2025, 8, 10))
    late = site_service.resolve_rate(site, date(2025, 8, 20))
    assert early is not None and early.billing_rate == Decimal("60")
    assert late is not None and late.billing_rate == Decimal("75")


def test_overlapping_rate_periods_are_rejected(session: Session):
    """Requirement 6.4, 6.6: the chain must not overlap."""
    client = _make_client(session)
    site = _make_site(session, client.id)
    with pytest.raises(site_service.OverlappingRates):
        site_service.replace_rate_history(
            session,
            site.id,
            [
                _rate("60", date(2025, 8, 1), date(2025, 8, 20)),
                _rate("75", date(2025, 8, 15)),
            ],
            context=_context(),
        )


def test_rate_history_is_preserved_so_a_past_period_does_not_change(session: Session):
    """Requirement 6.6: adding a newer rate must not change what an already-billed past period was
    worth.

    A site bills at 60 through August. September opens a new rate of 75. The resolver for an August
    date must still return 60 — the past row is kept as it was, a new one is chained after it, and
    nothing rewrites history in place.
    """
    client = _make_client(session)
    site = _make_site(session, client.id)

    # The rate as it stood when August was billed: 60, open-ended.
    site_service.replace_rate_history(
        session,
        site.id,
        [_rate("60", date(2025, 8, 1))],
        context=_context(),
    )
    session.commit()
    august_before = site_service.resolve_rate(site, date(2025, 8, 20))
    assert august_before is not None and august_before.billing_rate == Decimal("60")

    # Later, a rate change from September: close August's row at 31 Aug, open 75 from 1 Sep.
    site_service.replace_rate_history(
        session,
        site.id,
        [
            _rate("60", date(2025, 8, 1), date(2025, 8, 31)),
            _rate("75", date(2025, 9, 1)),
        ],
        context=_context(),
    )
    session.commit()

    august_after = site_service.resolve_rate(site, date(2025, 8, 20))
    september = site_service.resolve_rate(site, date(2025, 9, 10))
    # The August billing is unchanged; only September sees the new rate.
    assert august_after is not None and august_after.billing_rate == Decimal("60")
    assert september is not None and september.billing_rate == Decimal("75")


def test_overtime_billing_rate_is_optional(session: Session):
    """Requirement 17.2: a rate with no overtime rate configured is valid."""
    client = _make_client(session)
    site = _make_site(session, client.id)
    site_service.replace_rate_history(
        session,
        site.id,
        [_rate("60", date(2025, 1, 1))],
        context=_context(),
    )
    session.commit()
    current = site_service.resolve_rate(site, date(2025, 6, 1))
    assert current is not None
    assert current.overtime_billing_rate is None


# --------------------------------------------------------------------------- manager grant (user_sites)


def test_assigning_a_manager_writes_the_user_sites_grant(session: Session):
    """Requirement 2.3, 6.7: a manager put on a site can see it, because the grant is written."""
    client = _make_client(session)
    manager = _make_manager(session)
    site = _make_site(session, client.id, manager_user_id=manager.id)

    grant = session.get(UserSite, {"user_id": manager.id, "site_id": site.id})
    assert grant is not None


def test_changing_the_manager_moves_the_grant(session: Session):
    client = _make_client(session)
    first = _make_manager(session)
    second = _make_manager(session)
    site = _make_site(session, client.id, manager_user_id=first.id)

    site_service.update_site(session, site.id, SiteUpdate(manager_user_id=second.id), context=_context())
    session.commit()

    assert session.get(UserSite, {"user_id": first.id, "site_id": site.id}) is None
    assert session.get(UserSite, {"user_id": second.id, "site_id": site.id}) is not None


def test_clearing_the_manager_revokes_the_grant(session: Session):
    client = _make_client(session)
    manager = _make_manager(session)
    site = _make_site(session, client.id, manager_user_id=manager.id)

    site_service.update_site(session, site.id, SiteUpdate(manager_user_id=None), context=_context())
    session.commit()

    assert session.get(UserSite, {"user_id": manager.id, "site_id": site.id}) is None


# --------------------------------------------------------------------------- employee assignment


def test_replace_site_employees_maintains_the_many_to_many(session: Session):
    """Requirement 7.1."""
    client = _make_client(session)
    site = _make_site(session, client.id)
    alice = _make_employee(session, passport="P0000001")
    bob = _make_employee(session, passport="P0000002")

    site_service.replace_site_employees(session, site.id, [alice.id, bob.id], context=_context())
    session.commit()

    assert set(site_service.assigned_employee_ids(session, site.id)) == {alice.id, bob.id}

    # Replacing the set removes those not named.
    site_service.replace_site_employees(session, site.id, [alice.id], context=_context())
    session.commit()
    assert site_service.assigned_employee_ids(session, site.id) == [alice.id]


def test_replace_employee_sites_maintains_the_same_table_from_the_other_side(session: Session):
    """Requirement 7.1: assigning from the employee side writes the same `employee_sites` rows."""
    client = _make_client(session)
    site_a = _make_site(session, client.id, number="S-1")
    site_b = _make_site(session, client.id, number="S-2")
    employee = _make_employee(session)

    site_service.replace_employee_sites(session, employee.id, [site_a.id, site_b.id], context=_context())
    session.commit()

    assert set(site_service.assigned_site_ids(session, employee.id)) == {site_a.id, site_b.id}
    # And the site side sees the same fact.
    assert employee.id in set(site_service.assigned_employee_ids(session, site_a.id))


def test_assignment_does_not_restrict_where_time_can_be_recorded(session: Session):
    """Requirement 7.2: an assignment is an expectation only.

    The assignment table is `employee_sites`; the ability to record time is governed by the site's
    `assignment_mode` and the scan path, never by the presence or absence of an assignment row. The
    check here is structural: assigning an employee to one site leaves no row that would forbid a
    time entry at another, and a time entry can be written at an unassigned site with nothing in the
    schema objecting.
    """
    from datetime import UTC, datetime

    from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus

    client = _make_client(session)
    assigned = _make_site(session, client.id, number="S-1")
    other = _make_site(session, client.id, number="S-2")
    employee = _make_employee(session)

    site_service.replace_employee_sites(session, employee.id, [assigned.id], context=_context())
    session.commit()

    # A time entry at the *unassigned* site is accepted by the schema: assignment does not gate it.
    entry = TimeEntry(
        employee_id=employee.id,
        site_id=other.id,
        work_date=date(2025, 8, 1),
        check_in_at=datetime(2025, 8, 1, 5, 0, tzinfo=UTC),
        source=TimeEntrySource.QR_SCAN,
        status=TimeEntryStatus.DRAFT,
        flags=[],
    )
    session.add(entry)
    session.commit()

    assert session.get(TimeEntry, entry.id) is not None
    # The assignment row exists only for the assigned site, not the one time was recorded at.
    rows = list(session.scalars(select(EmployeeSite).where(EmployeeSite.employee_id == employee.id)))
    assert [row.site_id for row in rows] == [assigned.id]


def test_assigning_an_unknown_employee_is_rejected(session: Session):
    client = _make_client(session)
    site = _make_site(session, client.id)
    with pytest.raises(site_service.EmployeeNotFound):
        site_service.replace_site_employees(session, site.id, [uuid.uuid4()], context=_context())


# --------------------------------------------------------------------------- audit


def test_creation_writes_an_audit_row_per_field(session: Session):
    """Requirement 13: every field set at creation is audited."""
    client = _make_client(session)
    site = _make_site(session, client.id)

    rows = list(
        session.scalars(
            select(ChangeLog).where(ChangeLog.entity_type == "sites", ChangeLog.entity_id == site.id)
        )
    )
    fields = {row.field for row in rows}
    assert "name" in fields
    assert "site_number" in fields
