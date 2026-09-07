"""Report aggregation over the computed payroll and billing records (Requirement 18).

This is a read-only aggregation layer. It does **not** recompute money — every figure a report shows
has already been produced by the payroll engine (`app.services.payroll`, writing `payroll_records`
and `payroll_site_allocations`) or the billing engine (`app.services.billing`, writing
`billing_records`). A report groups and sums those stored figures. Reusing the computed records rather
than re-deriving from `time_entries` is what makes the reconciliation in Requirement 18.9 hold by
construction: a per-employee cost *is* the payroll record's total for the month, and a per-site cost
*is* the sum of the allocations to that site, so the report cannot disagree with the record it read.

The four reports of Requirement 18.1–18.4:

* **By employee (18.1).** Hours and cost per employee for a period, from `payroll_records`. Cost is
  the record's `total_pay`; hours are the four bucket minutes summed. Reconciles trivially: each row's
  cost is exactly one payroll record's total.

* **By site (18.2).** Hours, billing, cost and profit per site. Billing comes from `billing_records`;
  cost is the payroll cost allocated to the site (`payroll_site_allocations` summed for the month);
  profit is billing minus that cost. The sum of the per-site figures equals the totals (18.9), because
  the totals are computed as the sum of the rows, not from a separate query.

* **By client (18.3).** The billed amount per site under a client, and the client total, from
  `billing_records` grouped by client. Each client total is the sum of its sites' amounts (18.9).

* **Profitability (18.4).** Total billing, cost and gross profit for the selected filters (month,
  employee, site, client, project). Assembled from the same per-site rows the by-site report uses, so
  a filtered profitability figure is the sum of the sites it admits.

**Every figure is ILS (18.7).** The report carries the currency alongside the period and the filters
applied so a response is self-describing — the router stamps `currency="ILS"` and echoes the filters
back. Money is `Decimal` end to end so the two-place figures the engines produced are never coerced
through a float.

**Scope (Requirement 2.5).** The cost, billing and profit reports are finance data and the router puts
them behind the finance guard. The by-employee report is the exception the task calls out: a site
manager may see *hours* for their sites but not wage or cost. The service therefore accepts an
optional site scope and, when the caller is not a finance role, narrows the by-employee rows to the
employees who worked the caller's sites and omits their cost — the router does the omission through the
schema, and the service exposes `restrict_to_sites` so the aggregation counts only the manager's
sites' minutes. Nothing here commits: reports are pure reads.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.authz import SiteScope
from app.models.billing import BillingRecord
from app.models.employee import Employee, EmployeeStatus
from app.models.payroll import PayrollRecord, PayrollSiteAllocation
from app.models.site import EmployeeSite, Site, SiteStatus
from app.models.staffing_company import StaffingCompany
from app.models.time_entry import TimeEntry

#: The currency every report states its figures in (Requirement 18.7). The system is single-currency
#: (Israeli new shekel); the constant is named here so a report response carries it explicitly rather
#: than leaving the front end to assume it.
CURRENCY = "ILS"

_ZERO = Decimal("0.00")


# --------------------------------------------------------------------------- filters


@dataclass(frozen=True, slots=True)
class ReportFilters:
    """The filters a report was run with, echoed back so a response is self-describing (Req 18.7).

    `year` and `month` name the period and are always present. `employee_id`, `site_id`, `client_id`
    and `project` are optional narrowings the profitability report applies (Requirement 18.4); a
    report that does not use one leaves it `None`. `project` is carried because the requirement names
    it as a filter dimension, though the schema has no separate project entity — a site stands in for a
    project of work — so it is accepted and echoed but not yet used to narrow, and is documented as
    such rather than silently dropped.
    """

    year: int
    month: int
    employee_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    project: str | None = None
    #: Narrows the by-staffing-company report to one provider (Requirement 4.1). None for the other
    #: reports, which do not use it.
    staffing_company_id: uuid.UUID | None = None


# --------------------------------------------------------------------------- by employee (18.1)


@dataclass(frozen=True, slots=True)
class EmployeeReportRow:
    """One employee's hours and cost for a period (Requirement 18.1).

    `regular_minutes` … `holiday_minutes` are the four buckets from the payroll record; `total_minutes`
    is their sum. `cost` is the record's `total_pay` — the single source of the employee's cost, so the
    report reconciles to the payroll record exactly (Requirement 18.9). When the caller may not see
    money (a site manager), the router omits `cost`; the minutes remain, because hours are what a
    manager may see for their sites (Requirement 2.5).
    """

    employee_id: uuid.UUID
    employee_name: str
    employee_name_en: str
    employee_number: str | None
    regular_minutes: int
    overtime_minutes: int
    shabbat_minutes: int
    holiday_minutes: int
    cost: Decimal

    @property
    def total_minutes(self) -> int:
        return (
            self.regular_minutes
            + self.overtime_minutes
            + self.shabbat_minutes
            + self.holiday_minutes
        )


@dataclass(frozen=True, slots=True)
class EmployeeReport:
    """The by-employee report: one row per employee, plus the period totals (Requirement 18.1)."""

    rows: tuple[EmployeeReportRow, ...] = ()
    total_minutes: int = 0
    total_cost: Decimal = _ZERO


def report_by_employee(
    session: Session,
    *,
    filters: ReportFilters,
    scope: SiteScope | None = None,
) -> EmployeeReport:
    """Hours and cost per employee for a month, from `payroll_records` (Requirement 18.1, 18.9).

    Each row's cost is one payroll record's `total_pay`, so the report is the payroll records for the
    period grouped by employee — reconciliation with payroll (Requirement 18.9) is by construction, not
    a second computation that could drift.

    When `scope` is a restricted site scope (a site manager), the rows are narrowed to the employees
    who have a payroll allocation to one of the caller's sites, and the minutes shown are only those
    worked at the caller's sites — a manager sees the hours their sites earned, not the employee's
    whole month. The cost is still carried on the row; the router strips it for a non-finance caller,
    because the wage is what a manager may not see (Requirement 2.5), and stripping at the boundary is
    the one place the redaction is guaranteed to happen.

    An `employee_id` filter narrows to a single employee (used by the profitability drill-down).
    """
    if scope is not None and not scope.unrestricted:
        return _employee_report_scoped(session, filters=filters, scope=scope)

    conditions = [PayrollRecord.year == filters.year, PayrollRecord.month == filters.month]
    if filters.employee_id is not None:
        conditions.append(PayrollRecord.employee_id == filters.employee_id)

    statement = (
        select(PayrollRecord, Employee.full_name, Employee.full_name_en, Employee.employee_number)
        .join(Employee, Employee.id == PayrollRecord.employee_id)
        .where(*conditions)
        .order_by(Employee.full_name, PayrollRecord.employee_id)
    )

    rows: list[EmployeeReportRow] = []
    total_minutes = 0
    total_cost = _ZERO
    for record, name, name_en, number in session.execute(statement):
        row = EmployeeReportRow(
            employee_id=record.employee_id,
            employee_name=name,
            employee_name_en=name_en,
            employee_number=number,
            regular_minutes=record.regular_minutes,
            overtime_minutes=record.overtime_minutes,
            shabbat_minutes=record.shabbat_minutes,
            holiday_minutes=record.holiday_minutes,
            cost=record.total_pay,
        )
        rows.append(row)
        total_minutes += row.total_minutes
        total_cost += row.cost

    return EmployeeReport(rows=tuple(rows), total_minutes=total_minutes, total_cost=total_cost)


def _employee_report_scoped(
    session: Session, *, filters: ReportFilters, scope: SiteScope
) -> EmployeeReport:
    """The by-employee report narrowed to a site manager's sites (Requirement 2.5, 18.1).

    Built from `payroll_site_allocations` rather than the whole `payroll_records` row: a manager sees
    the minutes and cost that fall to *their* sites, not the employee's full month. The allocations for
    the manager's sites in the month are grouped by employee; the minutes are the sum of the
    allocations' buckets, and the cost is the sum of the allocations' costs. An empty scope yields no
    rows, which is the correct reading of a manager with no assignment — not "every employee".
    """
    if scope.is_empty:
        return EmployeeReport()

    conditions = [
        PayrollRecord.year == filters.year,
        PayrollRecord.month == filters.month,
        PayrollSiteAllocation.site_id.in_(scope.site_ids),
    ]
    if filters.employee_id is not None:
        conditions.append(PayrollRecord.employee_id == filters.employee_id)

    statement = (
        select(
            PayrollRecord.employee_id,
            Employee.full_name,
            Employee.full_name_en,
            Employee.employee_number,
            func.coalesce(func.sum(PayrollSiteAllocation.regular_minutes), 0),
            func.coalesce(func.sum(PayrollSiteAllocation.overtime_minutes), 0),
            func.coalesce(func.sum(PayrollSiteAllocation.shabbat_minutes), 0),
            func.coalesce(func.sum(PayrollSiteAllocation.holiday_minutes), 0),
            func.coalesce(func.sum(PayrollSiteAllocation.cost), 0),
        )
        .join(PayrollRecord, PayrollRecord.id == PayrollSiteAllocation.payroll_record_id)
        .join(Employee, Employee.id == PayrollRecord.employee_id)
        .where(*conditions)
        .group_by(
            PayrollRecord.employee_id, Employee.full_name, Employee.full_name_en, Employee.employee_number
        )
        .order_by(Employee.full_name, PayrollRecord.employee_id)
    )

    rows: list[EmployeeReportRow] = []
    total_minutes = 0
    total_cost = _ZERO
    for employee_id, name, name_en, number, regular, overtime, shabbat, holiday, cost in session.execute(
        statement
    ):
        row = EmployeeReportRow(
            employee_id=employee_id,
            employee_name=name,
            employee_name_en=name_en,
            employee_number=number,
            regular_minutes=int(regular),
            overtime_minutes=int(overtime),
            shabbat_minutes=int(shabbat),
            holiday_minutes=int(holiday),
            cost=Decimal(cost),
        )
        rows.append(row)
        total_minutes += row.total_minutes
        total_cost += row.cost

    return EmployeeReport(rows=tuple(rows), total_minutes=total_minutes, total_cost=total_cost)


# --------------------------------------------------------------------------- site cost (shared)


def _site_costs(session: Session, *, year: int, month: int) -> dict[uuid.UUID, Decimal]:
    """The cost payroll allocated to each site for the month (Requirement 18.2, 18.9).

    Summed from `payroll_site_allocations` across every employee's record for the month — the same
    read the billing service uses for profit — so a site's cost in the by-site report is the same
    figure that produced its billing profit, and the two reports cannot disagree. A site absent here
    had no allocated cost.
    """
    rows = session.execute(
        select(
            PayrollSiteAllocation.site_id,
            func.coalesce(func.sum(PayrollSiteAllocation.cost), 0),
        )
        .join(PayrollRecord, PayrollRecord.id == PayrollSiteAllocation.payroll_record_id)
        .where(PayrollRecord.year == year, PayrollRecord.month == month)
        .group_by(PayrollSiteAllocation.site_id)
    ).all()
    return {site_id: Decimal(cost) for site_id, cost in rows}


# --------------------------------------------------------------------------- by site (18.2)


@dataclass(frozen=True, slots=True)
class SiteReportRow:
    """One site's hours, billing, cost and profit for a period (Requirement 18.2).

    `billing` is the site's `billing_records.total_amount`; `cost` is the payroll cost allocated to the
    site; `profit` is `billing − cost`. The minutes are the billable minutes the billing record stored.
    """

    site_id: uuid.UUID
    site_name: str
    client_id: uuid.UUID
    regular_minutes: int
    overtime_minutes: int
    billing: Decimal
    cost: Decimal
    profit: Decimal

    @property
    def total_minutes(self) -> int:
        return self.regular_minutes + self.overtime_minutes


@dataclass(frozen=True, slots=True)
class SiteReport:
    """The by-site report: one row per site, plus the period totals (Requirement 18.2).

    The totals are the sum of the rows, so `total_billing` equals the sum of the rows' `billing`, and
    likewise for cost and profit — the reconciliation of Requirement 18.9 holds by construction.
    """

    rows: tuple[SiteReportRow, ...] = ()
    total_minutes: int = 0
    total_billing: Decimal = _ZERO
    total_cost: Decimal = _ZERO
    total_profit: Decimal = _ZERO


def _site_rows(session: Session, *, filters: ReportFilters) -> list[SiteReportRow]:
    """The per-site rows for a period, joining billing to the site name and the allocated cost.

    Billing comes from `billing_records`; cost from the shared `_site_costs`. Optional `site_id` and
    `client_id` filters narrow the rows (used by the by-site read and the profitability filter). The
    rows are ordered by client then site so the by-client aggregation groups naturally and the order is
    stable across runs.
    """
    conditions = [BillingRecord.year == filters.year, BillingRecord.month == filters.month]
    if filters.site_id is not None:
        conditions.append(BillingRecord.site_id == filters.site_id)
    if filters.client_id is not None:
        conditions.append(BillingRecord.client_id == filters.client_id)

    statement = (
        select(BillingRecord, Site.name)
        .join(Site, Site.id == BillingRecord.site_id)
        .where(*conditions)
        .order_by(BillingRecord.client_id, BillingRecord.site_id)
    )

    site_costs = _site_costs(session, year=filters.year, month=filters.month)
    rows: list[SiteReportRow] = []
    for record, site_name in session.execute(statement):
        cost = site_costs.get(record.site_id, _ZERO)
        billing = record.total_amount
        rows.append(
            SiteReportRow(
                site_id=record.site_id,
                site_name=site_name,
                client_id=record.client_id,
                regular_minutes=record.regular_minutes,
                overtime_minutes=record.overtime_minutes,
                billing=billing,
                cost=cost,
                profit=billing - cost,
            )
        )
    return rows


def report_by_site(session: Session, *, filters: ReportFilters) -> SiteReport:
    """Hours, billing, cost and profit per site for a period (Requirement 18.2, 18.9).

    Totals are summed from the rows, so the sum of the per-site figures equals the corresponding total
    to the agora — the reconciliation of Requirement 18.9. Finance data; the router guards it.
    """
    rows = _site_rows(session, filters=filters)
    total_minutes = sum(row.total_minutes for row in rows)
    total_billing = sum((row.billing for row in rows), _ZERO)
    total_cost = sum((row.cost for row in rows), _ZERO)
    return SiteReport(
        rows=tuple(rows),
        total_minutes=total_minutes,
        total_billing=total_billing,
        total_cost=total_cost,
        total_profit=total_billing - total_cost,
    )


# --------------------------------------------------------------------------- by client (18.3)


@dataclass(frozen=True, slots=True)
class ClientSiteAmount:
    """One site's billed amount under a client (Requirement 18.3)."""

    site_id: uuid.UUID
    site_name: str
    amount: Decimal


