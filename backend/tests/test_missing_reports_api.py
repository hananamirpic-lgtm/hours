"""Missing-report detection over HTTP and through the service (Requirement 14.3, 22.3).

The system detects missing time reports of three kinds for the days an employee was expected at a
site (Requirement 14.3):

* **missing check-out** — an open entry (`check_out_at IS NULL`): arrived, never left;
* **missing check-in** — a manual or system marker with no check-in behind it, carried by the
  `missing_check_in` flag: a departure with no arrival;
* **both missing** — expected at a site but recorded nothing at all.

A complete day — a whole, closed, unflagged entry — produces no finding. Expectation comes from the
`employee_sites` assignment covering the date. The endpoint is scoped by role (Requirement 22.3): a
site manager sees only their sites; admin and accounting see all.

The detection lives in the reports service so Task 30's notifications reuse it, so a few tests call
the service directly to exercise the classification, and the rest go over HTTP to exercise the route,
the scope and the response shape. The sign-in helper and the make-* helpers mirror
`test_reports_api.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.core.authz import SiteScope
from app.models.client import Client
from app.models.employee import Employee
from app.models.site import EmployeeSite, Site
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.models.user import UserRole
from app.models.user_site import UserSite
from app.services import reports as reports_service
from auth_support import DEFAULT_PASSWORD

# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def reports_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(reports_client, make_user):
    def _sign_in(role: UserRole, **user_overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **user_overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **user_overrides)
        payload["username"] = user.username

        response = reports_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        header = {"Authorization": f"Bearer {response.json()['access_token']}"}
        return header, user

    return _sign_in


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


_PASSPORT = iter(f"M{n:07d}" for n in range(1, 1_000_000))


def _make_employee(session: Session, *, name: str = "Worker") -> Employee:
    passport = next(_PASSPORT)
    employee = Employee(
        full_name=name,
        full_name_en=name,
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


def _make_client(session: Session, *, name: str = "Acme") -> Client:
    client = Client(name=name)
    session.add(client)
    session.commit()
    return client


def _make_site(session: Session, *, client: Client, number: str) -> Site:
    site = Site(
        name=f"Site {number}",
        site_number=number,
        client_id=client.id,
        qr_token=f"placeholder-{uuid.uuid4().hex}",
        qr_token_version=1,
    )
    session.add(site)
    session.commit()
    return site


def _assign_site(
    session: Session,
    *,
    employee: Employee,
    site: Site,
    assigned_from: date = date(2025, 8, 1),
    assigned_to: date | None = None,
) -> None:
    """Record that `employee` is expected at `site` over a period (`employee_sites`)."""
    session.add(
        EmployeeSite(
            employee_id=employee.id,
            site_id=site.id,
            assigned_from=assigned_from,
            assigned_to=assigned_to,
        )
    )
    session.commit()


def _assign_day(session: Session, *, employee: Employee, site: Site, day: int) -> None:
    """Expect `employee` at `site` on exactly one August 2025 day."""
    _assign_site(
        session,
        employee=employee,
        site=site,
        assigned_from=date(2025, 8, day),
        assigned_to=date(2025, 8, day),
    )


def _assign_manager(session: Session, user, site: Site) -> None:
    session.add(UserSite(user_id=user.id, site_id=site.id))
    session.commit()


def _complete_entry(
    session: Session, *, employee: Employee, site: Site, day: int, minutes: int = 480
) -> TimeEntry:
    """A whole day: check-in and check-out both recorded, no flag. Produces no finding."""
    local_in = datetime(2025, 8, day, 8, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
    check_in = local_in.astimezone(UTC)
    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=date(2025, 8, day),
        check_in_at=check_in,
        check_out_at=check_in + timedelta(minutes=minutes),
        total_minutes=minutes,
        source=TimeEntrySource.QR_SCAN,
        is_manual=False,
        status=TimeEntryStatus.APPROVED,
        flags=[],
    )
    session.add(entry)
    session.commit()
    return entry


def _open_entry(session: Session, *, employee: Employee, site: Site, day: int) -> TimeEntry:
    """An arrival with no departure: `check_out_at IS NULL`. Missing check-out."""
    local_in = datetime(2025, 8, day, 8, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=date(2025, 8, day),
        check_in_at=local_in.astimezone(UTC),
        check_out_at=None,
        total_minutes=None,
        source=TimeEntrySource.QR_SCAN,
        is_manual=False,
        status=TimeEntryStatus.DRAFT,
        flags=[],
    )
    session.add(entry)
    session.commit()
    return entry


def _missing_checkin_marker(
    session: Session, *, employee: Employee, site: Site, day: int
) -> TimeEntry:
    """A manual/system marker standing for a departure with no check-in behind it.

    The schema requires `check_in_at`, so a departure-without-arrival cannot be a normal completed
    entry; it is recorded as a manual marker carrying the `missing_check_in` flag (Requirement 14.3).
    """
    local = datetime(2025, 8, day, 17, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
    check_in = local.astimezone(UTC)
    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=date(2025, 8, day),
        check_in_at=check_in,
        check_out_at=check_in + timedelta(minutes=1),
        total_minutes=1,
        source=TimeEntrySource.MANUAL,
        is_manual=True,
        manual_reason="check-out recorded with no check-in",
        status=TimeEntryStatus.DRAFT,
        flags=[reports_service.FLAG_MISSING_CHECK_IN],
    )
    session.add(entry)
    session.commit()
    return entry


# ===================================================================== the three kinds (14.3)


def test_missing_checkout_detected(reports_client, sign_in, session: Session):
    """Requirement 14.3: an open entry on an expected day is a missing-check-out finding."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session, client=_make_client(session), number="A")
    _assign_site(session, employee=employee, site=site)
    _open_entry(session, employee=employee, site=site, day=4)

    response = reports_client.get(
        "/api/reports/missing-reports?date_from=2025-08-04&date_to=2025-08-04", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    finding = body["findings"][0]
    assert finding["kind"] == "missing_checkout"
    assert finding["employee_id"] == str(employee.id)
    assert finding["site_id"] == str(site.id)
    assert finding["work_date"] == "2025-08-04"


def test_missing_checkin_detected(reports_client, sign_in, session: Session):
    """Requirement 14.3: a missing-check-in marker on an expected day is a missing-check-in finding."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session, client=_make_client(session), number="A")
    _assign_site(session, employee=employee, site=site)
    _missing_checkin_marker(session, employee=employee, site=site, day=4)

    response = reports_client.get(
        "/api/reports/missing-reports?date_from=2025-08-04&date_to=2025-08-04", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["findings"][0]["kind"] == "missing_checkin"


def test_both_missing_detected(reports_client, sign_in, session: Session):
    """Requirement 14.3: an expected day with no entry at all is a both-missing finding."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session, client=_make_client(session), number="A")
    _assign_day(session, employee=employee, site=site, day=4)

    response = reports_client.get(
        "/api/reports/missing-reports?date_from=2025-08-04&date_to=2025-08-04", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    finding = body["findings"][0]
    assert finding["kind"] == "both_missing"
    assert finding["employee_id"] == str(employee.id)
    assert finding["site_id"] == str(site.id)


def test_complete_day_produces_no_finding(reports_client, sign_in, session: Session):
    """Requirement 14.3: a whole day — a closed, unflagged entry — produces no finding."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session, client=_make_client(session), number="A")
    _assign_day(session, employee=employee, site=site, day=4)
    _complete_entry(session, employee=employee, site=site, day=4)

    response = reports_client.get(
        "/api/reports/missing-reports?date_from=2025-08-04&date_to=2025-08-04", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 0
    assert body["findings"] == []


def test_all_three_kinds_over_a_range(reports_client, sign_in, session: Session):
    """Requirement 14.3: across a range, each of the three kinds is detected on its own day.

    One employee expected 4–6 August: an open entry on the 4th (missing check-out), a marker on the
    5th (missing check-in), nothing on the 6th (both missing). A complete day would add nothing.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session, client=_make_client(session), number="A")
    _assign_site(
        session, employee=employee, site=site,
        assigned_from=date(2025, 8, 4), assigned_to=date(2025, 8, 6),
    )
    _open_entry(session, employee=employee, site=site, day=4)
    _missing_checkin_marker(session, employee=employee, site=site, day=5)
    # 6th: expected, nothing recorded.

    response = reports_client.get(
        "/api/reports/missing-reports?date_from=2025-08-04&date_to=2025-08-06", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    kinds = {(f["work_date"], f["kind"]) for f in body["findings"]}
    assert ("2025-08-04", "missing_checkout") in kinds
    assert ("2025-08-05", "missing_checkin") in kinds
    assert ("2025-08-06", "both_missing") in kinds
    assert body["total"] == 3


# ===================================================================== envelope (22.3)


def test_response_states_range_and_filters(reports_client, sign_in, session: Session):
    """The response echoes the range and filters so it is self-describing (Requirement 22.3)."""
    headers, _ = sign_in(UserRole.ADMIN)
    response = reports_client.get(
        "/api/reports/missing-reports?date_from=2025-08-01&date_to=2025-08-31", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["date_from"] == "2025-08-01"
    assert body["date_to"] == "2025-08-31"
    assert body["employee_id"] is None
    assert body["site_id"] is None


# ===================================================================== scope by role (22.3, 2.3)


def test_site_manager_sees_only_their_sites(reports_client, sign_in, session: Session):
    """Requirement 22.3, 2.3: a site manager's missing reports are limited to their assigned sites.

    An employee is expected at two sites and records nothing at either. The manager, assigned Site A
    only, sees the Site A finding but not the Site B one.
    """
    admin_headers, _ = sign_in(UserRole.ADMIN)
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)
    client = _make_client(session)
    site_a = _make_site(session, client=client, number="A")
    site_b = _make_site(session, client=client, number="B")
    _assign_day(session, employee=employee, site=site_a, day=4)
    _assign_day(session, employee=employee, site=site_b, day=4)
    _assign_manager(session, manager, site_a)

    manager_body = reports_client.get(
        "/api/reports/missing-reports?date_from=2025-08-04&date_to=2025-08-04",
        headers=manager_headers,
    ).json()
    manager_sites = {f["site_id"] for f in manager_body["findings"]}
    assert manager_sites == {str(site_a.id)}

    admin_body = reports_client.get(
        "/api/reports/missing-reports?date_from=2025-08-04&date_to=2025-08-04",
        headers=admin_headers,
    ).json()
    admin_sites = {f["site_id"] for f in admin_body["findings"]}
    assert admin_sites == {str(site_a.id), str(site_b.id)}


def test_accounting_sees_every_site(reports_client, sign_in, session: Session):
    """Requirement 2.6: accounting reads the whole business, so every site's missing reports."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    accounting_headers, _ = sign_in(UserRole.ACCOUNTING)
    employee = _make_employee(session)
    site = _make_site(session, client=_make_client(session), number="A")
    _assign_day(session, employee=employee, site=site, day=4)

    body = reports_client.get(
        "/api/reports/missing-reports?date_from=2025-08-04&date_to=2025-08-04",
        headers=accounting_headers,
    ).json()
    assert body["total"] == 1


def test_employee_role_cannot_read(reports_client, sign_in, session: Session):
    """Requirement 2.7: an employee has no business on the missing-reports list."""
    employee_headers, _ = sign_in(UserRole.EMPLOYEE)
    response = reports_client.get(
        "/api/reports/missing-reports?date_from=2025-08-04&date_to=2025-08-04",
        headers=employee_headers,
    )
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_manager_with_no_assignment_sees_nothing(reports_client, sign_in, session: Session):
    """An empty scope means nothing, not everything (SiteScope invariant)."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)
    site = _make_site(session, client=_make_client(session), number="A")
    _assign_day(session, employee=employee, site=site, day=4)

    body = reports_client.get(
        "/api/reports/missing-reports?date_from=2025-08-04&date_to=2025-08-04",
        headers=manager_headers,
    ).json()
    assert body["total"] == 0


# ===================================================================== service directly


def test_service_classifies_the_three_kinds(session: Session):
    """The detection is in the service so Task 30 can reuse it — exercise it directly (14.3)."""
    employee = _make_employee(session)
    site = _make_site(session, client=_make_client(session), number="A")
    _assign_site(
        session, employee=employee, site=site,
        assigned_from=date(2025, 8, 4), assigned_to=date(2025, 8, 6),
    )
    _open_entry(session, employee=employee, site=site, day=4)
    _missing_checkin_marker(session, employee=employee, site=site, day=5)
    # 6th: nothing.

    findings = reports_service.detect_missing_reports(
        session,
        date_from=date(2025, 8, 4),
        date_to=date(2025, 8, 6),
        scope=SiteScope.all_sites(),
    )
    by_day = {(f.work_date, f.kind) for f in findings}
    assert (date(2025, 8, 4), reports_service.MissingReportKind.MISSING_CHECKOUT) in by_day
    assert (date(2025, 8, 5), reports_service.MissingReportKind.MISSING_CHECKIN) in by_day
    assert (date(2025, 8, 6), reports_service.MissingReportKind.BOTH_MISSING) in by_day


def test_service_complete_day_no_finding(session: Session):
    """A whole day produces no finding, through the service (Requirement 14.3)."""
    employee = _make_employee(session)
    site = _make_site(session, client=_make_client(session), number="A")
    _assign_day(session, employee=employee, site=site, day=4)
    _complete_entry(session, employee=employee, site=site, day=4)

    findings = reports_service.detect_missing_reports(
        session,
        date_from=date(2025, 8, 4),
        date_to=date(2025, 8, 4),
        scope=SiteScope.all_sites(),
    )
    assert findings == []
