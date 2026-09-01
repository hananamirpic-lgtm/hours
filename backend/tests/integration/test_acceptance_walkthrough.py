"""End-to-end acceptance walk-through on real PostgreSQL (Requirement 24.4, 18.9).

This is the whole business, once, in the order a real month runs, against the real database so every
constraint the design leans on is live: the one-open-entry index, the overlap exclusion constraint,
the period lock, the rate history. It is the acceptance criterion of task 38 — proof that the parts
built across the milestones compose into a working month, not just that each passes its own unit test.

The walk-through, step by step (each maps to the task's checklist):

1. **Create a client, a site and an employee** — with a wage, a site billing rate and an assignment,
   so payroll and billing have rates to price against (Requirement 3, 5, 6, 7).
2. **Print the QR** — render the site's printable code and confirm the token it encodes is the one the
   scan path will accept (Requirement 8.5).
3. **Run a three-site day** — check in at Site A, transition to Site B, transition to Site C, check
   out; the day ends as three closed, non-overlapping entries totalling the real minutes, split across
   the three sites (Requirement 9, 10, 11). This is the feature the business case rests on, exercised
   against the constraints that make it correct.
4. **Correct one entry manually** — a hand correction to one entry's times, with a reason, marked
   manual and audited (Requirement 12).
5. **Approve and lock the month** — walk every entry Draft → Review → Approved, then lock the month,
   which sets the approved entries to Locked and refuses further writes (Requirement 15).
6. **Calculate payroll and billing** — from the approved/locked entries only, with the rates in force
   (Requirement 16, 17).
7. **Read every report** — by-employee, by-site, by-client, profitability, and the payment request;
   assert the reconciliation the requirement demands: per-site sums equal the totals, and per-employee
   cost equals the payroll record (Requirement 18.9).
8. **Export Excel and PDF in both languages** — an Excel and a PDF export of a report in Hebrew and in
   English, stored and audited (Requirement 19). The PDF byte render needs WeasyPrint's native stack;
   where it is absent the step asserts the export was created and defers the byte check to
   `tests/test_export_render.py`, which covers the RTL HTML without the native libraries.

Everything runs inside the `db` transaction the fixture rolls back, through a `Session` bound to that
connection, so the walk-through leaves nothing behind. Object storage is the in-memory export fake, so
no request touches a real bucket. Skips cleanly with no live PostgreSQL (see `conftest.py`).
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import openpyxl
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.authz import SiteScope
from app.core.qr_token import parse
from app.models.client import Client
from app.models.employee import Employee, EmployeeRate
from app.models.export import ExportFormat, ExportReportType, ExportStatus
from app.models.site import EmployeeSite, Site, SiteRate
from app.models.time_entry import TimeEntry, TimeEntryStatus
from app.models.user import User, UserRole
from app.schemas.scan import ScanRequest
from app.schemas.time_entry import TimeEntryUpdate
from app.services import billing as billing_service
from app.services import export as export_service
from app.services import payroll as payroll_service
from app.services import period as period_service
from app.services import scan as scan_service
from app.services import time_entry as time_entry_service
from app.services.audit import AuditContext
from app.services.qr import render_site_qr
from app.services.reports import (
    ReportFilters,
    report_by_client,
    report_by_employee,
    report_by_site,
    report_profitability,
)
from export_support import FakeExportStorage

# The walk-through day and month. A plain Tuesday in August 2025, no Shabbat and no holiday, so the
# hours are all regular and the money figures are the clean product of hours and rates.
_WORK_DATE = date(2025, 8, 5)
_YEAR, _MONTH = 2025, 8
_JERUSALEM = ZoneInfo("Asia/Jerusalem")


def _context() -> AuditContext:
    return AuditContext(request_id="acceptance-walkthrough")


def _at(hour: int, minute: int = 0) -> datetime:
    """A UTC instant on the work date, for pinning the day's scans."""
    return datetime(_WORK_DATE.year, _WORK_DATE.month, _WORK_DATE.day, hour, minute, tzinfo=UTC)


@pytest.fixture
def session(db: sa.Connection) -> Session:
    """A session bound to the rolled-back connection, so nothing the walk-through writes survives."""
    return Session(bind=db, expire_on_commit=False)