@dataclass(slots=True)
class ClientReportRow:
    """One client's per-site amounts and its total (Requirement 18.3).

    `sites` lists the amount billed per site; `total` is their sum — the client total the requirement
    asks for, equal to the sum of the site amounts (Requirement 18.9).
    """

    client_id: uuid.UUID
    sites: list[ClientSiteAmount] = field(default_factory=list)
    total: Decimal = _ZERO


@dataclass(frozen=True, slots=True)
class ClientReport:
    """The by-client report: one row per client with its site breakdown, plus the grand total."""

    rows: tuple[ClientReportRow, ...] = ()
    total: Decimal = _ZERO


def report_by_client(session: Session, *, filters: ReportFilters) -> ClientReport:
    """The billed amount per site under each client, and the client total (Requirement 18.3, 18.9).

    Built from the same per-site billing rows the by-site report uses, grouped by client. Each client's
    total is the sum of its sites' amounts, and the grand total is the sum of the client totals — the
    reconciliation of Requirement 18.9. Finance data; the router guards it.
    """
    rows = _site_rows(session, filters=filters)

    order: list[uuid.UUID] = []
    by_client: dict[uuid.UUID, ClientReportRow] = {}
    for site_row in rows:
        client_row = by_client.get(site_row.client_id)
        if client_row is None:
            client_row = ClientReportRow(client_id=site_row.client_id)
            by_client[site_row.client_id] = client_row
            order.append(site_row.client_id)
        client_row.sites.append(
            ClientSiteAmount(
                site_id=site_row.site_id, site_name=site_row.site_name, amount=site_row.billing
            )
        )
        client_row.total += site_row.billing

    client_rows = tuple(by_client[client_id] for client_id in order)
    grand_total = sum((row.total for row in client_rows), _ZERO)
    return ClientReport(rows=client_rows, total=grand_total)


