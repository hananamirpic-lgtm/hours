"""Scan endpoints over HTTP (Requirement 9, 7.3, 7.4, 23.1).

The claims worth an HTTP test are the ones about the request, the response and the state machine: a
check-in creates an entry stamped with the server time (Requirement 9.1, 9.2); a second identical
scan inside the window is ignored rather than opening a second shift (Requirement 9.3); an inactive
employee and an inactive site are both refused (Requirement 9.4); a locked period is refused
(Requirement 9.5); a request carrying a location field is rejected as an unknown field (Requirement
9.6); and the status endpoint reflects the open shift (Requirement 23.1). The deeper branching is
pinned in `test_scan_service.py`; here the subject is the wiring.

The sign-in helper mirrors the one in `test_site_api.py`. The employee role does not require 2FA, so
an employee login needs no TOTP; an administrator does, and the helper enrols one so an admin token
can seed data through the API where convenient.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.qr_token import mint
from app.models.client import Client
from app.models.employee import Employee, EmployeeStatus
from app.models.period_lock import PeriodLock
from app.models.site import AssignmentMode, Site, SiteStatus
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.models.user import UserRole
from auth_support import DEFAULT_PASSWORD


@pytest.fixture
def scans_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(scans_client, make_user):
    """Sign a user in and return (auth header, user). An admin gets a 2FA secret; an employee does not."""

    def _sign_in(role: UserRole, **user_overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **user_overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **user_overrides)
        payload["username"] = user.username

        response = scans_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        header = {"Authorization": f"Bearer {response.json()['access_token']}"}
        return header, user

    return _sign_in


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


# --------------------------------------------------------------------------- data helpers


_PASSPORT = iter(f"P{n:07d}" for n in range(1, 100000))


def _make_employee(session: Session, *, status: EmployeeStatus = EmployeeStatus.ACTIVE) -> Employee:
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
        status=status,
    )
    session.add(employee)
    session.commit()
    return employee


def _make_site(
    session: Session,
    *,
    number: str = "S-1",
    status: SiteStatus = SiteStatus.ACTIVE,
    assignment_mode: AssignmentMode = AssignmentMode.OPEN,
    version: int = 1,
) -> Site:
    client = Client(name="Acme")
    session.add(client)
    session.flush()
    site = Site(
        name=f"Site {number}",
        site_number=number,
        client_id=client.id,
        status=status,
        assignment_mode=assignment_mode,
        qr_token=f"placeholder-{uuid.uuid4().hex}",
        qr_token_version=version,
    )
    session.add(site)
    session.commit()
    return site


def _employee_login(sign_in, session: Session, employee: Employee):
    """An employee-role login linked to `employee`, and its auth header."""
    return sign_in(UserRole.EMPLOYEE, employee_id=employee.id)


def _token_for(site: Site) -> str:
    return mint(site.id, site.qr_token_version)


# --------------------------------------------------------------------------- check-in


def test_check_in_creates_an_entry_with_the_server_time(scans_client, sign_in, session: Session):
    """Requirement 9.1, 9.2: a valid scan with no open shift creates an entry stamped server-side."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    before = datetime.now(UTC)
    response = scans_client.post(
        "/api/scans", json={"qr_token": _token_for(site)}, headers=headers
    )
    after = datetime.now(UTC)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "check_in"
    assert body["site_id"] == str(site.id)
    assert body["is_open"] is True

    entry = session.scalars(select_time_entries(employee.id)).one()
    assert entry.check_out_at is None
    # The recorded time is the server's, inside the window the request was handled in. SQLite hands
    # the timestamp back naive, so compare in UTC by normalising to an aware value.
    recorded = entry.check_in_at
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=UTC)
    assert before <= recorded <= after


def select_time_entries(employee_id: uuid.UUID):
    return select(TimeEntry).where(TimeEntry.employee_id == employee_id)


