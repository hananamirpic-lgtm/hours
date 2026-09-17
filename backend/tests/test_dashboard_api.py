"""The administrator home dashboard over HTTP and through the service (Requirement 18.5, 18.6).

The dashboard heads the console with, for the current month, the number of active employees and active
sites, total work hours, billing, employee cost and gross profit (Requirement 18.5), and an attention
section counting employees without a check-out, without a check-in, and days missing entirely
(Requirement 18.6). The finance figures are the by-site report's totals for the month, so the
dashboard reconciles with that report by construction (Requirement 18.9); the attention counts are the
missing-report findings for the month bucketed by kind, so a count and the list it links to agree.

The service (`build_dashboard`) takes `today` so a test can fix the month; the router passes the real
clock. The counts and the attention section are exercised through the service against a fixed month,
and the HTTP layer is exercised for the guard (finance only) and the response shape. The sign-in,
seeded settings and make-* helpers mirror `test_reports_api.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.employee import Employee, EmployeeRate, EmployeeStatus
from app.models.setting import Setting, SettingValueType
from app.models.site import EmployeeSite, Site, SiteRate, SiteStatus
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.models.user import UserRole
from app.services import billing as billing_service
from app.services import payroll as payroll_service
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


@pytest.fixture(autouse=True)
def _seed_settings(session: Session) -> None:
    """The classification settings billing and payroll read (SQLite carries no seed data)."""
    rows = [
        ("overtime_daily_threshold_minutes", "480", SettingValueType.INTEGER),
        ("shabbat_start_weekday", "4", SettingValueType.INTEGER),
        ("shabbat_start_time", "16:00", SettingValueType.TIME),
        ("shabbat_end_weekday", "5", SettingValueType.INTEGER),
        ("shabbat_end_time", "20:00", SettingValueType.TIME),
    ]
    for key, value, value_type in rows:
        session.add(Setting(key=key, value=value, value_type=value_type))
    session.commit()


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


_PASSPORT = iter(f"D{n:07d}" for n in range(1, 1_000_000))


def _make_employee(
    session: Session, *, name: str = "Worker", status: EmployeeStatus = EmployeeStatus.ACTIVE
) -> Employee:
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
        status=status,
    )
    session.add(employee)
    session.commit()
    return employee


def _add_wage(session: Session, employee: Employee, *, wage: str) -> None:
    session.add(
        EmployeeRate(
            employee_id=employee.id,
            hourly_wage=Decimal(wage),
            overtime_rate=Decimal(wage),
            shabbat_holiday_rate=Decimal(wage),
            travel_allowance_daily=Decimal("0"),
            effective_from=date(2025, 1, 1),
            effective_to=None,
        )
    )
    session.commit()


def _make_client(session: Session, *, name: str = "Acme") -> Client:
    client = Client(name=name)
    session.add(client)
    session.commit()
    return client


def _make_site(
    session: Session,
    *,
    client: Client,
    number: str,
    billing_rate: str,
    status: SiteStatus = SiteStatus.ACTIVE,
) -> Site:
    site = Site(
        name=f"Site {number}",
        site_number=number,
        client_id=client.id,
        qr_token=f"placeholder-{uuid.uuid4().hex}",
        qr_token_version=1,
        status=status,
    )
    session.add(site)
    session.flush()
    site.rates.append(
        SiteRate(
            billing_rate=Decimal(billing_rate),
            overtime_billing_rate=None,
            effective_from=date(2025, 1, 1),
            effective_to=None,
        )
    )
    session.commit()
    return site


def _entry(
    session: Session,
    *,
    employee: Employee,
    site: Site,
    work_date: date,
    start_hour: int,
    minutes: int,
    status: TimeEntryStatus = TimeEntryStatus.APPROVED,
    open_shift: bool = False,
    flags: list[str] | None = None,
) -> TimeEntry:
    local_in = datetime(
        work_date.year, work_date.month, work_date.day, start_hour, 0,
        tzinfo=ZoneInfo("Asia/Jerusalem"),
    )
    check_in = local_in.astimezone(UTC)
    check_out = None if open_shift else check_in + timedelta(minutes=minutes)
    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=work_date,
        check_in_at=check_in,
        check_out_at=check_out,
        total_minutes=None if open_shift else minutes,
        source=TimeEntrySource.QR_SCAN,
        is_manual=False,
        status=status,
        flags=flags or [],
    )
    session.add(entry)
    session.commit()
    return entry


def _weekday_in_month(today: date) -> date:
    """A mid-month day that is Sunday–Thursday, so a 07:00 shift is clear of the Shabbat window.

    Friday (weekday 4) evening and Saturday (weekday 5) fall inside the seeded Shabbat window, which
    would push the hours into the Shabbat bucket rather than regular/overtime and leave the billing —
    and so the by-site total the dashboard reads — at zero. Starting from the 15th and stepping back
    avoids that without depending on which month the test runs in.
    """
    day = today.replace(day=15)
    while day.weekday() in (4, 5):  # Friday, Saturday
        day -= timedelta(days=1)
    return day


def _assign(
    session: Session, *, employee: Employee, site: Site, day: date, day_to: date | None = None
) -> None:
    session.add(
        EmployeeSite(
            employee_id=employee.id,
            site_id=site.id,
            assigned_from=day,
            assigned_to=day_to or day,
        )
    )
    session.commit()


# ===================================================================== figures (18.5)


def test_dashboard_reports_current_month_figures(session: Session):
    """Requirement 18.5: the dashboard shows the current month's headline figures.

    The brief's two-site day placed in the current month: one employee at 35 ₪ working 4.5 h at Site A
    (60 ₪) and 5.0 h at Site B (75 ₪), with the 30-minute gap between the two shifts split 15/15 and
    paid and billed at both sites, so A is worked 4.75 h and B 5.25 h. Billing is 285 + 393.75 =
    678.75, cost 350.00, profit 328.75, and the total hours are 600 minutes. Payroll and billing are
    computed first, as production runs them, so the by-site totals the dashboard reads exist.
    """
    today = date.today()
    work_day = _weekday_in_month(today)  # a Sun–Thu day, clear of the Shabbat window

    employee = _make_employee(session)
    _add_wage(session, employee, wage="35.00")
    client = _make_client(session)
    site_a = _make_site(session, client=client, number="A", billing_rate="60.00")
    site_b = _make_site(session, client=client, number="B", billing_rate="75.00")
    _entry(session, employee=employee, site=site_a, work_date=work_day, start_hour=7, minutes=270)
    _entry(session, employee=employee, site=site_b, work_date=work_day, start_hour=12, minutes=300)

    payroll_service.calculate_payroll(
        session, employee_id=employee.id, year=today.year, month=today.month
    )
    session.commit()
    billing_service.calculate_billing(session, year=today.year, month=today.month)
    session.commit()

    report = reports_service.build_dashboard(session, today=today)

    assert report.year == today.year
    assert report.month == today.month
    assert report.currency == "ILS"
    assert report.active_employees == 1
    assert report.active_sites == 2
    assert report.total_minutes == 600
    assert report.total_billing == Decimal("678.75")
    assert report.total_cost == Decimal("350.00")
    assert report.total_profit == Decimal("328.75")


def test_active_counts_exclude_non_active(session: Session):
    """Requirement 18.5: only active employees and active sites are counted.

    Two employees (one active, one terminated) and two sites (one active, one on hold): the counts are
    one each, because "active" is a current status, not a per-period figure.
    """
    _make_employee(session, name="Active", status=EmployeeStatus.ACTIVE)
    _make_employee(session, name="Gone", status=EmployeeStatus.TERMINATED)
    client = _make_client(session)
    _make_site(session, client=client, number="A", billing_rate="60.00", status=SiteStatus.ACTIVE)
    _make_site(session, client=client, number="B", billing_rate="60.00", status=SiteStatus.ON_HOLD)

    report = reports_service.build_dashboard(session, today=date.today())

    assert report.active_employees == 1
    assert report.active_sites == 1


def test_empty_month_is_all_zero(session: Session):
    """A month with no data is a valid dashboard: every figure and count is zero, not an error."""
    report = reports_service.build_dashboard(session, today=date.today())

    assert report.active_employees == 0
    assert report.active_sites == 0
    assert report.total_minutes == 0
    assert report.total_billing == Decimal("0.00")
    assert report.total_cost == Decimal("0.00")
    assert report.total_profit == Decimal("0.00")
    assert report.attention.missing_checkout == 0
    assert report.attention.missing_checkin == 0
    assert report.attention.missing_reports == 0


# ===================================================================== attention (18.6)


def test_attention_counts_the_three_kinds(session: Session):
    """Requirement 18.6: the attention section counts each of the three missing-report kinds.

    One employee expected at a site across three current-month days: an open entry (missing check-out),
    a missing-check-in marker, and a day with nothing (both missing). Each attention count is one.
    """
    today = date.today()
    d1 = today.replace(day=10)
    d2 = today.replace(day=11)
    d3 = today.replace(day=12)

    employee = _make_employee(session)
    site = _make_site(session, client=_make_client(session), number="A", billing_rate="60.00")
    _assign(session, employee=employee, site=site, day=d1, day_to=d3)

    _entry(session, employee=employee, site=site, work_date=d1, start_hour=8, minutes=0, open_shift=True)
    _entry(
        session, employee=employee, site=site, work_date=d2, start_hour=17, minutes=1,
        flags=[reports_service.FLAG_MISSING_CHECK_IN],
    )
    # d3: expected, nothing recorded → both missing.

    report = reports_service.build_dashboard(session, today=today)

    assert report.attention.missing_checkout == 1
    assert report.attention.missing_checkin == 1
    assert report.attention.missing_reports == 1


def test_attention_reconciles_with_the_missing_report_list(session: Session):
    """Requirement 18.6, 18.9: the attention counts equal the missing-report findings for the month.

    The dashboard bucket counts must equal the number of findings of each kind that the missing-report
    detection returns over the same month, so a count and the list it links to never disagree.
    """
    today = date.today()
    employee = _make_employee(session)
    site = _make_site(session, client=_make_client(session), number="A", billing_rate="60.00")
    _assign(session, employee=employee, site=site, day=today.replace(day=5), day_to=today.replace(day=7))
    _entry(
        session, employee=employee, site=site, work_date=today.replace(day=5),
        start_hour=8, minutes=0, open_shift=True,
    )
    # 6th and 7th: nothing → both missing.

    month_start = today.replace(day=1)
    if today.month == 12:
        month_end = today.replace(day=31)
    else:
        month_end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)

    findings = reports_service.detect_missing_reports(
        session,
        date_from=month_start,
        date_to=month_end,
        scope=reports_service.SiteScope.all_sites(),
    )
    from app.services.reports import MissingReportKind

    report = reports_service.build_dashboard(session, today=today)
    assert report.attention.missing_checkout == sum(
        1 for f in findings if f.kind is MissingReportKind.MISSING_CHECKOUT
    )
    assert report.attention.missing_reports == sum(
        1 for f in findings if f.kind is MissingReportKind.BOTH_MISSING
    )


# ===================================================================== HTTP shape and guards


def test_admin_reads_dashboard_over_http(reports_client, sign_in, session: Session):
    """Requirement 18.5: an administrator reads the dashboard, with the full shape."""
    headers, _ = sign_in(UserRole.ADMIN)
    _make_employee(session)
    _make_site(session, client=_make_client(session), number="A", billing_rate="60.00")

    response = reports_client.get("/api/reports/dashboard", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    today = date.today()
    assert body["year"] == today.year
    assert body["month"] == today.month
    assert body["currency"] == "ILS"
    assert body["active_employees"] == 1
    assert body["active_sites"] == 1
    assert "attention" in body
    assert set(body["attention"]) == {"missing_checkout", "missing_checkin", "missing_reports"}


def test_accounting_reads_dashboard(reports_client, sign_in, session: Session):
    """Requirement 2.6: accounting reads the finance dashboard."""
    headers, _ = sign_in(UserRole.ACCOUNTING)
    response = reports_client.get("/api/reports/dashboard", headers=headers)
    assert response.status_code == 200, response.text


def test_site_manager_cannot_read_dashboard(reports_client, sign_in, session: Session):
    """Requirement 2.5, 18.5: the dashboard carries billing and profit — finance only."""
    headers, _ = sign_in(UserRole.SITE_MANAGER)
    response = reports_client.get("/api/reports/dashboard", headers=headers)
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_employee_cannot_read_dashboard(reports_client, sign_in, session: Session):
    """Requirement 2.7: an employee has no business on the administrator dashboard."""
    headers, _ = sign_in(UserRole.EMPLOYEE)
    response = reports_client.get("/api/reports/dashboard", headers=headers)
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"