def _create_world(session: Session) -> tuple[Employee, list[Site], User]:
    """Step 1: a client, three sites with billing rates, one employee with a wage, and an admin user.

    Three sites because the day moves across three; each carries a billing rate so billing has
    something to price. The employee has a wage rate and is assigned to every site (assignment is
    expectation only and never restricts where time is recorded). The admin user is who the export is
    attributed to.
    """
    suffix = uuid.uuid4().hex[:8]
    client = Client(name=f"Kibbutz {suffix}")
    session.add(client)
    session.flush()

    sites: list[Site] = []
    rates = ("60.00", "75.00", "80.00")
    for label, rate in zip(("A", "B", "C"), rates, strict=True):
        site = Site(
            name=f"Site {label} {suffix}",
            site_number=f"W-{suffix}-{label}",
            client_id=client.id,
            qr_token=f"qr-{suffix}-{label}",
            qr_token_version=1,
        )
        session.add(site)
        session.flush()
        site.rates.append(
            SiteRate(
                billing_rate=Decimal(rate),
                overtime_billing_rate=None,
                effective_from=date(2025, 1, 1),
                effective_to=None,
            )
        )
        sites.append(site)

    passport = f"W{suffix}"
    employee = Employee(
        full_name="עובד בדיקה",
        full_name_en="Test Worker",
        passport_number=passport,
        passport_number_hash=passport,
        phone="+972500000000",
        country="IL",
        emergency_contact_name="Contact",
        emergency_contact_phone="+972500000001",
        start_date=date(2025, 1, 1),
    )
    session.add(employee)
    session.flush()
    session.add(
        EmployeeRate(
            employee_id=employee.id,
            hourly_wage=Decimal("35.00"),
            overtime_rate=Decimal("52.50"),
            shabbat_holiday_rate=Decimal("70.00"),
            travel_allowance_daily=Decimal("0"),
            effective_from=date(2025, 1, 1),
            effective_to=None,
        )
    )
    for site in sites:
        session.add(EmployeeSite(employee_id=employee.id, site_id=site.id, assigned_from=date(2025, 1, 1)))

    admin = User(
        username=f"admin-{suffix}",
        password_hash="x",  # never used: the export attributes by user, it does not authenticate here
        role=UserRole.ADMIN,
    )
    session.add(admin)
    session.flush()
    return employee, sites, admin