# --------------------------------------------------------------------------- profitability (18.4)


@dataclass(frozen=True, slots=True)
class ProfitabilityReport:
    """Total billing, cost and gross profit for the selected filters (Requirement 18.4).

    `site_count` is how many sites contributed, so a filtered figure states its own breadth. The
    figures are the sums over the sites the filters admit, so a profitability figure and the by-site
    report it is derived from always agree (Requirement 18.9).
    """

    total_billing: Decimal = _ZERO
    total_cost: Decimal = _ZERO
    total_profit: Decimal = _ZERO
    site_count: int = 0


def report_profitability(session: Session, *, filters: ReportFilters) -> ProfitabilityReport:
    """Billing, cost and gross profit for a month under the given filters (Requirement 18.4).

    Assembled from the per-site rows the by-site report uses, narrowed by whatever of site, client and
    (when given) employee the filters carry, so the profitability total is the sum of the sites it
    admits and reconciles with the by-site report over the same filters (Requirement 18.9). An
    `employee_id` filter narrows to the sites that employee's cost was allocated to for the month, so
    "this employee's profitability" is the profit of the work they contributed to.
    """
    rows = _site_rows(session, filters=filters)

    if filters.employee_id is not None:
        allowed = _sites_worked_by_employee(
            session, employee_id=filters.employee_id, year=filters.year, month=filters.month
        )
        rows = [row for row in rows if row.site_id in allowed]

    total_billing = sum((row.billing for row in rows), _ZERO)
    total_cost = sum((row.cost for row in rows), _ZERO)
    return ProfitabilityReport(
        total_billing=total_billing,
        total_cost=total_cost,
        total_profit=total_billing - total_cost,
        site_count=len(rows),
    )


