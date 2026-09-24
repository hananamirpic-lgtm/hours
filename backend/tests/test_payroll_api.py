"""Payroll calculation over HTTP and through the service (Requirement 16, 15.8).

The pure arithmetic is pinned in `test_payroll_calculation.py`; here the subject is the wiring, the
guards and the rules that need a database — the claims a request or a real session proves:

* only Approved or Locked entries feed a calculation, and Draft/Review are excluded (Requirement 15.8);
* a recalculation replaces the existing draft rather than duplicating it (Requirement 16.10);
* the per-site allocation is persisted and its costs sum to the worked pay (Requirement 16.8);
* a mid-month rate change is applied per work date end to end (Requirement 16.9);
* the endpoints are restricted to administrators and accounting (Requirement 2.5, 2.6).

The sign-in helper mirrors `test_period_api.py`: an admin enrols 2FA, others do not. The settings the
classification reads are seeded per test, since the SQLite unit-test schema carries no seed data.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.employee import Employee, EmployeeRate
from app.models.payroll import CalculationStatus, PayrollRecord
from app.models.setting import Setting, SettingValueType
from app.models.site import Site
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.models.user import UserRole
from auth_support import DEFAULT_PASSWORD

# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def payroll_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(payroll_client, make_user):
    def _sign_in(role: UserRole, **user_overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **user_overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **user_overrides)
        payload["username"] = user.username

        response = payroll_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        header = {"Authorization": f"Bearer {response.json()['access_token']}"}
        return header, user

    return _sign_in


@pytest.fixture(autouse=True)
def _seed_settings(session: Session) -> None:
    """The classification settings the payroll service reads (SQLite carries no seed data)."""
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


def _add_rate(
    session: Session,
    employee: Employee,
    *,
    wage: str,
    effective_from: date,
    effective_to: date | None = None,
    travel: str = "0",
) -> None:
    session.add(
        EmployeeRate(
            employee_id=employee.id,
            hourly_wage=Decimal(wage),
            overtime_rate=Decimal(wage),
            shabbat_holiday_rate=Decimal(wage),
            travel_allowance_daily=Decimal(travel),
            effective_from=effective_from,
            effective_to=effective_to,
        )
    )
    session.commit()


def _make_site(session: Session, *, number: str = "S-1") -> Site:
    client = Client(name="Acme")
    session.add(client)
    session.flush()
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
    """A completed entry on a plain August weekday, in the business timezone, clear of Shabbat.

    Times are built in Jerusalem local time and stored as UTC, matching how a real scan is stored, so
    the classification reads the same local wall-clock the day was worked in.
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    local_in = datetime(2025, 8, day, start_hour, tzinfo=ZoneInfo("Asia/Jerusalem"))
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


# ===================================================================== the named figures


def test_a_flat_month_of_212_hours_at_35_totals_7420(payroll_client, sign_in, session: Session):
    """Requirement 16.5: 212 h at 35 ₪ is 7,420.00 ₪ end to end.

    The rate card is flat (regular = overtime = Shabbat = 35), so however the 212 hours split across
    the four buckets — some of these August days cross a Friday-evening Shabbat window — the pay is
    212 × 35 = 7,420.00. The month totals 12,720 minutes across the buckets, and the total pay is the
    named figure regardless of the split.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_rate(session, employee, wage="35.00", effective_from=date(2025, 1, 1))
    site = _make_site(session)
    # 20 days × 636 minutes = 12,720 minutes = 212 hours.
    for day in range(1, 21):
        _make_entry(session, employee=employee, site=site, day=day, start_hour=6, minutes=636)

    response = payroll_client.post(
        "/api/payroll/calculate",
        json={"employee_id": str(employee.id), "year": 2025, "month": 8},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    total_minutes = (
        body["regular_minutes"]
        + body["overtime_minutes"]
        + body["shabbat_minutes"]
        + body["holiday_minutes"]
    )
    assert total_minutes == 12_720
    assert Decimal(body["total_pay"]) == Decimal("7420.00")


def test_the_briefs_two_site_day_allocates_157_50_and_175_00(payroll_client, sign_in, session: Session):
    """Requirement 16.8 with the travel-time split: the 11:30->12:00 gap is exactly 30 minutes, at the
    travel cap, so it is paid and split 15/15 between the two sites. Site A becomes 4.75 h and Site B
    5.25 h; at the flat 35 ₪ rate that is 166.25 / 183.75, summing to the worked pay 350.00 (the day's
    9.5 clocked hours plus the half-hour of travel).
    """
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_rate(session, employee, wage="35.00", effective_from=date(2025, 1, 1))
    site_a = _make_site(session, number="A")
    site_b = _make_site(session, number="B")
    # Site A 07:00–11:30 (270 min), Site B 12:00–17:00 (300 min), same plain weekday.
    _make_entry(session, employee=employee, site=site_a, day=4, start_hour=7, minutes=270)
    _make_entry(session, employee=employee, site=site_b, day=4, start_hour=12, minutes=300)

    response = payroll_client.post(
        "/api/payroll/calculate",
        json={"employee_id": str(employee.id), "year": 2025, "month": 8},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(body["total_pay"]) == Decimal("350.00")
    by_site = {alloc["site_id"]: Decimal(alloc["cost"]) for alloc in body["allocations"]}
    assert by_site[str(site_a.id)] == Decimal("166.25")
    assert by_site[str(site_b.id)] == Decimal("183.75")
    assert sum(by_site.values()) == Decimal("350.00")


def test_allocations_sum_exactly_to_the_worked_pay(payroll_client, sign_in, session: Session):
    """Requirement 16.8: the persisted per-site costs add back to the record's worked pay to the agora."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_rate(session, employee, wage="35.00", effective_from=date(2025, 1, 1))
    site_a = _make_site(session, number="A")
    site_b = _make_site(session, number="B")
    _make_entry(session, employee=employee, site=site_a, day=4, start_hour=7, minutes=270)
    _make_entry(session, employee=employee, site=site_b, day=4, start_hour=12, minutes=300)

    payroll_client.post(
        "/api/payroll/calculate",
        json={"employee_id": str(employee.id), "year": 2025, "month": 8},
        headers=headers,
    )
    record = session.scalars(select(PayrollRecord)).one()
    worked = record.regular_pay + record.overtime_pay + record.shabbat_pay + record.holiday_pay
    allocated = sum((alloc.cost for alloc in record.allocations), Decimal("0.00"))
    assert allocated == worked


