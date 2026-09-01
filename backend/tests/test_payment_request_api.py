"""Per-client payment request over HTTP (Requirement 18.8, 17.3).

The payment request is the client's invoice backing: one section per billed site, broken into the
employees who worked it with their hours, the site rate and the amount, plus the client total. It is
derived from the stored `billing_records`, so the claim the task asks these tests to prove is
reconciliation — the per-site amount equals the billing record for that site, and the client total
equals the sum of the billing records for the same period:

* the brief's two-site day: Site A bills 270.00 ₪ and Site B 375.00 ₪, so the request totals 645.00 ₪
  and each site section's amount equals its `billing_records.total_amount`;
* a site worked by two employees splits the site's billed total across them so the lines sum to the
  record exactly (Requirement 17.3), never a re-rounding that drifts;
* an unknown client is a 404; a manager may not read a payment request (Requirement 17.7).

The fixtures mirror `test_billing_api.py`: billing must be calculated first (which needs payroll for
the cost), then the payment request is read back.
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


_PASSPORT = iter(f"PR{n:06d}" for n in range(1, 100000))


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


def _make_site(
    session: Session,
    *,
    client: Client,
    number: str,
    billing_rate: str,
    overtime_billing_rate: str | None = None,
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


def _calculate_payroll(billing_client, headers, employee: Employee) -> None:
    response = billing_client.post(
        "/api/payroll/calculate",
        json={"employee_id": str(employee.id), "year": 2025, "month": 8},
        headers=headers,
    )
    assert response.status_code == 200, response.text


def _calculate_billing(billing_client, headers) -> None:
    response = billing_client.post("/api/billing/calculate", json={"year": 2025, "month": 8}, headers=headers)
    assert response.status_code == 200, response.text


# ===================================================================== the brief's example


def test_payment_request_totals_match_the_billing_records(billing_client, sign_in, session: Session):
    """Requirement 18.8, 17.3: each site amount and the client total equal the billing records.

    The brief's two-site day: Site A bills 4.5 h × 60 = 270.00 and Site B 5.0 h × 75 = 375.00. The
    payment request's per-site amounts must equal the stored `billing_records.total_amount` for those
    sites, and the client total must equal their sum, 645.00.
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
    _calculate_billing(billing_client, headers)

    response = billing_client.get(
        f"/api/billing/clients/{client.id}/payment-request?year=2025&month=8", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # The client and period are named on the request.
    assert body["client_id"] == str(client.id)
    assert body["client_name"] == "Acme"
    assert body["year"] == 2025
    assert body["month"] == 8

    # Each site section's amount equals that site's billing record.
    records = {
        record.site_id: record
        for record in session.scalars(
            select(BillingRecord).where(BillingRecord.year == 2025, BillingRecord.month == 8)
        )
    }
    by_site = {s["site_id"]: s for s in body["sites"]}
    assert Decimal(by_site[str(site_a.id)]["amount"]) == records[site_a.id].total_amount
    assert Decimal(by_site[str(site_b.id)]["amount"]) == records[site_b.id].total_amount
    assert Decimal(by_site[str(site_a.id)]["amount"]) == Decimal("270.00")
    assert Decimal(by_site[str(site_b.id)]["amount"]) == Decimal("375.00")

    # The client total equals the sum of the billing records for the period.
    records_total = sum((r.total_amount for r in records.values()), Decimal("0.00"))
    assert Decimal(body["total_amount"]) == records_total
    assert Decimal(body["total_amount"]) == Decimal("645.00")

    # Each site section carries the employee line with hours, rate and amount (Requirement 18.8).
    site_a_line = by_site[str(site_a.id)]["lines"][0]
    assert site_a_line["employee_id"] == str(employee.id)
    assert site_a_line["total_minutes"] == 270
    assert Decimal(site_a_line["hours"]) == Decimal("4.50")
    assert Decimal(site_a_line["rate"]) == Decimal("60.00")
    assert Decimal(site_a_line["amount"]) == Decimal("270.00")


# ===================================================================== multi-employee site reconciles


def test_a_site_worked_by_two_employees_splits_to_the_record_total(billing_client, sign_in, session: Session):
    """Requirement 17.3: two employees' line amounts sum to the site's billed total exactly.

    Both work an odd shift at the same 60 ₪ site — 07:00–07:50 (50 min) and 08:00–08:50 (50 min) — so
    the exact per-employee billing is 50 ⁄ 60 × 60 = 50.00 each, summing cleanly; the test's point is
    that whatever rounding the split does, the two line amounts add up to the site record to the agora.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    alice = _make_employee(session, name="Alice")
    bob = _make_employee(session, name="Bob")
    _add_wage(session, alice, wage="35.00")
    _add_wage(session, bob, wage="35.00")
    client = _make_client(session)
    site = _make_site(session, client=client, number="A", billing_rate="60.00")
    # Non-overlapping shifts of 50 minutes each, same day, same site.
    _make_entry(session, employee=alice, site=site, day=5, start_hour=7, minutes=50)
    _make_entry(session, employee=bob, site=site, day=5, start_hour=8, minutes=50)

    _calculate_payroll(billing_client, headers, alice)
    _calculate_payroll(billing_client, headers, bob)
    _calculate_billing(billing_client, headers)

    response = billing_client.get(
        f"/api/billing/clients/{client.id}/payment-request?year=2025&month=8", headers=headers
    )
    assert response.status_code == 200, response.text
    section = response.json()["sites"][0]

    record = session.scalars(select(BillingRecord).where(BillingRecord.site_id == site.id)).one()
    line_sum = sum((Decimal(line["amount"]) for line in section["lines"]), Decimal("0.00"))
    assert len(section["lines"]) == 2
    assert line_sum == record.total_amount
    assert Decimal(section["amount"]) == record.total_amount


# ===================================================================== empty period


def test_a_client_with_no_billing_yields_an_empty_request(billing_client, sign_in, session: Session):
    """A client with no billing records for the period gets an empty request, not an error."""
    headers, _ = sign_in(UserRole.ADMIN)
    client = _make_client(session)

    response = billing_client.get(
        f"/api/billing/clients/{client.id}/payment-request?year=2025&month=8", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sites"] == []
    assert Decimal(body["total_amount"]) == Decimal("0.00")


# ===================================================================== authorization / not found


def test_unknown_client_is_not_found(billing_client, sign_in, session: Session):
    """An unknown client id on the payment-request path is a 404."""
    headers, _ = sign_in(UserRole.ADMIN)

    response = billing_client.get(
        f"/api/billing/clients/{uuid.uuid4()}/payment-request?year=2025&month=8", headers=headers
    )
    assert response.status_code == 404
    assert _code(response) == "client_not_found"


def test_payment_request_is_finance_only(billing_client, sign_in, session: Session):
    """Requirement 17.7: a site manager may not read a client's payment request — it is billing data."""
    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)
    client = _make_client(session)

    response = billing_client.get(
        f"/api/billing/clients/{client.id}/payment-request?year=2025&month=8",
        headers=manager_headers,
    )
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"