def _sites_worked_by_employee(
    session: Session, *, employee_id: uuid.UUID, year: int, month: int
) -> set[uuid.UUID]:
    """The sites an employee's payroll cost was allocated to in the month (Requirement 18.4).

    Used to narrow profitability to one employee: the sites they contributed to are exactly the sites
    their payroll record allocated cost to.
    """
    rows = session.execute(
        select(PayrollSiteAllocation.site_id)
        .join(PayrollRecord, PayrollRecord.id == PayrollSiteAllocation.payroll_record_id)
        .where(
            PayrollRecord.employee_id == employee_id,
            PayrollRecord.year == year,
            PayrollRecord.month == month,
        )
    ).scalars()
    return set(rows)


# --------------------------------------------------------------------------- missing reports (14.3)


class MissingReportKind(enum.StrEnum):
    """The three kinds of missing time report the system detects (Requirement 14.3).

    * ``MISSING_CHECKOUT`` — an arrival was recorded but no departure: the entry is still open
      (``check_out_at IS NULL``) after the day it belongs to. The employee scanned in and never
      scanned out.
    * ``MISSING_CHECKIN`` — a departure marker exists with no genuine check-in behind it. A completed
      entry cannot store this (the schema requires a check-in timestamp), so it is represented by a
      manual or system marker carrying the :data:`FLAG_MISSING_CHECK_IN` flag — the "manual or system
      marker with no check-in" of the design.
    * ``BOTH_MISSING`` — the employee was expected at a site on that date but recorded nothing at all:
      no entry, open or closed.
    """

    MISSING_CHECKOUT = "missing_checkout"
    MISSING_CHECKIN = "missing_checkin"
    BOTH_MISSING = "both_missing"