def test_a_location_field_is_rejected_as_unknown(scans_client, sign_in, session: Session):
    """Requirement 9.6: no location field exists, so one sent is a 422 unknown field."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    response = scans_client.post(
        "/api/scans",
        json={"qr_token": _token_for(site), "latitude": 32.1, "longitude": 34.8},
        headers=headers,
    )
    assert response.status_code == 422


def test_a_second_identical_scan_within_the_window_is_ignored(scans_client, sign_in, session: Session):
    """Requirement 9.3: a duplicate submission returns the existing entry, not a second one."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)
    token = _token_for(site)

    first = scans_client.post("/api/scans", json={"qr_token": token}, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["action"] == "check_in"

    second = scans_client.post("/api/scans", json={"qr_token": token}, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["action"] == "duplicate_ignored"
    # The same entry, and only one exists.
    assert second.json()["time_entry_id"] == first.json()["time_entry_id"]
    entries = list(session.scalars(select_time_entries(employee.id)))
    assert len(entries) == 1


# --------------------------------------------------------------------------- check-out


def test_a_same_site_rescan_checks_out(scans_client, sign_in, session: Session):
    """Requirement 10.1: scanning again at the site of the open shift closes it with a total."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)
    token = _token_for(site)

    first = scans_client.post("/api/scans", json={"qr_token": token}, headers=headers)
    assert first.json()["action"] == "check_in"

    # Age the open entry so the second scan is outside the duplicate window rather than ignored.
    entry = session.scalars(select_time_entries(employee.id)).one()
    entry.check_in_at = datetime.now(UTC) - timedelta(hours=1)
    session.commit()

    out = scans_client.post("/api/scans", json={"qr_token": token}, headers=headers)
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["action"] == "check_out"
    assert body["is_open"] is False

    session.expire_all()
    closed = session.scalars(select_time_entries(employee.id)).one()
    assert closed.check_out_at is not None
    assert closed.total_minutes is not None and closed.total_minutes >= 59


def test_the_explicit_checkout_endpoint_closes_the_open_shift(scans_client, sign_in, session: Session):
    """Requirement 10.6: POST /api/scans/checkout closes the current shift without a QR."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    scans_client.post("/api/scans", json={"qr_token": _token_for(site)}, headers=headers)

    response = scans_client.post("/api/scans/checkout", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "check_out"
    assert body["is_open"] is False
    assert body["site_id"] == str(site.id)


def test_checkout_without_an_open_shift_is_rejected(scans_client, sign_in, session: Session):
    """Requirement 10.6: checking out with nothing open is a 409 no_open_shift."""
    employee = _make_employee(session)
    _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    response = scans_client.post("/api/scans/checkout", headers=headers)
    assert response.status_code == 409
    assert _code(response) == "no_open_shift"


def test_an_implausible_shift_is_flagged_on_checkout(scans_client, sign_in, session: Session):
    """Requirement 10.5: a shift over the implausible threshold is flagged, not refused."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    scans_client.post("/api/scans", json={"qr_token": _token_for(site)}, headers=headers)

    # Backdate the open check-in so the shift is longer than the seeded 16-hour threshold.
    entry = session.scalars(select_time_entries(employee.id)).one()
    entry.check_in_at = datetime.now(UTC) - timedelta(hours=17)
    session.commit()

    response = scans_client.post("/api/scans/checkout", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "check_out"
    assert "implausible_duration" in body["flags"]


# --------------------------------------------------------------------------- guards


def test_an_inactive_employee_is_rejected(scans_client, sign_in, session: Session):
    """Requirement 9.4: a non-active employee cannot check in."""
    employee = _make_employee(session, status=EmployeeStatus.INACTIVE)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    response = scans_client.post("/api/scans", json={"qr_token": _token_for(site)}, headers=headers)
    assert response.status_code == 409
    assert _code(response) == "employee_not_active"
    assert list(session.scalars(select_time_entries(employee.id))) == []


def test_an_inactive_site_is_rejected(scans_client, sign_in, session: Session):
    """Requirement 9.4: a non-active site cannot accept a check-in."""
    employee = _make_employee(session)
    site = _make_site(session, status=SiteStatus.ON_HOLD)
    headers, _ = _employee_login(sign_in, session, employee)

    response = scans_client.post("/api/scans", json={"qr_token": _token_for(site)}, headers=headers)
    assert response.status_code == 409
    assert _code(response) == "site_not_active"


def test_a_locked_period_is_rejected(settings, scans_client, sign_in, session: Session):
    """Requirement 9.5: a scan into a locked month is refused."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    # The scan stamps work_date in the local business timezone, and the period-lock check keys off
    # that local date's month. Lock the local month so the lock matches the month the scan resolves
    # to even near midnight UTC, when the UTC and local dates can fall in different months.
    local_today = datetime.now(UTC).astimezone(ZoneInfo(settings.app_timezone)).date()
    session.add(
        PeriodLock(year=local_today.year, month=local_today.month, locked_at=datetime.now(UTC))
    )
    session.commit()

    response = scans_client.post("/api/scans", json={"qr_token": _token_for(site)}, headers=headers)
    assert response.status_code == 409
    assert _code(response) == "period_locked"


def test_an_invalid_token_is_rejected(scans_client, sign_in, session: Session):
    """Requirement 8.7, 9.1: a malformed or forged token creates no entry."""
    employee = _make_employee(session)
    headers, _ = _employee_login(sign_in, session, employee)

    response = scans_client.post(
        "/api/scans", json={"qr_token": "site:not-a-real-token"}, headers=headers
    )
    assert response.status_code == 400
    assert _code(response) == "invalid_qr"


# --------------------------------------------------------------------------- conflict


def test_an_open_shift_elsewhere_is_a_conflict_naming_the_other_site(
    scans_client, sign_in, session: Session
):
    """Requirement 11.4: a scan at a different site while a shift is open is a 409 naming Site A."""
    employee = _make_employee(session)
    site_a = _make_site(session, number="S-A")
    site_b = _make_site(session, number="S-B")
    headers, _ = _employee_login(sign_in, session, employee)

    first = scans_client.post("/api/scans", json={"qr_token": _token_for(site_a)}, headers=headers)
    assert first.status_code == 200, first.text

    conflict = scans_client.post("/api/scans", json={"qr_token": _token_for(site_b)}, headers=headers)
    assert conflict.status_code == 409
    error = conflict.json()["detail"]["error"]
    assert error["code"] == "open_shift_elsewhere"
    assert error["params"]["site_id"] == str(site_a.id)
    assert error["params"]["site_name"] == site_a.name
    assert error["actions"] == ["transition", "cancel"]


# --------------------------------------------------------------------------- transition


def test_transition_closes_site_a_and_opens_site_b(scans_client, sign_in, session: Session):
    """Requirement 11.5: confirming the move closes the open shift and opens the target."""
    employee = _make_employee(session)
    site_a = _make_site(session, number="S-A")
    site_b = _make_site(session, number="S-B")
    headers, _ = _employee_login(sign_in, session, employee)

    first = scans_client.post("/api/scans", json={"qr_token": _token_for(site_a)}, headers=headers)
    assert first.status_code == 200, first.text

    # Age the open entry so the transition's close is well outside the duplicate window.
    entry = session.scalars(select_time_entries(employee.id)).one()
    entry.check_in_at = datetime.now(UTC) - timedelta(hours=1)
    session.commit()

    response = scans_client.post(
        "/api/scans/transition", json={"qr_token": _token_for(site_b)}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "check_in"
    assert body["site_id"] == str(site_b.id)
    assert body["is_open"] is True

    session.expire_all()
    entries = list(session.scalars(select_time_entries(employee.id).order_by(TimeEntry.check_in_at)))
    assert len(entries) == 2
    closed, opened = entries
    assert closed.site_id == site_a.id
    assert closed.check_out_at is not None
    assert closed.source is TimeEntrySource.SYSTEM_TRANSITION
    assert opened.site_id == site_b.id
    assert opened.check_out_at is None
    # Exactly one open entry after the move.
    assert sum(1 for e in entries if e.check_out_at is None) == 1


def test_transition_to_the_same_site_is_a_conflict(scans_client, sign_in, session: Session):
    """A transition to the site already open is refused with same_site_transition."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    scans_client.post("/api/scans", json={"qr_token": _token_for(site)}, headers=headers)

    response = scans_client.post(
        "/api/scans/transition", json={"qr_token": _token_for(site)}, headers=headers
    )
    assert response.status_code == 409
    assert _code(response) == "same_site_transition"


def test_transition_into_an_inactive_site_is_refused(scans_client, sign_in, session: Session):
    """The new shift runs the check-in guards: a transition into an inactive site is a 409."""
    employee = _make_employee(session)
    site_a = _make_site(session, number="S-A")
    site_b = _make_site(session, number="S-B", status=SiteStatus.ON_HOLD)
    headers, _ = _employee_login(sign_in, session, employee)

    scans_client.post("/api/scans", json={"qr_token": _token_for(site_a)}, headers=headers)

    response = scans_client.post(
        "/api/scans/transition", json={"qr_token": _token_for(site_b)}, headers=headers
    )
    assert response.status_code == 409
    assert _code(response) == "site_not_active"

    # The transaction rolled back: Site A is still the single open shift.
    session.expire_all()
    open_entries = [
        e for e in session.scalars(select_time_entries(employee.id)) if e.check_out_at is None
    ]
    assert len(open_entries) == 1
    assert open_entries[0].site_id == site_a.id


# --------------------------------------------------------------------------- end and move


def test_end_and_move_closes_without_a_qr(scans_client, sign_in, session: Session):
    """Requirement 11.6: end-and-move closes the shift with no departure QR, opening none."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    scans_client.post("/api/scans", json={"qr_token": _token_for(site)}, headers=headers)

    response = scans_client.post("/api/scans/end-and-move", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "check_out"
    assert body["is_open"] is False
    assert body["site_id"] == str(site.id)

    session.expire_all()
    entry = session.scalars(select_time_entries(employee.id)).one()
    assert entry.check_out_at is not None
    assert entry.source is TimeEntrySource.SYSTEM_TRANSITION


def test_end_and_move_without_an_open_shift_is_rejected(scans_client, sign_in, session: Session):
    """Requirement 11.6: with nothing open, end-and-move is a 409 no_open_shift."""
    employee = _make_employee(session)
    _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    response = scans_client.post("/api/scans/end-and-move", headers=headers)
    assert response.status_code == 409
    assert _code(response) == "no_open_shift"


# --------------------------------------------------------------------------- assignment mode


def test_a_strict_site_rejects_an_unassigned_check_in(scans_client, sign_in, session: Session):
    """Requirement 7.4: strict mode refuses a check-in by an employee not assigned to the site."""
    employee = _make_employee(session)
    site = _make_site(session, assignment_mode=AssignmentMode.STRICT)
    headers, _ = _employee_login(sign_in, session, employee)

    response = scans_client.post("/api/scans", json={"qr_token": _token_for(site)}, headers=headers)
    assert response.status_code == 409
    assert _code(response) == "unassigned_site_rejected"


def test_an_open_site_flags_an_unassigned_check_in(scans_client, sign_in, session: Session):
    """Requirement 7.3: open mode allows the check-in and flags it for manager review."""
    employee = _make_employee(session)
    site = _make_site(session, assignment_mode=AssignmentMode.OPEN)
    headers, _ = _employee_login(sign_in, session, employee)

    response = scans_client.post("/api/scans", json={"qr_token": _token_for(site)}, headers=headers)
    assert response.status_code == 200, response.text
    assert "unassigned_site" in response.json()["flags"]


# --------------------------------------------------------------------------- status


def test_status_reports_no_open_shift_then_the_open_shift(scans_client, sign_in, session: Session):
    """Requirement 23.1: status is empty before a check-in and names the open shift after."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    empty = scans_client.get("/api/scans/status", headers=headers)
    assert empty.status_code == 200, empty.text
    assert empty.json()["open_shift"] is None

    scans_client.post("/api/scans", json={"qr_token": _token_for(site)}, headers=headers)

    open_status = scans_client.get("/api/scans/status", headers=headers)
    assert open_status.status_code == 200
    shift = open_status.json()["open_shift"]
    assert shift is not None
    assert shift["site_id"] == str(site.id)


# --------------------------------------------------------------------------- authorization


def test_a_login_with_no_employee_cannot_scan(scans_client, sign_in, session: Session):
    """A manager login has no employee record of its own, so it has nothing to scan."""
    site = _make_site(session)
    # An admin login is admitted by the guard but carries no employee_id.
    headers, _ = sign_in(UserRole.ADMIN)
    response = scans_client.post("/api/scans", json={"qr_token": _token_for(site)}, headers=headers)
    assert response.status_code == 403
    assert _code(response) == "no_employee_for_caller"


def test_the_duplicate_window_does_not_swallow_a_scan_after_it(scans_client, sign_in, session: Session):
    """A scan outside the window is not a duplicate. Uses a pre-existing old entry to prove the edge."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    # An old, already-closed check-in at the same site, well outside the 60s window.
    old = datetime.now(UTC) - timedelta(hours=2)
    session.add(
        TimeEntry(
            employee_id=employee.id,
            site_id=site.id,
            work_date=old.date(),
            check_in_at=old,
            check_out_at=old + timedelta(hours=1),
            total_minutes=60,
            source=TimeEntrySource.QR_SCAN,
            status=TimeEntryStatus.DRAFT,
            flags=[],
        )
    )
    session.commit()

    response = scans_client.post("/api/scans", json={"qr_token": _token_for(site)}, headers=headers)
    assert response.status_code == 200, response.text
    # A fresh check-in, not a duplicate of the two-hour-old entry.
    assert response.json()["action"] == "check_in"


# --------------------------------------------------------------------------- recent work history


def _completed_entry(
    session: Session,
    *,
    employee: Employee,
    site: Site,
    work_date: date,
    check_in: datetime,
    minutes: int,
) -> TimeEntry:
    """A completed entry for `employee` at `site` on `work_date`, worth `minutes`."""
    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=work_date,
        check_in_at=check_in,
        check_out_at=check_in + timedelta(minutes=minutes),
        total_minutes=minutes,
        source=TimeEntrySource.QR_SCAN,
        status=TimeEntryStatus.DRAFT,
        flags=[],
    )
    session.add(entry)
    session.commit()
    return entry


def test_history_returns_only_the_callers_days_newest_first(scans_client, sign_in, session: Session):
    """Requirement 23.3: the caller gets their own completed days, newest first, with per-day totals."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    base = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)
    _completed_entry(
        session, employee=employee, site=site, work_date=date(2025, 3, 10), check_in=base, minutes=120
    )
    _completed_entry(
        session,
        employee=employee,
        site=site,
        work_date=date(2025, 3, 12),
        check_in=base + timedelta(days=2),
        minutes=90,
    )

    response = scans_client.get("/api/scans/history", headers=headers)
    assert response.status_code == 200, response.text
    days = response.json()["days"]
    assert [d["work_date"] for d in days] == ["2025-03-12", "2025-03-10"]
    assert [d["total_minutes"] for d in days] == [90, 120]


def test_history_never_shows_another_employees_entries(scans_client, sign_in, session: Session):
    """The endpoint is scoped to the caller: another employee's completed days never appear."""
    caller = _make_employee(session)
    other = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, caller)

    base = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)
    # Only the other employee has any completed work.
    _completed_entry(
        session, employee=other, site=site, work_date=date(2025, 3, 10), check_in=base, minutes=200
    )

    response = scans_client.get("/api/scans/history", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["days"] == []


def test_history_sums_a_multi_site_day_across_sites(scans_client, sign_in, session: Session):
    """Requirement 11.2: a day split between two sites reports the combined minutes as the day total."""
    employee = _make_employee(session)
    site_a = _make_site(session, number="S-A")
    site_b = _make_site(session, number="S-B")
    headers, _ = _employee_login(sign_in, session, employee)

    work_date = date(2025, 3, 10)
    morning = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)
    afternoon = datetime(2025, 3, 10, 13, 0, tzinfo=UTC)
    _completed_entry(
        session, employee=employee, site=site_a, work_date=work_date, check_in=morning, minutes=120
    )
    _completed_entry(
        session, employee=employee, site=site_b, work_date=work_date, check_in=afternoon, minutes=90
    )

    response = scans_client.get("/api/scans/history", headers=headers)
    assert response.status_code == 200, response.text
    days = response.json()["days"]
    assert len(days) == 1
    assert days[0]["work_date"] == "2025-03-10"
    assert days[0]["total_minutes"] == 210


def test_history_excludes_an_open_shift(scans_client, sign_in, session: Session):
    """An open shift has no total yet, so a day that holds only one does not appear in the history."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    completed_day = date(2025, 3, 10)
    _completed_entry(
        session,
        employee=employee,
        site=site,
        work_date=completed_day,
        check_in=datetime(2025, 3, 10, 8, 0, tzinfo=UTC),
        minutes=60,
    )
    # An open shift on a *different*, later day: no check-out, no total.
    session.add(
        TimeEntry(
            employee_id=employee.id,
            site_id=site.id,
            work_date=date(2025, 3, 11),
            check_in_at=datetime(2025, 3, 11, 8, 0, tzinfo=UTC),
            check_out_at=None,
            total_minutes=None,
            source=TimeEntrySource.QR_SCAN,
            status=TimeEntryStatus.DRAFT,
            flags=[],
        )
    )
    session.commit()

    response = scans_client.get("/api/scans/history", headers=headers)
    assert response.status_code == 200, response.text
    days = response.json()["days"]
    # Only the completed day; the open shift's day is absent, and the completed total is unaffected.
    assert [d["work_date"] for d in days] == ["2025-03-10"]
    assert days[0]["total_minutes"] == 60


def test_history_for_a_login_with_no_employee_is_empty(scans_client, sign_in, session: Session):
    """A login not linked to an employee gets an empty history, matching scan_status's quiet read."""
    # An admin login is admitted by the guard but carries no employee_id — like scan_status, this is a
    # quiet empty read rather than an error.
    headers, _ = sign_in(UserRole.ADMIN)
    response = scans_client.get("/api/scans/history", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["days"] == []


# --------------------------------------------------------------------------- history with a date range


def _seed_days(session: Session, *, employee: Employee, site: Site, days: dict[date, int]) -> None:
    """One completed entry per `work_date` in `days`, each worth the mapped minutes."""
    for index, (work_date, minutes) in enumerate(days.items()):
        _completed_entry(
            session,
            employee=employee,
            site=site,
            work_date=work_date,
            check_in=datetime(work_date.year, work_date.month, work_date.day, 8, 0, tzinfo=UTC)
            + timedelta(minutes=index),
            minutes=minutes,
        )


def test_history_range_returns_only_days_within_it_inclusive(scans_client, sign_in, session: Session):
    """A ranged query returns exactly the days between date_from and date_to, both bounds included."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    _seed_days(
        session,
        employee=employee,
        site=site,
        days={
            date(2025, 3, 9): 60,  # before the range
            date(2025, 3, 10): 120,  # lower bound, included
            date(2025, 3, 12): 90,  # inside
            date(2025, 3, 15): 100,  # upper bound, included
            date(2025, 3, 16): 45,  # after the range
        },
    )

    response = scans_client.get(
        "/api/scans/history",
        params={"date_from": "2025-03-10", "date_to": "2025-03-15"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    days = response.json()["days"]
    # Only the three days in [2025-03-10, 2025-03-15], newest first; the bounds themselves are kept.
    assert [d["work_date"] for d in days] == ["2025-03-15", "2025-03-12", "2025-03-10"]
    assert [d["total_minutes"] for d in days] == [100, 90, 120]


def test_history_range_is_not_capped_at_fourteen_days(scans_client, sign_in, session: Session):
    """A range returns all its days: the 14-day cap of the unfiltered view does not apply."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    # Twenty consecutive completed days — more than the recent-work cap of 14.
    start = date(2025, 1, 1)
    _seed_days(
        session,
        employee=employee,
        site=site,
        days={start + timedelta(days=n): 60 for n in range(20)},
    )

    response = scans_client.get(
        "/api/scans/history",
        params={"date_from": "2025-01-01", "date_to": "2025-01-20"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    days = response.json()["days"]
    assert len(days) == 20  # every day in the range, not just the most recent 14


def test_history_without_a_range_is_still_capped_newest_first(scans_client, sign_in, session: Session):
    """The home-screen contract is unchanged: no params keeps the newest-first 14-day cap."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    start = date(2025, 1, 1)
    _seed_days(
        session,
        employee=employee,
        site=site,
        days={start + timedelta(days=n): 60 for n in range(20)},
    )

    response = scans_client.get("/api/scans/history", headers=headers)
    assert response.status_code == 200, response.text
    days = response.json()["days"]
    # Capped at 14 and newest first — the most recent day is 2025-01-20.
    assert len(days) == 14
    assert days[0]["work_date"] == "2025-01-20"
    assert days[-1]["work_date"] == "2025-01-07"


def test_history_range_never_shows_another_employees_entries(scans_client, sign_in, session: Session):
    """The range stays self-scoped: another employee's days in the same window never appear."""
    caller = _make_employee(session)
    other = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, caller)

    # The caller has one day in the window; the other employee has a day in the same window.
    _seed_days(session, employee=caller, site=site, days={date(2025, 3, 10): 120})
    _seed_days(session, employee=other, site=site, days={date(2025, 3, 11): 200})

    response = scans_client.get(
        "/api/scans/history",
        params={"date_from": "2025-03-01", "date_to": "2025-03-31"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    days = response.json()["days"]
    assert [d["work_date"] for d in days] == ["2025-03-10"]
    assert days[0]["total_minutes"] == 120


def test_history_inverted_range_returns_empty_and_does_not_error(scans_client, sign_in, session: Session):
    """An inverted range (from after to) matches no day and answers empty, without erroring."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = _employee_login(sign_in, session, employee)

    _seed_days(session, employee=employee, site=site, days={date(2025, 3, 10): 120})

    response = scans_client.get(
        "/api/scans/history",
        params={"date_from": "2025-03-15", "date_to": "2025-03-10"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["days"] == []


def test_history_range_sums_a_multi_site_day_across_sites(scans_client, sign_in, session: Session):
    """Requirement 11.2 within a range: a day split across sites reports the combined minutes."""
    employee = _make_employee(session)
    site_a = _make_site(session, number="S-A")
    site_b = _make_site(session, number="S-B")
    headers, _ = _employee_login(sign_in, session, employee)

    work_date = date(2025, 3, 10)
    _completed_entry(
        session,
        employee=employee,
        site=site_a,
        work_date=work_date,
        check_in=datetime(2025, 3, 10, 8, 0, tzinfo=UTC),
        minutes=120,
    )
    _completed_entry(
        session,
        employee=employee,
        site=site_b,
        work_date=work_date,
        check_in=datetime(2025, 3, 10, 13, 0, tzinfo=UTC),
        minutes=90,
    )

    response = scans_client.get(
        "/api/scans/history",
        params={"date_from": "2025-03-10", "date_to": "2025-03-10"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    days = response.json()["days"]
    assert len(days) == 1
    assert days[0]["work_date"] == "2025-03-10"
    assert days[0]["total_minutes"] == 210