# ===================================================================== mid-month rate change


def test_a_mid_month_rate_change_is_applied_per_date(payroll_client, sign_in, session: Session):
    """Requirement 16.9: days before and after a rate change carry the rate in force on each date.

    35 ₪ up to 15 August, 40 ₪ from 16 August. One 8-hour day on the 10th and one on the 20th give
    8 × 35 + 8 × 40 = 280 + 320 = 600 ₪, proof the rate is resolved per work date.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_rate(
        session, employee, wage="35.00", effective_from=date(2025, 1, 1), effective_to=date(2025, 8, 15)
    )
    _add_rate(session, employee, wage="40.00", effective_from=date(2025, 8, 16))
    site = _make_site(session)
    _make_entry(session, employee=employee, site=site, day=10, start_hour=6, minutes=480)
    _make_entry(session, employee=employee, site=site, day=20, start_hour=6, minutes=480)

    response = payroll_client.post(
        "/api/payroll/calculate",
        json={"employee_id": str(employee.id), "year": 2025, "month": 8},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert Decimal(response.json()["total_pay"]) == Decimal("600.00")


# ===================================================================== approved/locked only


def test_only_approved_and_locked_entries_feed_payroll(payroll_client, sign_in, session: Session):
    """Requirement 15.8: Draft and Review entries are excluded from the calculation.

    An Approved 8-hour day and a Draft 8-hour day: only the Approved one is paid, so the total is
    8 × 35 = 280 ₪, not 560 ₪.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_rate(session, employee, wage="35.00", effective_from=date(2025, 1, 1))
    site = _make_site(session)
    _make_entry(
        session, employee=employee, site=site, day=5, start_hour=6, minutes=480,
        status=TimeEntryStatus.APPROVED,
    )
    _make_entry(
        session, employee=employee, site=site, day=6, start_hour=6, minutes=480,
        status=TimeEntryStatus.DRAFT,
    )

    response = payroll_client.post(
        "/api/payroll/calculate",
        json={"employee_id": str(employee.id), "year": 2025, "month": 8},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert Decimal(response.json()["total_pay"]) == Decimal("280.00")


# ===================================================================== idempotent recalculation


def test_recalculation_replaces_the_draft_rather_than_duplicating(
    payroll_client, sign_in, session: Session
):
    """Requirement 16.10: a second calculation overwrites the record, not appends a second one."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_rate(session, employee, wage="35.00", effective_from=date(2025, 1, 1))
    site = _make_site(session)
    entry = _make_entry(session, employee=employee, site=site, day=5, start_hour=6, minutes=480)

    body = {"employee_id": str(employee.id), "year": 2025, "month": 8}
    first = payroll_client.post("/api/payroll/calculate", json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert Decimal(first.json()["total_pay"]) == Decimal("280.00")

    # A second approved day is added, then payroll is recalculated.
    _make_entry(session, employee=employee, site=site, day=6, start_hour=6, minutes=480)
    second = payroll_client.post("/api/payroll/calculate", json=body, headers=headers)
    assert second.status_code == 200, second.text
    assert Decimal(second.json()["total_pay"]) == Decimal("560.00")

    # Exactly one record for the employee-month, with the recalculated figure.
    records = session.scalars(
        select(PayrollRecord).where(PayrollRecord.employee_id == employee.id)
    ).all()
    assert len(records) == 1
    assert records[0].total_pay == Decimal("560.00")
    assert records[0].id == uuid.UUID(first.json()["id"])
    assert entry is not None


def test_recalculation_is_stable_when_nothing_changed(payroll_client, sign_in, session: Session):
    """Requirement 16.10: recalculating an unchanged month yields the identical figures."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_rate(session, employee, wage="35.00", effective_from=date(2025, 1, 1))
    site = _make_site(session)
    _make_entry(session, employee=employee, site=site, day=5, start_hour=6, minutes=480)

    body = {"employee_id": str(employee.id), "year": 2025, "month": 8}
    first = payroll_client.post("/api/payroll/calculate", json=body, headers=headers).json()
    second = payroll_client.post("/api/payroll/calculate", json=body, headers=headers).json()

    assert first["id"] == second["id"]
    assert first["total_pay"] == second["total_pay"]
    assert first["regular_minutes"] == second["regular_minutes"]


# ===================================================================== reads


def test_get_one_returns_the_record_with_allocations(payroll_client, sign_in, session: Session):
    """Requirement 16.6, 16.8: the single-record read returns the record and its per-site allocation."""
    headers, _ = sign_in(UserRole.ACCOUNTING)
    admin_headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_rate(session, employee, wage="35.00", effective_from=date(2025, 1, 1))
    site = _make_site(session)
    _make_entry(session, employee=employee, site=site, day=5, start_hour=6, minutes=480)
    payroll_client.post(
        "/api/payroll/calculate",
        json={"employee_id": str(employee.id), "year": 2025, "month": 8},
        headers=admin_headers,
    )

    response = payroll_client.get(f"/api/payroll/{employee.id}/2025/8", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(body["total_pay"]) == Decimal("280.00")
    assert len(body["allocations"]) == 1
    assert body["allocations"][0]["site_id"] == str(site.id)


def test_get_one_missing_record_is_404(payroll_client, sign_in, session: Session):
    """A month with no calculated record is a not-found."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)

    response = payroll_client.get(f"/api/payroll/{employee.id}/2025/8", headers=headers)
    assert response.status_code == 404
    assert _code(response) == "payroll_record_not_found"


def test_list_returns_records_for_a_period(payroll_client, sign_in, session: Session):
    """Requirement 16.6: the list returns a period's records."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    accounting_headers, _ = sign_in(UserRole.ACCOUNTING)
    employee = _make_employee(session)
    _add_rate(session, employee, wage="35.00", effective_from=date(2025, 1, 1))
    site = _make_site(session)
    _make_entry(session, employee=employee, site=site, day=5, start_hour=6, minutes=480)
    payroll_client.post(
        "/api/payroll/calculate",
        json={"employee_id": str(employee.id), "year": 2025, "month": 8},
        headers=admin_headers,
    )

    response = payroll_client.get("/api/payroll?year=2025&month=8", headers=accounting_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["employee_id"] == str(employee.id)


def test_a_locked_month_is_marked_final(payroll_client, sign_in, session: Session):
    """Requirement 15.4, 16.6: a record for a locked month is final, not a draft."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _add_rate(session, employee, wage="35.00", effective_from=date(2025, 1, 1))
    site = _make_site(session)
    _make_entry(session, employee=employee, site=site, day=5, start_hour=6, minutes=480)
    # Lock August so the record is computed as final.
    payroll_client.post("/api/periods/2025/8/lock", json={"force": True}, headers=headers)

    response = payroll_client.post(
        "/api/payroll/calculate",
        json={"employee_id": str(employee.id), "year": 2025, "month": 8},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == CalculationStatus.FINAL.value


# ===================================================================== authorization


def test_calculate_is_finance_only(payroll_client, sign_in, session: Session):
    """Requirement 2.5, 2.6: a site manager may not calculate payroll — it is wage data."""
    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)

    response = payroll_client.post(
        "/api/payroll/calculate",
        json={"employee_id": str(employee.id), "year": 2025, "month": 8},
        headers=manager_headers,
    )
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_read_is_finance_only(payroll_client, sign_in, session: Session):
    """Requirement 2.5, 2.6: a site manager may not read payroll records."""
    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)

    listed = payroll_client.get("/api/payroll", headers=manager_headers)
    assert listed.status_code == 403
    assert _code(listed) == "insufficient_role"

    one = payroll_client.get(f"/api/payroll/{employee.id}/2025/8", headers=manager_headers)
    assert one.status_code == 403


def test_accounting_may_calculate(payroll_client, sign_in, session: Session):
    """Requirement 2.6: accounting may calculate payroll."""
    headers, _ = sign_in(UserRole.ACCOUNTING)
    employee = _make_employee(session)
    _add_rate(session, employee, wage="35.00", effective_from=date(2025, 1, 1))
    site = _make_site(session)
    _make_entry(session, employee=employee, site=site, day=5, start_hour=6, minutes=480)

    response = payroll_client.post(
        "/api/payroll/calculate",
        json={"employee_id": str(employee.id), "year": 2025, "month": 8},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert Decimal(response.json()["total_pay"]) == Decimal("280.00")