#: The flag a manual or system entry carries to stand for a departure recorded with no check-in behind
#: it (Requirement 14.3). The `flags` column already carries a small closed vocabulary of markers
#: (`unassigned_site`, `implausible_duration`); this is the third. A completed `time_entries` row
#: cannot express "checked out but never checked in" because `check_in_at` is NOT NULL, so the marker
#: is how that state is recorded for the detection to find and for a manager to correct.
FLAG_MISSING_CHECK_IN = "missing_check_in"


@dataclass(frozen=True, slots=True)
class MissingReportFinding:
    """One missing-report finding: an employee, a date, a site and which of the three kinds it is.

    `site_id` is the site the employee was expected at (their assignment for that date) for a
    both-missing finding, or the site of the offending entry for a missing check-out or check-in. It
    is what the notification's `dedupe_key = type:employee:date:site` is built from (design "Missing-
    report detection"), so it is carried on the finding rather than left for the caller to re-derive.
    """

    employee_id: uuid.UUID
    employee_name: str
    employee_name_en: str
    employee_number: str | None
    work_date: date
    site_id: uuid.UUID
    site_name: str
    kind: MissingReportKind


def _expected_placements(
    session: Session,
    *,
    date_from: date,
    date_to: date,
    scope: SiteScope,
    employee_id: uuid.UUID | None,
    site_id: uuid.UUID | None,
) -> list[EmployeeSite]:
    """The assignments whose period overlaps the range and lies inside the caller's scope.

    An `employee_sites` row is an expectation: the employee was expected at the site from
    `assigned_from` until `assigned_to` (inclusive), or open-endedly when `assigned_to` is null. The
    range overlap is `assigned_from <= date_to AND (assigned_to IS NULL OR assigned_to >= date_from)`.
    A restricted (site-manager) scope narrows to their sites; an empty scope yields nothing, which is
    the correct reading of a manager with no assignment. The optional employee and site filters narrow
    further.
    """
    conditions = [
        EmployeeSite.assigned_from <= date_to,
        or_(EmployeeSite.assigned_to.is_(None), EmployeeSite.assigned_to >= date_from),
    ]
    if employee_id is not None:
        conditions.append(EmployeeSite.employee_id == employee_id)
    if site_id is not None:
        conditions.append(EmployeeSite.site_id == site_id)
    if not scope.unrestricted:
        if not scope.site_ids:
            return []
        conditions.append(EmployeeSite.site_id.in_(scope.site_ids))

    return list(session.scalars(select(EmployeeSite).where(*conditions)))


