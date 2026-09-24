"""Report queries over HTTP and through the service (Requirement 18).

The reports recompute nothing — they group the figures the payroll and billing engines already
produced — so the subject here is the aggregation, the reconciliation the requirement demands, the
envelope every response carries, and the guards:

* the four reports return the right shape and figures: hours and cost per employee (18.1); hours,
  billing, cost and profit per site (18.2); amount per site and client total (18.3); billing, cost and
  gross profit for the filters (18.4);
* **reconciliation (18.9)**: the sum of the per-site figures equals the corresponding totals, and each
  per-employee cost equals that employee's payroll record for the period;
* every response states its period, the filters applied and the currency `ILS` (18.7);
* a site manager sees hours for their sites on the by-employee report but no cost, and cannot reach the
  billing, profit or client reports at all (Requirement 2.5);
* **performance (24.2)**: one month, 100 employees, 20 sites returns within 5 seconds.

The reports read the computed records, so each test calculates payroll for every employee-month and
billing for the month before reading a report — the same order production runs them in. The sign-in
helper, the seeded settings and the make-* helpers mirror `test_billing_api.py`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.employee import Employee, EmployeeRate
from app.models.setting import Setting, SettingValueType
from app.models.site import Site, SiteRate
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.models.user import UserRole
from app.models.user_site import UserSite
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


_PASSPORT = iter(f"R{n:07d}" for n in range(1, 1_000_000))


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


def _make_site(session: Session, *, client: Client, number: str, billing_rate: str) -> Site:
    site = Site(
        name=f"Site {number}",
        site_number=number,
        client_id=client.id,
        qr_token=f"placeholder-{uuid.uuid4().hex}",
        qr_token_version=1,
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


def _make_entry(
    session: Session,
    *,
    employee: Employee,
    site: Site,
    day: int,
    start_hour: int,
    minutes: int,
    status: TimeEntryStatus = TimeEntryStatus.APPROVED,
) -> TimeEntry:
    local_in = datetime(2025, 8, day, start_hour, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
    check_in = local_in.astimezone(UTC)
    check_out = check_in + timedelta(minutes=minutes)
    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=date(2025, 8, day),
        check_in_at=check_in,
        check_out_at=check_out,
        total_minutes=minutes,
        source=TimeEntrySource.QR_SCAN,
        is_manual=False,
        status=status,
        flags=[],
    )
    session.add(entry)
    session.commit()
    return entry


def _calc_payroll(reports_client, headers, employee: Employee) -> None:
    response = reports_client.post(
        "/api/payroll/calculate",
        json={"employee_id": str(employee.id), "year": 2025, "month": 8},
        headers=headers,
    )
    assert response.status_code == 200, response.text


def _calc_billing(reports_client, headers) -> None:
    response = reports_client.post(
        "/api/billing/calculate", json={"year": 2025, "month": 8}, headers=headers
    )
    assert response.status_code == 200, response.text


def _brief_scenario(reports_client, headers, session: Session):
    """The brief's two-site day, computed and billed, ready to read a report from.

    One client, two sites (A at 60 ₪, B at 75 ₪); one employee paid 35 ₪ working 4.5 h at A and 5.0 h
    at B, with a 30-minute gap between the sites split 15/15 as travel time. Payroll then allocates
    166.25 at A and 183.75 at B; billing bills 285.00 at A and 393.75 at B (travel is paid and billed).
    Returns the client, the two sites and the employee.
    """
    employee = _make_employee(session)
    _add_wage(session, employee, wage="35.00")
    client = _make_client(session)
    site_a = _make_site(session, client=client, number="A", billing_rate="60.00")
    site_b = _make_site(session, client=client, number="B", billing_rate="75.00")
    _make_entry(session, employee=employee, site=site_a, day=4, start_hour=7, minutes=270)
    _make_entry(session, employee=employee, site=site_b, day=4, start_hour=12, minutes=300)
    _calc_payroll(reports_client, headers, employee)
    _calc_billing(reports_client, headers)
    return client, site_a, site_b, employee


# ===================================================================== by employee (18.1)


def test_by_employee_reports_hours_and_cost(reports_client, sign_in, session: Session):
    """Requirement 18.1: the by-employee report shows hours and cost for the period."""
    headers, _ = sign_in(UserRole.ADMIN)
    _, _, _, employee = _brief_scenario(reports_client, headers, session)

    response = reports_client.get(
        "/api/reports/by-employee?year=2025&month=8", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["employee_id"] == str(employee.id)
    # 4.5 h + 5.0 h clocked plus the 30-minute travel gap (split 15/15) = 600 minutes; 480 regular
    # + 120 overtime after the travel minutes are folded in.
    assert row["total_minutes"] == 600
    assert row["regular_minutes"] == 480
    assert row["overtime_minutes"] == 120
    # Cost is the payroll total: 35 ₪ over 10 h (9.5 clocked + 0.5 travel).
    assert Decimal(row["cost"]) == Decimal("350.00")
    assert Decimal(body["total_cost"]) == Decimal("350.00")


# ===================================================================== by site (18.2)


def test_by_site_reports_hours_billing_cost_profit(reports_client, sign_in, session: Session):
    """Requirement 18.2: the by-site report shows hours, billing, cost and profit per site."""
    headers, _ = sign_in(UserRole.ADMIN)
    _, site_a, site_b, _ = _brief_scenario(reports_client, headers, session)

    response = reports_client.get("/api/reports/by-site?year=2025&month=8", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    by_site = {s["site_id"]: s for s in body["rows"]}
    assert Decimal(by_site[str(site_a.id)]["billing"]) == Decimal("285.00")
    assert Decimal(by_site[str(site_a.id)]["cost"]) == Decimal("166.25")
    assert Decimal(by_site[str(site_a.id)]["profit"]) == Decimal("118.75")
    assert Decimal(by_site[str(site_b.id)]["billing"]) == Decimal("393.75")
    assert Decimal(by_site[str(site_b.id)]["cost"]) == Decimal("183.75")
    assert Decimal(by_site[str(site_b.id)]["profit"]) == Decimal("210.00")


# ===================================================================== by client (18.3)


def test_by_client_reports_amount_per_site_and_total(reports_client, sign_in, session: Session):
    """Requirement 18.3: the by-client report shows the amount per site and the client total."""
    headers, _ = sign_in(UserRole.ADMIN)
    client, site_a, site_b, _ = _brief_scenario(reports_client, headers, session)

    response = reports_client.get("/api/reports/by-client?year=2025&month=8", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["client_id"] == str(client.id)
    per_site = {s["site_id"]: Decimal(s["amount"]) for s in row["sites"]}
    assert per_site[str(site_a.id)] == Decimal("285.00")
    assert per_site[str(site_b.id)] == Decimal("393.75")
    assert Decimal(row["total"]) == Decimal("678.75")
    assert Decimal(body["total"]) == Decimal("678.75")


# ===================================================================== profitability (18.4)


def test_profitability_reports_billing_cost_gross_profit(reports_client, sign_in, session: Session):
    """Requirement 18.4: the profitability report shows total billing, cost and gross profit."""
    headers, _ = sign_in(UserRole.ADMIN)
    _brief_scenario(reports_client, headers, session)

    response = reports_client.get(
        "/api/reports/profitability?year=2025&month=8", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(body["total_billing"]) == Decimal("678.75")
    assert Decimal(body["total_cost"]) == Decimal("350.00")
    assert Decimal(body["total_profit"]) == Decimal("328.75")
    assert body["site_count"] == 2


def test_profitability_filters_by_site(reports_client, sign_in, session: Session):
    """Requirement 18.4: a site filter narrows profitability to that site's figures."""
    headers, _ = sign_in(UserRole.ADMIN)
    _, site_a, _, _ = _brief_scenario(reports_client, headers, session)

    response = reports_client.get(
        f"/api/reports/profitability?year=2025&month=8&site_id={site_a.id}", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["site_count"] == 1
    assert Decimal(body["total_billing"]) == Decimal("285.00")
    assert Decimal(body["total_cost"]) == Decimal("166.25")
    assert Decimal(body["total_profit"]) == Decimal("118.75")
    assert body["filters"]["site_id"] == str(site_a.id)


# ===================================================================== envelope (18.7)


@pytest.mark.parametrize(
    "path",
    [
        "/api/reports/by-employee?year=2025&month=8",
        "/api/reports/by-site?year=2025&month=8",
        "/api/reports/by-client?year=2025&month=8",
        "/api/reports/profitability?year=2025&month=8",
    ],
)
def test_every_report_states_period_filters_and_currency(
    reports_client, sign_in, session: Session, path: str
):
    """Requirement 18.7: every report states its period, the filters applied and ILS."""
    headers, _ = sign_in(UserRole.ADMIN)
    _brief_scenario(reports_client, headers, session)

    response = reports_client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["year"] == 2025
    assert body["month"] == 8
    assert body["currency"] == "ILS"
    assert body["filters"]["year"] == 2025
    assert body["filters"]["month"] == 8


# ===================================================================== reconciliation (18.9)


def test_per_site_sums_equal_the_totals(reports_client, sign_in, session: Session):
    """Requirement 18.9: the sum of the per-site figures equals the corresponding totals.

    A richer scenario than the brief's — three sites under two clients, two employees — so the sums
    are non-trivial. The by-site totals must equal the sum of the rows for billing, cost and profit,
    and the by-client grand total must equal the sum of the client totals and of every site amount.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    client_1 = _make_client(session, name="Client One")
    client_2 = _make_client(session, name="Client Two")
    site_a = _make_site(session, client=client_1, number="A", billing_rate="60.00")
    site_b = _make_site(session, client=client_1, number="B", billing_rate="75.00")
    site_c = _make_site(session, client=client_2, number="C", billing_rate="50.00")

    alice = _make_employee(session, name="Alice")
    bob = _make_employee(session, name="Bob")
    _add_wage(session, alice, wage="40.00")
    _add_wage(session, bob, wage="30.00")

    _make_entry(session, employee=alice, site=site_a, day=4, start_hour=7, minutes=270)
    _make_entry(session, employee=alice, site=site_b, day=4, start_hour=12, minutes=300)
    _make_entry(session, employee=bob, site=site_c, day=5, start_hour=6, minutes=480)
    _make_entry(session, employee=bob, site=site_a, day=6, start_hour=8, minutes=300)

    _calc_payroll(reports_client, headers, alice)
    _calc_payroll(reports_client, headers, bob)
    _calc_billing(reports_client, headers)

    by_site = reports_client.get(
        "/api/reports/by-site?year=2025&month=8", headers=headers
    ).json()

    summed_billing = sum(Decimal(r["billing"]) for r in by_site["rows"])
    summed_cost = sum(Decimal(r["cost"]) for r in by_site["rows"])
    summed_profit = sum(Decimal(r["profit"]) for r in by_site["rows"])
    assert summed_billing == Decimal(by_site["total_billing"])
    assert summed_cost == Decimal(by_site["total_cost"])
    assert summed_profit == Decimal(by_site["total_profit"])
    # And profit is billing minus cost at the total level too.
    assert Decimal(by_site["total_profit"]) == Decimal(by_site["total_billing"]) - Decimal(
        by_site["total_cost"]
    )

    by_client = reports_client.get(
        "/api/reports/by-client?year=2025&month=8", headers=headers
    ).json()
    # Each client's total equals the sum of its sites' amounts.
    for row in by_client["rows"]:
        assert Decimal(row["total"]) == sum(Decimal(s["amount"]) for s in row["sites"])
    # The grand total equals the sum of the client totals, and the by-site billing total.
    assert Decimal(by_client["total"]) == sum(Decimal(r["total"]) for r in by_client["rows"])
    assert Decimal(by_client["total"]) == Decimal(by_site["total_billing"])

    # Profitability over no filter reconciles with the by-site totals.
    profit = reports_client.get(
        "/api/reports/profitability?year=2025&month=8", headers=headers
    ).json()
    assert Decimal(profit["total_billing"]) == Decimal(by_site["total_billing"])
    assert Decimal(profit["total_cost"]) == Decimal(by_site["total_cost"])
    assert Decimal(profit["total_profit"]) == Decimal(by_site["total_profit"])


def test_per_employee_cost_equals_the_payroll_record(reports_client, sign_in, session: Session):
    """Requirement 18.9: each per-employee report cost equals that employee's payroll record.

    The report cost is read from the payroll record, so this proves the two never diverge: for each
    employee the by-employee row's cost equals the `total_pay` the payroll read returns for the same
    period.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    alice = _make_employee(session, name="Alice")
    bob = _make_employee(session, name="Bob")
    _add_wage(session, alice, wage="40.00")
    _add_wage(session, bob, wage="30.00")
    client = _make_client(session)
    site = _make_site(session, client=client, number="A", billing_rate="60.00")
    _make_entry(session, employee=alice, site=site, day=4, start_hour=7, minutes=300)
    _make_entry(session, employee=bob, site=site, day=5, start_hour=6, minutes=480)
    _calc_payroll(reports_client, headers, alice)
    _calc_payroll(reports_client, headers, bob)

    report = reports_client.get(
        "/api/reports/by-employee?year=2025&month=8", headers=headers
    ).json()
    report_cost = {r["employee_id"]: Decimal(r["cost"]) for r in report["rows"]}

    for employee in (alice, bob):
        payroll = reports_client.get(
            f"/api/payroll/{employee.id}/2025/8", headers=headers
        ).json()
        assert report_cost[str(employee.id)] == Decimal(payroll["total_pay"])

    # And the report total equals the sum of the payroll records.
    assert Decimal(report["total_cost"]) == sum(report_cost.values())


# ===================================================================== authorization (2.5)


def _assign_manager(session: Session, user, site: Site) -> None:
    session.add(UserSite(user_id=user.id, site_id=site.id))
    session.commit()


def test_site_manager_sees_hours_but_not_cost_on_by_employee(
    reports_client, sign_in, session: Session
):
    """Requirement 2.5: a site manager sees hours for their sites but not the wage/cost.

    The manager is assigned Site A only. The by-employee report shows the hours the employee worked at
    Site A (270 min), not the whole day, and carries no cost figure.
    """
    admin_headers, _ = sign_in(UserRole.ADMIN)
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    _, site_a, _site_b, employee = _brief_scenario(reports_client, admin_headers, session)
    _assign_manager(session, manager, site_a)

    response = reports_client.get(
        "/api/reports/by-employee?year=2025&month=8", headers=manager_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["employee_id"] == str(employee.id)
    # Only Site A's hours: 270 clocked + its 15-minute half of the 30-minute travel gap = 285, not
    # the full day. Cost is still hidden from a manager.
    assert row["total_minutes"] == 285
    # No cost for a manager.
    assert row["cost"] is None
    assert body["total_cost"] is None


def test_site_manager_cannot_read_by_site(reports_client, sign_in, session: Session):
    """Requirement 2.5, 18.2: the by-site report carries billing and profit — finance only."""
    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)
    response = reports_client.get(
        "/api/reports/by-site?year=2025&month=8", headers=manager_headers
    )
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_site_manager_cannot_read_by_client(reports_client, sign_in, session: Session):
    """Requirement 2.5, 18.3: the by-client report is client billing — finance only."""
    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)
    response = reports_client.get(
        "/api/reports/by-client?year=2025&month=8", headers=manager_headers
    )
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_site_manager_cannot_read_profitability(reports_client, sign_in, session: Session):
    """Requirement 2.5, 18.4: the profitability report is billing and profit — finance only."""
    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)
    response = reports_client.get(
        "/api/reports/profitability?year=2025&month=8", headers=manager_headers
    )
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_accounting_may_read_every_report(reports_client, sign_in, session: Session):
    """Requirement 2.6: accounting reads the business reports, including cost and billing."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    accounting_headers, _ = sign_in(UserRole.ACCOUNTING)
    _brief_scenario(reports_client, admin_headers, session)

    for path in (
        "/api/reports/by-employee?year=2025&month=8",
        "/api/reports/by-site?year=2025&month=8",
        "/api/reports/by-client?year=2025&month=8",
        "/api/reports/profitability?year=2025&month=8",
    ):
        response = reports_client.get(path, headers=accounting_headers)
        assert response.status_code == 200, f"{path}: {response.text}"


# ===================================================================== performance (24.2)


@pytest.mark.slow
def test_reports_over_100_employees_and_20_sites_are_under_5_seconds(
    reports_client, sign_in, session: Session
):
    """Requirement 24.2: a month of 100 employees over 20 sites reports within 5 seconds.

    Builds one month of data — 100 employees, each working an 8-hour approved day at each of 20 sites —
    computes payroll for every employee and billing for the month, then times reading all four
    reports. The assertion is on the read time, which is what the requirement bounds; the setup and
    calculation are excluded from the timed section.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    client = _make_client(session)
    sites = [
        _make_site(session, client=client, number=f"S{i:02d}", billing_rate="60.00")
        for i in range(20)
    ]
    employees = [_make_employee(session, name=f"E{i:03d}") for i in range(100)]

    # Bulk-insert wages and one entry per (employee, site) in as few commits as possible, so the setup
    # does not dominate the test. Each employee works one 8-hour day at each site, spread across the
    # month so no single day crosses the overtime threshold across sites for that employee.
    rows: list[object] = []
    for employee in employees:
        rows.append(
            EmployeeRate(
                employee_id=employee.id,
                hourly_wage=Decimal("35.00"),
                overtime_rate=Decimal("35.00"),
                shabbat_holiday_rate=Decimal("35.00"),
                travel_allowance_daily=Decimal("0"),
                effective_from=date(2025, 1, 1),
                effective_to=None,
            )
        )
    session.add_all(rows)
    session.commit()

    # One distinct weekday per site, skipping Saturdays (Shabbat), so every site's 06:00–14:00 shift
    # is billable regular hours and every site produces a billing record. August 2025 Saturdays are
    # 2, 9, 16, 23, 30; the 20 days below are the first 20 non-Saturday days of the month.
    saturdays = {2, 9, 16, 23, 30}
    site_days = [d for d in range(1, 32) if d not in saturdays][:20]

    entries: list[TimeEntry] = []
    for employee in employees:
        for index, site in enumerate(sites):
            # Each employee works one 8-hour shift per site, each site on its own weekday, 06:00 start.
            day = site_days[index]
            local_in = datetime(2025, 8, day, 6, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
            check_in = local_in.astimezone(UTC)
            entries.append(
                TimeEntry(
                    employee_id=employee.id,
                    site_id=site.id,
                    work_date=date(2025, 8, day),
                    check_in_at=check_in,
                    check_out_at=check_in + timedelta(minutes=480),
                    total_minutes=480,
                    source=TimeEntrySource.QR_SCAN,
                    is_manual=False,
                    status=TimeEntryStatus.APPROVED,
                    flags=[],
                )
            )
    session.add_all(entries)
    session.commit()

    # Compute payroll and billing through the service directly: the 100 employee-months only need to
    # exist for the reports to read, and 100 HTTP round-trips (each re-authenticating) would time the
    # setup, not the reports. The reports themselves are read over HTTP below, which is what 24.2
    # bounds.
    from app.services import billing as billing_service
    from app.services import payroll as payroll_service

    for employee in employees:
        payroll_service.calculate_payroll(session, employee_id=employee.id, year=2025, month=8)
        session.commit()  # one unit of work per employee-month, as the payroll endpoint does
    billing_service.calculate_billing(session, year=2025, month=8)
    session.commit()

    started = time.perf_counter()
    for path in (
        "/api/reports/by-employee?year=2025&month=8",
        "/api/reports/by-site?year=2025&month=8",
        "/api/reports/by-client?year=2025&month=8",
        "/api/reports/profitability?year=2025&month=8",
    ):
        response = reports_client.get(path, headers=headers)
        assert response.status_code == 200, response.text
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0, f"reports took {elapsed:.2f}s, target is under 5s"
    # Sanity: the reports actually returned the full population.
    by_employee = reports_client.get(
        "/api/reports/by-employee?year=2025&month=8", headers=headers
    ).json()
    assert len(by_employee["rows"]) == 100
    by_site = reports_client.get(
        "/api/reports/by-site?year=2025&month=8", headers=headers
    ).json()
    assert len(by_site["rows"]) == 20
