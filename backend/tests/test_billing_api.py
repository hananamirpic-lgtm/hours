"""Billing calculation over HTTP and through the service (Requirement 17).

The pure arithmetic is pinned in `test_billing_calculation.py`; here the subject is the wiring, the
guards and the rules that need a database — the claims a request or a real session proves:

* the brief's example end to end: 4.5 h at Site A (60 ₪) and 5.0 h at Site B (75 ₪) bill 645 ₪; with
  the employee paid 35 ₪ the cost is 332.50 ₪ and the profit is 312.50 ₪ (Requirement 17.1, 17.4);
* a mid-month site-rate change is applied per work date (Requirement 17.1);
* only Approved or Locked hours bill, and the excluded unapproved hours are reported (Requirement 17.6);
* profit is billing minus the cost payroll allocated to the site (Requirement 17.4);
* the endpoints are restricted to administrators and accounting (Requirement 2.5, 17.7).

The billing example needs payroll to have run first, so the cost is allocated per site: the tests
calculate payroll for the employee-month, then billing for the month, and read profit back. The
sign-in helper and the seeded settings mirror `test_payroll_api.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import BillingRecord
from app.models.client import Client
from app.models.employee import Employee, EmployeeRate
from app.models.payroll import CalculationStatus
from app.models.setting import Setting, SettingValueType
from app.models.site import Site, SiteRate
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.models.user import UserRole
from auth_support import DEFAULT_PASSWORD

# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def billing_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(billing_client, make_user):
    def _sign_in(role: UserRole, **user_overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **user_overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **user_overrides)
        payload["username"] = user.username

        response = billing_client.post("/api/auth/login", json=payload)
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


_PASSPORT = iter(f"P{n:07d}" for n in range(1, 100000))


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
    session.commit()
    return employee


def _add_wage(
    session: Session,
    employee: Employee,
    *,
    wage: str,
    effective_from: date = date(2025, 1, 1),
    effective_to: date | None = None,
) -> None:
    session.add(
        EmployeeRate(
            employee_id=employee.id,
            hourly_wage=Decimal(wage),
            overtime_rate=Decimal(wage),
            shabbat_holiday_rate=Decimal(wage),
            travel_allowance_daily=Decimal("0"),
            effective_from=effective_from,
            effective_to=effective_to,
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
    overtime_billing_rate: str | None = None,
    effective_from: date = date(2025, 1, 1),
    effective_to: date | None = None,
) -> Site:
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
            overtime_billing_rate=Decimal(overtime_billing_rate) if overtime_billing_rate else None,
            effective_from=effective_from,
            effective_to=effective_to,
        )
    )
    session.commit()
    return site


def _add_site_rate(
    session: Session,
    site: Site,
    *,
    billing_rate: str,
    overtime_billing_rate: str | None = None,
    effective_from: date,
    effective_to: date | None = None,
) -> None:
    site.rates.append(
        SiteRate(
            billing_rate=Decimal(billing_rate),
            overtime_billing_rate=Decimal(overtime_billing_rate) if overtime_billing_rate else None,
            effective_from=effective_from,
            effective_to=effective_to,
        )
    )
    session.commit()


def _make_entry(
    session: Session,
    *,
    employee: Employee,
    site: Site,
    day: int,
    start_hour: int,
    start_minute: int = 0,
    minutes: int,
    status: TimeEntryStatus = TimeEntryStatus.APPROVED,
) -> TimeEntry:
    """A completed entry on a plain August weekday, in the business timezone, clear of Shabbat."""
    local_in = datetime(2025, 8, day, start_hour, start_minute, tzinfo=ZoneInfo("Asia/Jerusalem"))
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


def _calculate_payroll(billing_client, headers, employee: Employee) -> None:
    """Run payroll for the employee-month, so the per-site cost allocations exist for billing."""
    response = billing_client.post(
        "/api/payroll/calculate",
        json={"employee_id": str(employee.id), "year": 2025, "month": 8},
        headers=headers,
    )
    assert response.status_code == 200, response.text


# ===================================================================== the brief's example


def test_the_briefs_example_bills_645_costs_332_50_profits_312_50(
    billing_client, sign_in, session: Session
):
    """Requirement 17.1, 17.4: the brief's two-site day bills 645 ₪, costs 332.50 ₪, profits 312.50 ₪.

    One client, two sites: Site A billed at 60 ₪, Site B at 75 ₪. The employee works 07:00–11:30 at A
    (4.5 h) and 12:00–17:00 at B (5.0 h), paid a flat 35 ₪. Payroll allocates the cost 157.50 at A and
    175.00 at B (332.50 total); billing bills 4.5 × 60 = 270.00 at A and 5.0 × 75 = 375.00 at B
    (645.00 total); profit is 645.00 − 332.50 = 312.50.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_wage(session, employee, wage="35.00")
    client = _make_client(session)
    site_a = _make_site(session, client=client, number="A", billing_rate="60.00")
    site_b = _make_site(session, client=client, number="B", billing_rate="75.00")
    _make_entry(session, employee=employee, site=site_a, day=4, start_hour=7, minutes=270)
    _make_entry(session, employee=employee, site=site_b, day=4, start_hour=12, minutes=300)

    _calculate_payroll(billing_client, headers, employee)

    response = billing_client.post(
        "/api/billing/calculate", json={"year": 2025, "month": 8}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()

    by_site = {s["site_id"]: s for s in body["sites"]}
    assert Decimal(by_site[str(site_a.id)]["billing"]) == Decimal("270.00")
    assert Decimal(by_site[str(site_b.id)]["billing"]) == Decimal("375.00")
    assert Decimal(by_site[str(site_a.id)]["cost"]) == Decimal("157.50")
    assert Decimal(by_site[str(site_b.id)]["cost"]) == Decimal("175.00")

    assert Decimal(body["total_billing"]) == Decimal("645.00")
    assert Decimal(body["total_cost"]) == Decimal("332.50")
    assert Decimal(body["total_profit"]) == Decimal("312.50")

    assert len(body["clients"]) == 1
    assert Decimal(body["clients"][0]["billing"]) == Decimal("645.00")
    assert Decimal(body["clients"][0]["profit"]) == Decimal("312.50")


# ===================================================================== mid-month site-rate change


def test_a_mid_month_site_rate_change_is_applied_per_date(billing_client, sign_in, session: Session):
    """Requirement 17.1: days before and after a site-rate change bill at the rate in force on each.

    Site A bills 60 ₪ up to 15 August and 70 ₪ from 16 August. One 8-hour day on the 10th and one on
    the 20th bill 8 × 60 + 8 × 70 = 480 + 560 = 1,040 ₪, proof the site rate is resolved per date.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_wage(session, employee, wage="35.00")
    client = _make_client(session)
    site = _make_site(
        session, client=client, number="A", billing_rate="60.00", effective_to=date(2025, 8, 15)
    )
    _add_site_rate(session, site, billing_rate="70.00", effective_from=date(2025, 8, 16))
    _make_entry(session, employee=employee, site=site, day=10, start_hour=6, minutes=480)
    _make_entry(session, employee=employee, site=site, day=20, start_hour=6, minutes=480)

    response = billing_client.post(
        "/api/billing/calculate", json={"year": 2025, "month": 8}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert Decimal(response.json()["total_billing"]) == Decimal("1040.00")


# ===================================================================== approved-only + excluded hours


def test_only_approved_hours_bill_and_unapproved_hours_are_reported(
    billing_client, sign_in, session: Session
):
    """Requirement 17.6: Draft/Review hours are excluded from billing and reported by count and minutes.

    One Approved 8-hour day (480 min) and one Draft 5-hour day (300 min) at a 60 ₪ site: only the
    Approved day bills, so the total is 8 × 60 = 480.00, not 780.00. The Draft day is reported as one
    excluded entry of 300 minutes, so the low figure is explained.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_wage(session, employee, wage="35.00")
    client = _make_client(session)
    site = _make_site(session, client=client, number="A", billing_rate="60.00")
    _make_entry(
        session, employee=employee, site=site, day=5, start_hour=6, minutes=480,
        status=TimeEntryStatus.APPROVED,
    )
    _make_entry(
        session, employee=employee, site=site, day=6, start_hour=6, minutes=300,
        status=TimeEntryStatus.DRAFT,
    )

    response = billing_client.post(
        "/api/billing/calculate", json={"year": 2025, "month": 8}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(body["total_billing"]) == Decimal("480.00")
    assert body["excluded_entry_count"] == 1
    assert body["excluded_minutes"] == 300


# ===================================================================== overtime billing rate


def test_overtime_bills_at_the_site_overtime_rate_where_configured(
    billing_client, sign_in, session: Session
):
    """Requirement 17.2: a site's overtime billing rate is used for its overtime hours.

    A single 10-hour day at a site billing 60 ₪ regular and 90 ₪ overtime: the employee's day crosses
    the 8-hour threshold, so 8 h bill at 60 (480.00) and 2 h at 90 (180.00), totalling 660.00.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_wage(session, employee, wage="35.00")
    client = _make_client(session)
    site = _make_site(
        session, client=client, number="A", billing_rate="60.00", overtime_billing_rate="90.00"
    )
    # 10 hours = 600 minutes on one day → 480 regular + 120 overtime.
    _make_entry(session, employee=employee, site=site, day=5, start_hour=6, minutes=600)

    response = billing_client.post(
        "/api/billing/calculate", json={"year": 2025, "month": 8}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(body["total_billing"]) == Decimal("660.00")
    site_row = body["sites"][0]
    assert site_row["regular_minutes"] == 480
    assert site_row["overtime_minutes"] == 120
    assert Decimal(site_row["overtime_rate_applied"]) == Decimal("90.00")


# ===================================================================== recalculation


def test_recalculation_replaces_the_drafts_rather_than_duplicating(
    billing_client, sign_in, session: Session
):
    """A second calculation overwrites the site's record, not appends a second one.

    The `(site_id, year, month)` unique constraint makes each recalculation an upsert per site.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_wage(session, employee, wage="35.00")
    client = _make_client(session)
    site = _make_site(session, client=client, number="A", billing_rate="60.00")
    _make_entry(session, employee=employee, site=site, day=5, start_hour=6, minutes=480)

    first = billing_client.post(
        "/api/billing/calculate", json={"year": 2025, "month": 8}, headers=headers
    )
    assert first.status_code == 200, first.text
    assert Decimal(first.json()["total_billing"]) == Decimal("480.00")

    # A second approved day is added, then billing is recalculated.
    _make_entry(session, employee=employee, site=site, day=6, start_hour=6, minutes=480)
    second = billing_client.post(
        "/api/billing/calculate", json={"year": 2025, "month": 8}, headers=headers
    )
    assert second.status_code == 200, second.text
    assert Decimal(second.json()["total_billing"]) == Decimal("960.00")

    records = session.scalars(
        select(BillingRecord).where(BillingRecord.site_id == site.id)
    ).all()
    assert len(records) == 1
    assert records[0].total_amount == Decimal("960.00")


# ===================================================================== locked month is final


def test_a_locked_month_is_marked_final(billing_client, sign_in, session: Session):
    """Requirement 15.4: a billing record for a locked month is final, not a draft."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_wage(session, employee, wage="35.00")
    client = _make_client(session)
    site = _make_site(session, client=client, number="A", billing_rate="60.00")
    _make_entry(session, employee=employee, site=site, day=5, start_hour=6, minutes=480)
    billing_client.post("/api/periods/2025/8/lock", json={"force": True}, headers=headers)

    response = billing_client.post(
        "/api/billing/calculate", json={"year": 2025, "month": 8}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["sites"][0]["status"] == CalculationStatus.FINAL.value


# ===================================================================== reads


def test_get_returns_the_stored_billing_for_a_period(billing_client, sign_in, session: Session):
    """Requirement 17.3: the read returns a period's stored billing per site and per client."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    accounting_headers, _ = sign_in(UserRole.ACCOUNTING)
    employee = _make_employee(session)
    _add_wage(session, employee, wage="35.00")
    client = _make_client(session)
    site = _make_site(session, client=client, number="A", billing_rate="60.00")
    _make_entry(session, employee=employee, site=site, day=5, start_hour=6, minutes=480)
    billing_client.post(
        "/api/billing/calculate", json={"year": 2025, "month": 8}, headers=admin_headers
    )

    response = billing_client.get("/api/billing?year=2025&month=8", headers=accounting_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["sites"]) == 1
    assert body["sites"][0]["site_id"] == str(site.id)
    assert Decimal(body["total_billing"]) == Decimal("480.00")


# ===================================================================== authorization


def test_calculate_is_finance_only(billing_client, sign_in, session: Session):
    """Requirement 2.5, 17.7: a site manager may not calculate billing — it is billing data."""
    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)

    response = billing_client.post(
        "/api/billing/calculate", json={"year": 2025, "month": 8}, headers=manager_headers
    )
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_read_is_finance_only(billing_client, sign_in, session: Session):
    """Requirement 2.5, 17.7: a site manager may not read billing."""
    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)

    response = billing_client.get("/api/billing?year=2025&month=8", headers=manager_headers)
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_accounting_may_calculate(billing_client, sign_in, session: Session):
    """Requirement 2.6: accounting may calculate billing."""
    headers, _ = sign_in(UserRole.ACCOUNTING)
    employee = _make_employee(session)
    _add_wage(session, employee, wage="35.00")
    client = _make_client(session)
    site = _make_site(session, client=client, number="A", billing_rate="60.00")
    _make_entry(session, employee=employee, site=site, day=5, start_hour=6, minutes=480)

    response = billing_client.post(
        "/api/billing/calculate", json={"year": 2025, "month": 8}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert Decimal(response.json()["total_billing"]) == Decimal("480.00")