def detect_missing_reports(
    session: Session,
    *,
    date_from: date,
    date_to: date,
    scope: SiteScope,
    employee_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
) -> list[MissingReportFinding]:
    """Classify missing time reports for the days employees were expected at a site (Requirement 14.3).

    The detection lives in the service, not the router, because Task 30's notifications reuse it: the
    weekly missing-report summary and the manager alerts are the same findings this returns, keyed by
    `type:employee:date:site` for idempotency (design "Missing-report detection"). Keeping it here is
    what lets the report and the notification agree by construction.

    For each `(employee, expected date, expected site)` — an `employee_sites` assignment covering a
    date in the range — the employee's non-deleted entries on that `work_date` decide the finding:

    * no entry at all → **both missing**: expected, recorded nothing;
    * an open entry (`check_out_at IS NULL`) → **missing check-out**: arrived, never left;
    * a marker entry carrying :data:`FLAG_MISSING_CHECK_IN` → **missing check-in**: a departure with
      no check-in behind it;
    * an entry that is complete and unflagged → **no finding**: the day is whole.

    A single expected day can yield more than one finding when the day carries more than one offending
    entry (an open entry *and* a missing-check-in marker), but never a both-missing finding alongside
    another kind — both-missing means the day is empty. The `date_from`/`date_to` range is inclusive;
    a single day passes the same date for both. `scope` narrows to a site manager's sites; the optional
    employee and site filters narrow further. The result is ordered by employee name, then date, then
    site, so it is stable across runs (Requirement 22.5) and reads top to bottom.
    """
    placements = _expected_placements(
        session,
        date_from=date_from,
        date_to=date_to,
        scope=scope,
        employee_id=employee_id,
        site_id=site_id,
    )
    if not placements:
        return []

    employee_ids = {placement.employee_id for placement in placements}
    site_ids = {placement.site_id for placement in placements}

    employees = {
        employee.id: employee
        for employee in session.scalars(
            select(Employee).where(Employee.id.in_(employee_ids))
        )
    }
    sites = {
        site.id: site
        for site in session.scalars(select(Site).where(Site.id.in_(site_ids)))
    }

    # The entries for the expected employees across the whole range, in one read. Grouped in memory by
    # (employee, date) so each expected day is decided against exactly the entries that fall on it —
    # far fewer round trips than a query per expected day, which matters for the 100-employee month.
    entries = session.scalars(
        select(TimeEntry).where(
            TimeEntry.employee_id.in_(employee_ids),
            TimeEntry.work_date >= date_from,
            TimeEntry.work_date <= date_to,
            TimeEntry.deleted_at.is_(None),
        )
    )
    entries_by_day: dict[tuple[uuid.UUID, date], list[TimeEntry]] = {}
    for entry in entries:
        entries_by_day.setdefault((entry.employee_id, entry.work_date), []).append(entry)

    findings: list[MissingReportFinding] = []
    for placement in placements:
        employee = employees.get(placement.employee_id)
        site = sites.get(placement.site_id)
        if employee is None or site is None:  # pragma: no cover — a placement's FK guarantees both
            continue

        placement_end = placement.assigned_to or date_to
        start = max(placement.assigned_from, date_from)
        end = min(placement_end, date_to)
        for offset in range((end - start).days + 1):
            day = date.fromordinal(start.toordinal() + offset)
            day_entries = entries_by_day.get((employee.id, day), [])
            findings.extend(
                _classify_day(employee=employee, site=site, day=day, entries=day_entries)
            )

    findings.sort(key=lambda f: (f.employee_name, f.work_date, f.site_name, f.kind.value))
    return findings


