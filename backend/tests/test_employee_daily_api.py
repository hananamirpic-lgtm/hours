"""The employee-daily report over HTTP (feature: employee-daily).

The subject here is the endpoint contract: every console role may read it (it is hours, no money), a
site manager is narrowed to their assigned sites, and the employee (mobile) role is refused. The live
calculation itself is proven in `test_employee_daily_report.py`.
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
from app.models.employee import Employee
from app.models.setting import Setting, SettingValueType
from app.models.site import Site, SiteRate
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.models.user import UserRole
from app.models.user_site import UserSite
from auth_support import DEFAULT_PASSWORD


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
    def _sign_in(role: UserRole, **overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **overrides)
        payload["username"] = user.username
        response = reports_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}, user

    return _sign_in


_PASSPORT = iter(f"E{n:07d}" for n in range(1, 1_000_000))


def _seed_settings(session: Session) -> None:
    for key, value, vtype in [
        ("overtime_daily_threshold_minutes", "480", SettingValueType.INTEGER),
        ("shabbat_start_weekday", "4", SettingValueType.INTEGER),
        ("shabbat_start_time", "16:00", SettingValueType.TIME),
        ("shabbat_end_weekday", "5", SettingValueType.INTEGER),
        ("shabbat_end_time", "20:00", SettingValueType.TIME),
    ]:
        session.add(Setting(key=key, value=value, value_type=vtype))
    session.commit()


def _employee(session: Session, name: str = "Worker") -> Employee:
    passport = next(_PASSPORT)
    e = Employee(
        full_name=name, full_name_en=name, passport_number=passport, passport_number_hash=passport,
        phone="+972500000000", country="Israel", emergency_contact_name="C",
        emergency_contact_phone="+972500000001", start_date=date(2025, 1, 1),
    )
    session.add(e)
    session.commit()
    return e


def _site(session: Session, number: str) -> Site:
    client = Client(name=f"Client {number}")
    session.add(client)
    session.flush()
    s = Site(
        name=f"Site {number}", site_number=number, client_id=client.id,
        qr_token=f"placeholder-{uuid.uuid4().hex}", qr_token_version=1,
    )
    session.add(s)
    session.flush()
    s.rates.append(SiteRate(billing_rate=Decimal("60.00"), overtime_billing_rate=Decimal("90.00"),
                            effective_from=date(2025, 1, 1), effective_to=None))
    session.commit()
    return s


def _entry(session: Session, *, employee: Employee, site: Site, day: int, minutes: int = 120) -> None:
    check_in = datetime(2025, 8, day, 8, 0, tzinfo=ZoneInfo("Asia/Jerusalem")).astimezone(UTC)
    session.add(TimeEntry(
        employee_id=employee.id, site_id=site.id, work_date=date(2025, 8, day),
        check_in_at=check_in, check_out_at=check_in + timedelta(minutes=minutes),
        total_minutes=minutes, source=TimeEntrySource.QR_SCAN, is_manual=False,
        status=TimeEntryStatus.APPROVED, flags=[],
    ))
    session.commit()


def _get(reports_client, headers):
    return reports_client.get(
        "/api/reports/employee-daily", params={"year": 2025, "month": 8}, headers=headers
    )


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.ACCOUNTING])
def test_whole_company_roles_get_the_report_with_no_money(reports_client, sign_in, session, role):
    """Every whole-company console role may read it, and the payload carries no money field."""
    _seed_settings(session)
    headers, _ = sign_in(role)
    emp = _employee(session)
    site = _site(session, "S1")
    _entry(session, employee=emp, site=site, day=4, minutes=120)

    response = _get(reports_client, headers)
    assert response.status_code == 200, response.text
    body = response.json()
    row = next(r for r in body["rows"] if r["employee_id"] == str(emp.id))
    assert row["total_minutes"] == 120
    assert row["approved_minutes"] == 120
    assert row["not_approved_minutes"] == 0
    # No money field anywhere in the payload.
    dumped = response.text
    for money in ("cost", "billing", "wage", "profit", "hourly_rate", "total_pay", "payment"):
        assert money not in dumped


def test_a_site_manager_is_scoped_to_their_sites(reports_client, sign_in, session: Session):
    """A site manager sees only entries at their assigned sites."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    _seed_settings(session)
    emp = _employee(session)
    mine = _site(session, "MINE")
    other = _site(session, "OTHER")
    session.add(UserSite(user_id=manager.id, site_id=mine.id))
    session.commit()
    _entry(session, employee=emp, site=mine, day=4, minutes=120)
    _entry(session, employee=emp, site=other, day=5, minutes=60)

    response = _get(reports_client, manager_headers)
    assert response.status_code == 200, response.text
    row = next(r for r in response.json()["rows"] if r["employee_id"] == str(emp.id))
    assert row["total_minutes"] == 120  # only the managed site


def test_the_employee_role_is_refused(reports_client, sign_in, session: Session):
    """A mobile employee login cannot read a console report."""
    headers, _ = sign_in(UserRole.EMPLOYEE)
    response = _get(reports_client, headers)
    assert response.status_code == 403