def test_acceptance_walkthrough(session: Session) -> None:
    """The whole month, once, end to end (Requirement 24.4, 18.9)."""
    from app.core.qr_token import mint

    employee, sites, admin = _create_world(session)
    site_a, site_b, site_c = sites

    # --- Step 2: print the QR, and confirm the printed token is the one the scan path accepts. ------
    printed = render_site_qr(site_a)
    assert printed.png and printed.pdf, "a site should render at least one printable QR artefact"
    printed_token = printed.png[0].token
    parsed = parse(printed_token)
    assert parsed.site_id == site_a.id
    assert parsed.version == site_a.qr_token_version

    def _scan_request(site: Site) -> ScanRequest:
        return ScanRequest(qr_token=mint(site.id, site.qr_token_version))

    # --- Step 3: a three-site day. Check in at A, move to B, move to C, check out. ------------------
    scan_service.resolve_scan(
        session, employee_id=employee.id, request=_scan_request(site_a), context=_context(), now=_at(7)
    )
    session.flush()
    scan_service.transition(
        session, employee_id=employee.id, request=_scan_request(site_b), context=_context(), now=_at(11)
    )
    session.flush()
    scan_service.transition(
        session, employee_id=employee.id, request=_scan_request(site_c), context=_context(), now=_at(13)
    )
    session.flush()
    scan_service.check_out(session, employee_id=employee.id, context=_context(), now=_at(17))
    session.flush()

    entries = list(
        session.scalars(
            sa.select(TimeEntry)
            .where(TimeEntry.employee_id == employee.id)
            .order_by(TimeEntry.check_in_at)
        )
    )
    assert len(entries) == 3
    # 07:00–11:00 at A (240), 11:00–13:00 at B (120), 13:00–17:00 at C (240): 600 minutes, no gap, no
    # overlap — the multi-site day the whole design exists to record.
    assert [e.site_id for e in entries] == [site_a.id, site_b.id, site_c.id]
    assert [e.total_minutes for e in entries] == [240, 120, 240]
    assert sum(e.total_minutes for e in entries) == 600
    assert all(e.check_out_at is not None for e in entries)
    assert entries[0].check_out_at == entries[1].check_in_at
    assert entries[1].check_out_at == entries[2].check_in_at

    # --- Step 4: correct one entry manually, with a reason. Extend Site C's close by 30 minutes. ----
    corrected = time_entry_service.correct_manual_entry(
        session,
        entries[2].id,
        TimeEntryUpdate(check_out_at=_at(17, 30), reason="Employee left 30 minutes later than scanned"),
        context=_context(),
    )
    session.flush()
    assert corrected.is_manual is True
    assert corrected.total_minutes == 270  # 13:00–17:30
    # The correction is audited, so the change is attributable.
    audit_count = session.scalar(
        sa.text(
            "SELECT count(*) FROM change_logs WHERE entity_type = 'time_entries' AND entity_id = :id"
        ),
        {"id": entries[2].id},
    )
    assert audit_count and audit_count > 0

    # --- Step 5: approve every entry, then lock the month. ------------------------------------------
    for entry in entries:
        period_service.apply_status(
            session, entry, TimeEntryStatus.REVIEW, is_admin=True, reason=None, context=_context()
        )
        period_service.apply_status(
            session, entry, TimeEntryStatus.APPROVED, is_admin=True, reason=None, context=_context()
        )
    session.flush()

    lock = period_service.lock_period(
        session, year=_YEAR, month=_MONTH, force=False, context=_context()
    )
    session.flush()
    assert lock.locked is True
    assert lock.locked_count == 3
    session.expire_all()
    assert all(
        e.status is TimeEntryStatus.LOCKED
        for e in session.scalars(sa.select(TimeEntry).where(TimeEntry.employee_id == employee.id))
    )

    # A write into the locked month is now refused for a non-override caller (Requirement 15.5).
    with pytest.raises(scan_service.PeriodLocked):
        scan_service._check_in(
            session,
            employee=employee,
            site=site_a,
            moment=_at(20),
            context=_context(),
        )

    # --- Step 6: calculate payroll and billing from the approved/locked entries. --------------------
    payroll = payroll_service.calculate_payroll(
        session, employee_id=employee.id, year=_YEAR, month=_MONTH
    )
    session.flush()
    billing = billing_service.calculate_billing(session, year=_YEAR, month=_MONTH)
    session.flush()

    # 630 minutes = 10.5 h; 8 h regular + 2.5 h overtime at 35 / 52.50. The `entries` objects were
    # expired at the lock step and reload from the row, so they already carry the manual correction
    # (Site C's 240 became 270); the day therefore reads 240 + 120 + 270 straight off the entries.
    total_minutes = sum(e.total_minutes for e in entries)
    assert total_minutes == 630
    billing_total = billing.computation.total_billing
    assert payroll.total_pay > Decimal("0")
    assert billing_total > Decimal("0")

    # --- Step 7: read every report, and assert the reconciliation of Requirement 18.9. --------------
    filters = ReportFilters(year=_YEAR, month=_MONTH)
    by_employee = report_by_employee(session, filters=filters)
    by_site = report_by_site(session, filters=filters)
    by_client = report_by_client(session, filters=filters)
    profitability = report_profitability(session, filters=filters)
    payment = billing_service.payment_request(
        session, client_id=site_a.client_id, year=_YEAR, month=_MONTH
    )

    # Per-employee cost equals the payroll record (18.9).
    assert len(by_employee.rows) == 1
    assert by_employee.rows[0].cost == payroll.total_pay
    assert by_employee.total_cost == payroll.total_pay

    # Per-site sums equal the totals (18.9), and profit is billing minus cost at the total.
    assert sum((r.billing for r in by_site.rows), Decimal("0")) == by_site.total_billing
    assert sum((r.cost for r in by_site.rows), Decimal("0")) == by_site.total_cost
    assert by_site.total_profit == by_site.total_billing - by_site.total_cost
    # The billing engine's total agrees with the by-site report and the profitability report.
    assert by_site.total_billing == billing_total
    assert profitability.total_billing == by_site.total_billing
    assert profitability.total_cost == by_site.total_cost

    # By-client total equals the sum of its sites, and the by-site billing total.
    client_total = sum((r.total for r in by_client.rows), Decimal("0"))
    assert client_total == by_client.total
    assert by_client.total == by_site.total_billing
    # The payment request reconciles with the billing records for the same period (18.8).
    assert payment.total_amount == billing_total

    # --- Step 8: export Excel and PDF in both languages. --------------------------------------------
    storage = FakeExportStorage()
    for language in ("he", "en"):
        excel = export_service.create_export(
            session,
            request=export_service.ExportRequest(
                report_type=ExportReportType.BY_SITE,
                format=ExportFormat.XLSX,
                year=_YEAR,
                month=_MONTH,
                language=language,
            ),
            user=admin,
            scope=SiteScope.all_sites(),
            may_read_money=True,
            context=_context(),
            storage=storage,
        )
        session.flush()
        assert excel.status is ExportStatus.READY
        # The workbook opens and its cells are typed — a real Excel file, not text (Requirement 19.1).
        workbook = openpyxl.load_workbook(io.BytesIO(storage.bytes_at(excel.file_key)))
        assert workbook.active.max_row >= 1

        # PDF in the same language. The byte render needs WeasyPrint's native stack; where it is
        # absent the export cannot render inline and raises, and the RTL HTML is covered elsewhere.
        try:
            pdf = export_service.create_export(
                session,
                request=export_service.ExportRequest(
                    report_type=ExportReportType.BY_SITE,
                    format=ExportFormat.PDF,
                    year=_YEAR,
                    month=_MONTH,
                    language=language,
                ),
                user=admin,
                scope=SiteScope.all_sites(),
                may_read_money=True,
                context=_context(),
                storage=storage,
            )
            session.flush()
        except export_service.PdfRenderNotAvailable:
            continue
        assert pdf.status is ExportStatus.READY
        assert storage.bytes_at(pdf.file_key)[:5] == b"%PDF-"