def _classify_day(
    *, employee: Employee, site: Site, day: date, entries: list[TimeEntry]
) -> list[MissingReportFinding]:
    """The findings for one expected `(employee, site, day)` given that day's entries (Req 14.3).

    An empty day is a both-missing finding. A day with entries yields a finding per offending entry —
    one for each open entry (missing check-out) and one for each missing-check-in marker — and nothing
    for an entry that is complete, so a whole day produces no finding. The finding names the expected
    site: that is the placement the report is reconciling against, and what the notification's dedupe
    key is built from (design "Missing-report detection").
    """

    def finding(kind: MissingReportKind) -> MissingReportFinding:
        return MissingReportFinding(
            employee_id=employee.id,
            employee_name=employee.full_name,
            employee_name_en=employee.full_name_en,
            employee_number=employee.employee_number,
            work_date=day,
            site_id=site.id,
            site_name=site.name,
            kind=kind,
        )

    if not entries:
        return [finding(MissingReportKind.BOTH_MISSING)]

    day_findings: list[MissingReportFinding] = []
    for entry in entries:
        if FLAG_MISSING_CHECK_IN in entry.flags:
            day_findings.append(finding(MissingReportKind.MISSING_CHECKIN))
        elif entry.check_out_at is None:
            day_findings.append(finding(MissingReportKind.MISSING_CHECKOUT))
    return day_findings


# --------------------------------------------------------------------------- dashboard (18.5, 18.6)


@dataclass(frozen=True, slots=True)
class DashboardAttention:
    """The attention counts the administrator home dashboard heads (Requirement 18.6).

    Three counts over the current month, each a call to action rather than a figure: how many
    employee-days are missing a check-out, how many are missing a check-in, and how many are missing
    entirely (both). They are the count of the findings :func:`detect_missing_reports` returns for the
    month, bucketed by kind — the same detection the missing-report list and the notifications use, so
    a dashboard count and the list it links to can never disagree. The front end links each count to
    the missing-report list filtered to that kind (Requirement 18.6).
    """

    missing_checkout: int = 0
    missing_checkin: int = 0
    missing_reports: int = 0


@dataclass(frozen=True, slots=True)
class DashboardReport:
    """The administrator home dashboard for the current month (Requirement 18.5, 18.6).

    The head-line figures the requirement names: the number of active employees and active sites right
    now, and — for the current month — total work hours (as minutes, the front end renders hours),
    billing, employee cost and gross profit. The finance figures are the by-site report's totals for
    the month, so the dashboard reconciles with the by-site report by construction (Requirement 18.9)
    rather than being a second, drifting computation. Below them sits the attention section
    (Requirement 18.6).

    `year` and `month` name the period the figures cover — the current month on the server — so the
    response is self-describing, and `currency` states the money is ILS (Requirement 18.7).
    """

    year: int
    month: int
    active_employees: int
    active_sites: int
    total_minutes: int
    total_billing: Decimal
    total_cost: Decimal
    total_profit: Decimal
    attention: DashboardAttention
    currency: str = CURRENCY


def build_dashboard(session: Session, *, today: date) -> DashboardReport:
    """Assemble the administrator home dashboard for the month `today` falls in (Requirement 18.5, 18.6).

    The finance figures are the by-site report's totals for the current month — billing, cost, profit
    and the billable minutes — so the dashboard cannot disagree with the report a reader would open to
    check it (Requirement 18.9). The active-employee and active-site counts are counts of the rows in
    each state right now, not for a period: "active" is a current status, not something a past month
    had. The attention counts come from :func:`detect_missing_reports` over the whole current month
    with an unrestricted scope — the dashboard is an administrator surface, so it sees every site — and
    are bucketed by kind.

    `today` is passed in rather than read here so the caller (the router) owns the clock and a test can
    fix the month; the service stays a pure function of its inputs.
    """
    year, month = today.year, today.month

    active_employees = session.scalar(
        select(func.count()).select_from(Employee).where(Employee.status == EmployeeStatus.ACTIVE)
    )
    active_sites = session.scalar(
        select(func.count()).select_from(Site).where(Site.status == SiteStatus.ACTIVE)
    )

    site_report = report_by_site(session, filters=ReportFilters(year=year, month=month))

    month_start = date(year, month, 1)
    month_end = _last_day_of_month(year, month)
    findings = detect_missing_reports(
        session,
        date_from=month_start,
        date_to=month_end,
        scope=SiteScope.all_sites(),
    )
    attention = DashboardAttention(
        missing_checkout=sum(1 for f in findings if f.kind is MissingReportKind.MISSING_CHECKOUT),
        missing_checkin=sum(1 for f in findings if f.kind is MissingReportKind.MISSING_CHECKIN),
        missing_reports=sum(1 for f in findings if f.kind is MissingReportKind.BOTH_MISSING),
    )

    return DashboardReport(
        year=year,
        month=month,
        active_employees=int(active_employees or 0),
        active_sites=int(active_sites or 0),
        total_minutes=site_report.total_minutes,
        total_billing=site_report.total_billing,
        total_cost=site_report.total_cost,
        total_profit=site_report.total_profit,
        attention=attention,
    )


