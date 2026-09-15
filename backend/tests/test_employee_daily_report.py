"""The live employee-daily hours report (feature: employee-daily), tested at the service level.

These assert the correctness properties without HTTP or payroll: the report reads straight from time
entries, so the tests build entries with chosen statuses, an open shift, and a two-shift day with a
travel gap, and check the total/approved/not-approved split, the open-shift-to-now behaviour, the
travel match against `classify_day`, and the site-scope narrowing. The API-level checks (role access,
scope over HTTP) live in `test_employee_daily_api.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.authz import SiteScope
from app.models.client import Client
from app.models.employee import Employee
from app.models.setting import Setting, SettingValueType
from app.models.site import Site, SiteRate
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.services import reports as reports_service

_YEAR = 2025
_MONTH = 8
_TZ = "Asia/Jerusalem"


def _seed_settings(session: Session) -> None:
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


_PASSPORT = iter(f"D{n:07d}" for n in range(1, 1_000_000))


def _employee(session: Session, *, name: str = "Worker") -> Employee:
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


def _site(session: Session, *, number: str) -> Site:
    client = Client(name=f"Client {number}")
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
    session.flush()
    site.rates.append(
        SiteRate(
            billing_rate=Decimal("60.00"),
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
    day: int,
    start_hour: int = 8,
    minutes: int | None = 120,
    start_minute: int = 0,
    status: TimeEntryStatus = TimeEntryStatus.APPROVED,
) -> TimeEntry:
    from zoneinfo import ZoneInfo

    local_in = datetime(_YEAR, _MONTH, day, start_hour, start_minute, tzinfo=ZoneInfo(_TZ))
    check_in = local_in.astimezone(UTC)
    check_out = None if minutes is None else check_in + timedelta(minutes=minutes)
    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=date(_YEAR, _MONTH, day),
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


def _row(report, employee_id):
    return next(r for r in report.rows if r.employee_id == employee_id)


def _now() -> datetime:
    return datetime(_YEAR, _MONTH, 28, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- properties


def test_total_equals_approved_plus_not_approved(session: Session):
    """Property 1: total == approved + not-approved for every row."""
    _seed_settings(session)
    emp = _employee(session)
    site = _site(session, number="S1")
    _entry(session, employee=emp, site=site, day=4, minutes=120, status=TimeEntryStatus.APPROVED)
    _entry(session, employee=emp, site=site, day=6, minutes=60, status=TimeEntryStatus.DRAFT)

    report = reports_service.report_employee_daily(
        session, year=_YEAR, month=_MONTH, scope=SiteScope.all_sites(), now=_now()
    )
    row = _row(report, emp.id)
    assert row.total_minutes == row.approved_minutes + row.not_approved_minutes
    assert row.approved_minutes == 120
    assert row.not_approved_minutes == 60
    assert row.total_minutes == 180


def test_status_buckets(session: Session):
    """Property 5: approved+locked -> approved; draft+review -> not-approved."""
    _seed_settings(session)
    emp = _employee(session)
    site = _site(session, number="S1")
    _entry(session, employee=emp, site=site, day=4, minutes=60, status=TimeEntryStatus.APPROVED)
    _entry(session, employee=emp, site=site, day=5, minutes=60, status=TimeEntryStatus.LOCKED)
    _entry(session, employee=emp, site=site, day=6, minutes=30, status=TimeEntryStatus.DRAFT)
    _entry(session, employee=emp, site=site, day=7, minutes=30, status=TimeEntryStatus.REVIEW)

    report = reports_service.report_employee_daily(
        session, year=_YEAR, month=_MONTH, scope=SiteScope.all_sites(), now=_now()
    )
    row = _row(report, emp.id)
    assert row.approved_minutes == 120  # approved + locked
    assert row.not_approved_minutes == 60  # draft + review


def test_live_without_payroll(session: Session):
    """Property 2: rows are produced with no payroll/billing record and an unlocked month."""
    _seed_settings(session)
    emp = _employee(session)
    site = _site(session, number="S1")
    _entry(session, employee=emp, site=site, day=4, minutes=120, status=TimeEntryStatus.DRAFT)

    report = reports_service.report_employee_daily(
        session, year=_YEAR, month=_MONTH, scope=SiteScope.all_sites(), now=_now()
    )
    assert _row(report, emp.id).total_minutes == 120


def test_open_shift_counted_to_now(session: Session):
    """Property 3: an open shift contributes minutes to now, and a later now yields more."""
    _seed_settings(session)
    emp = _employee(session)
    site = _site(session, number="S1")
    # Open shift starting 08:00 local on day 10.
    _entry(session, employee=emp, site=site, day=10, start_hour=8, minutes=None, status=TimeEntryStatus.DRAFT)

    from zoneinfo import ZoneInfo

    two_hours_in = datetime(_YEAR, _MONTH, 10, 10, 0, tzinfo=ZoneInfo(_TZ)).astimezone(UTC)
    three_hours_in = datetime(_YEAR, _MONTH, 10, 11, 0, tzinfo=ZoneInfo(_TZ)).astimezone(UTC)

    earlier = reports_service.report_employee_daily(
        session, year=_YEAR, month=_MONTH, scope=SiteScope.all_sites(), now=two_hours_in
    )
    later = reports_service.report_employee_daily(
        session, year=_YEAR, month=_MONTH, scope=SiteScope.all_sites(), now=three_hours_in
    )
    assert _row(earlier, emp.id).total_minutes == 120
    assert _row(later, emp.id).total_minutes == 180


def test_travel_time_matches_classify_day(session: Session):
    """Property 4: a two-shift day with a <=30-min gap folds in travel exactly as classify_day does."""
    _seed_settings(session)
    emp = _employee(session)
    site = _site(session, number="S1")
    # 08:00-11:30 (210 min), gap 30 min, 12:00-17:00 (300 min). Travel 30 split 15/15 -> +30 total.
    _entry(session, employee=emp, site=site, day=4, start_hour=8, start_minute=0, minutes=210)
    _entry(session, employee=emp, site=site, day=4, start_hour=12, start_minute=0, minutes=300)

    report = reports_service.report_employee_daily(
        session, year=_YEAR, month=_MONTH, scope=SiteScope.all_sites(), now=_now()
    )
    # 210 + 300 clocked = 510, plus 30 travel = 540.
    assert _row(report, emp.id).total_minutes == 540


def test_site_scope_narrows_to_managers_sites(session: Session):
    """Property 6: a restricted scope counts only the caller's sites; all-sites counts everything."""
    _seed_settings(session)
    emp = _employee(session)
    mine = _site(session, number="MINE")
    other = _site(session, number="OTHER")
    _entry(session, employee=emp, site=mine, day=4, minutes=120)
    _entry(session, employee=emp, site=other, day=5, minutes=60)

    scoped = reports_service.report_employee_daily(
        session, year=_YEAR, month=_MONTH, scope=SiteScope.limited_to([mine.id]), now=_now()
    )
    assert _row(scoped, emp.id).total_minutes == 120  # only the mine site

    everything = reports_service.report_employee_daily(
        session, year=_YEAR, month=_MONTH, scope=SiteScope.all_sites(), now=_now()
    )
    assert _row(everything, emp.id).total_minutes == 180  # both sites


def test_empty_scope_yields_no_rows(session: Session):
    """A manager with no assigned sites sees nothing, not everything."""
    _seed_settings(session)
    emp = _employee(session)
    site = _site(session, number="S1")
    _entry(session, employee=emp, site=site, day=4, minutes=120)

    report = reports_service.report_employee_daily(
        session, year=_YEAR, month=_MONTH, scope=SiteScope.nothing(), now=_now()
    )
    assert report.rows == ()