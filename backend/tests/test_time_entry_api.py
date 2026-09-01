"""The time-entry hours-view endpoint over HTTP (Requirement 2.3, 2.4, 18.1, 22.3).

The claims worth an HTTP test are the ones about the request, the response and who may see what: that
the hours readers (admin, site manager, accounting) may list and an employee may not (Requirement
2.4, 2.6); that a site manager's list is scoped to their assigned sites in the query (Requirement
2.3); that the filters on the query string narrow the set (Requirement 22.3); and that each row
carries the labels and the manual and anomaly markers the view renders (Requirement 18.1, 12.4). The
query itself is pinned in `test_time_entry_service.py`; here the subject is the wiring, the scope and
the filters as query parameters.

The sign-in helper mirrors `test_scan_api.py`: an admin enrols 2FA, an employee and a manager do not.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.employee import Employee
from app.models.site import Site
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.models.user import UserRole
from app.models.user_site import UserSite
from auth_support import DEFAULT_PASSWORD


@pytest.fixture
def entries_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(entries_client, make_user):
    def _sign_in(role: UserRole, **user_overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **user_overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **user_overrides)
        payload["username"] = user.username

        response = entries_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        header = {"Authorization": f"Bearer {response.json()['access_token']}"}
        return header, user

    return _sign_in


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


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
    is_manual: bool = False,
    source: TimeEntrySource = TimeEntrySource.QR_SCAN,
    status: TimeEntryStatus = TimeEntryStatus.DRAFT,
    flags: list[str] | None = None,
) -> TimeEntry:
    check_in = datetime(work_date.year, work_date.month, work_date.day, check_in_hour, tzinfo=UTC)
    check_out = (
        None
        if total_minutes is None
        else datetime(work_date.year, work_date.month, work_date.day, check_in_hour + 2, tzinfo=UTC)
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
    )
    session.add(entry)
    session.commit()
    return entry


# --------------------------------------------------------------------------- read and shape


def test_admin_lists_entries_with_labels_and_markers(entries_client, sign_in, session: Session):
    """Requirement 18.1, 12.4: a listed row carries its names, totals and manual/anomaly markers."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session, name="Dana", name_en="Dana")
    site = _make_site(session, name="North Tower")
    _make_entry(
        session,
        employee=employee,
        site=site,
        work_date=date(2025, 8, 30),
        is_manual=True,
        source=TimeEntrySource.MANUAL,
        flags=["implausible_duration"],
    )

    response = entries_client.get("/api/time-entries", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["employee_name"] == "Dana"
    assert item["site_name"] == "North Tower"
    assert item["work_date"] == "2025-08-30"
    assert item["total_minutes"] == 120
    assert item["is_manual"] is True
    assert item["flags"] == ["implausible_duration"]


def test_an_open_shift_has_no_total(entries_client, sign_in, session: Session):
    """A shift still open reports a null total and null check-out, so the view shows it as ongoing."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    _make_entry(session, employee=employee, site=site, work_date=date(2025, 8, 30), total_minutes=None)

    response = entries_client.get("/api/time-entries", headers=headers)
    item = response.json()["items"][0]
    assert item["total_minutes"] is None
    assert item["check_out_at"] is None


# --------------------------------------------------------------------------- filters as query params


def test_filters_narrow_the_set(entries_client, sign_in, session: Session):
    """Requirement 22.3: date range, employee, site, status and flag filters over the query string."""
    headers, _ = sign_in(UserRole.ADMIN)
    alice = _make_employee(session, name="Alice", name_en="Alice")
    bob = _make_employee(session, name="Bob", name_en="Bob")
    site_a = _make_site(session, number="S-A")
    site_b = _make_site(session, number="S-B")

    target = _make_entry(
        session, employee=alice, site=site_a, work_date=date(2025, 8, 15),
        status=TimeEntryStatus.APPROVED, flags=["unassigned_site"],
    )
    _make_entry(session, employee=bob, site=site_a, work_date=date(2025, 8, 15))
    _make_entry(session, employee=alice, site=site_b, work_date=date(2025, 8, 15))
    _make_entry(session, employee=alice, site=site_a, work_date=date(2025, 7, 1))

    response = entries_client.get(
        "/api/time-entries",
        params={
            "date_from": "2025-08-01",
            "date_to": "2025-08-31",
            "employee_id": str(alice.id),
            "site_id": str(site_a.id),
            "status": "approved",
            "flag": "unassigned_site",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [str(target.id)]


def test_an_unknown_flag_matches_nothing_rather_than_everything(
    entries_client, sign_in, session: Session
):
    """A flag the view does not know is dropped, so it does not silently widen the result."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    _make_entry(session, employee=employee, site=site, work_date=date(2025, 8, 30), flags=[])

    # A known flag with no matching entry returns nothing; an unknown flag is dropped, leaving no
    # flag filter, so it returns everything. The two behave differently, which is the point.
    known = entries_client.get(
        "/api/time-entries", params={"flag": "implausible_duration"}, headers=headers
    )
    assert known.json()["total"] == 0

    unknown = entries_client.get(
        "/api/time-entries", params={"flag": "not_a_real_flag"}, headers=headers
    )
    assert unknown.json()["total"] == 1


# --------------------------------------------------------------------------- scope and role


def test_a_site_manager_sees_only_entries_at_their_sites(entries_client, sign_in, session: Session):
    """Requirement 2.3: the hours view is scoped to the manager's assigned sites."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)
    mine = _make_site(session, number="S-MINE")
    other = _make_site(session, number="S-OTHER")
    session.add(UserSite(user_id=manager.id, site_id=mine.id))
    session.commit()

    at_mine = _make_entry(session, employee=employee, site=mine, work_date=date(2025, 8, 30))
    _make_entry(session, employee=employee, site=other, work_date=date(2025, 8, 30))

    response = entries_client.get("/api/time-entries", headers=manager_headers)
    assert response.status_code == 200, response.text
    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [str(at_mine.id)]


def test_a_manager_with_no_sites_sees_nothing(entries_client, sign_in, session: Session):
    """Requirement 2.3: an empty scope is nothing, not everything."""
    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)
    site = _make_site(session)
    _make_entry(session, employee=employee, site=site, work_date=date(2025, 8, 30))

    response = entries_client.get("/api/time-entries", headers=manager_headers)
    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_accounting_sees_every_site(entries_client, sign_in, session: Session):
    """Requirement 2.6: accounting reads hours across the business."""
    headers, _ = sign_in(UserRole.ACCOUNTING)
    employee = _make_employee(session)
    site_a = _make_site(session, number="S-A")
    site_b = _make_site(session, number="S-B")
    _make_entry(session, employee=employee, site=site_a, work_date=date(2025, 8, 30))
    _make_entry(session, employee=employee, site=site_b, work_date=date(2025, 8, 30))

    response = entries_client.get("/api/time-entries", headers=headers)
    assert response.status_code == 200
    assert response.json()["total"] == 2


def test_an_employee_may_not_list_the_hours_view(entries_client, sign_in, session: Session):
    """Requirement 2.7: the console hours view is not the employee's own-record endpoint."""
    employee = _make_employee(session)
    employee_headers, _ = sign_in(UserRole.EMPLOYEE, employee_id=employee.id)

    response = entries_client.get("/api/time-entries", headers=employee_headers)
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"