def _last_day_of_month(year: int, month: int) -> date:
    """The last calendar day of the given month, so the dashboard covers the whole current month.

    December rolls to the first of the next year; every other month to the first of the next month,
    less a day. Kept explicit rather than reaching for `calendar.monthrange` so the intent reads at the
    call site.
    """
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


__all__ = [
    "CURRENCY",
    "FLAG_MISSING_CHECK_IN",
    "ClientReport",
    "ClientReportRow",
    "ClientSiteAmount",
    "DashboardAttention",
    "DashboardReport",
    "EmployeeReport",
    "EmployeeReportRow",
    "MissingReportFinding",
    "MissingReportKind",
    "ProfitabilityReport",
    "ReportFilters",
    "SiteReport",
    "SiteReportRow",
    "build_dashboard",
    "detect_missing_reports",
    "report_by_client",
    "report_by_employee",
    "report_by_site",
    "report_profitability",
]


# --------------------------------------------------------------------------- by staffing company (Req 4)


@dataclass(frozen=True, slots=True)
class StaffingCompanyReport:
    """Total worked hours and payment for one staffing company over a period (Requirement 4).

    `total_minutes` is the sum of worked minutes over the period for the employees currently linked to
    the company; `total_payment` is `total_minutes / 60` multiplied by the company's single flat
    `hourly_rate`, or `None` when the company has no rate set — the report shows payment as unavailable
    rather than zero (Requirement 4.7). `total_payment` uses the company's flat rate, never an
    individual employee's pay rate (Requirement 4.5).
    """

    company_id: uuid.UUID
    company_name: str
    hourly_rate: Decimal | None
    total_minutes: int
    total_payment: Decimal | None

    @property
    def total_hours(self) -> Decimal:
        """Worked minutes expressed as hours, for the payment computation and the response."""
        return (Decimal(self.total_minutes) / Decimal(60)).quantize(Decimal("0.01"))


def report_by_staffing_company(
    session: Session, *, filters: ReportFilters
) -> StaffingCompanyReport:
    """Hours and payment for one staffing company for a month (Requirement 4).

    Minutes are summed from `payroll_records` for the month, joined to `Employee` and narrowed to the
    employees currently linked to the company — the same source the by-employee report reads, so the
    two reconcile. Payment is the resulting hours times the company's flat `hourly_rate`; a company
    with no rate yields `None` (presented as unavailable, never zero). The company must exist; a
    missing id raises `StaffingCompanyNotFound` so the router can answer 404.
    """
    company = session.get(StaffingCompany, filters.staffing_company_id)
    if company is None:
        from app.services.staffing_company import StaffingCompanyNotFound

        raise StaffingCompanyNotFound(filters.staffing_company_id)

    total_minutes = session.scalar(
        select(
            func.coalesce(
                func.sum(
                    PayrollRecord.regular_minutes
                    + PayrollRecord.overtime_minutes
                    + PayrollRecord.shabbat_minutes
                    + PayrollRecord.holiday_minutes
                ),
                0,
            )
        )
        .join(Employee, Employee.id == PayrollRecord.employee_id)
        .where(PayrollRecord.year == filters.year, PayrollRecord.month == filters.month)
        .where(Employee.staffing_company_id == company.id)
    ) or 0

    if company.hourly_rate is not None:
        total_hours = Decimal(total_minutes) / Decimal(60)
        total_payment = (total_hours * company.hourly_rate).quantize(Decimal("0.01"))
    else:
        total_payment = None

    return StaffingCompanyReport(
        company_id=company.id,
        company_name=company.name,
        hourly_rate=company.hourly_rate,
        total_minutes=int(total_minutes),
        total_payment=total_payment,
    )