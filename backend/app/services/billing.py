"""Billing calculation and persistence (Requirement 17).

This is the database-and-transaction layer around the pure calculation in
`app.calculations.billing`. The pure module knows arithmetic — per-site billing from hours and the
site rate in force per date, and profit as billing minus allocated cost — and nothing about where the
numbers come from. This module is the other half: it reads the month's billable entries across every
site, classifies each employee's day with the existing `app.calculations.hours.classify_day` to get
the regular/overtime split, resolves the *site's* billing rate in force on each work date, reads the
cost payroll already allocated to each site, hands the assembled site-days to the pure module, and
writes the result as `billing_records` rows.

The rules it owns, from the design's "Billing and profit" and Requirement 17.1–17.6:

**Billable entries only (Requirement 17.6).** Billing reads only Approved or Locked entries — the
same "approved hours" the payroll engine bills from (Requirement 15.8) — because Draft and Review
entries can still move. Unlike payroll, billing also *reports* what it left out: the count and minutes
of unapproved (Draft or Review) entries in the month, so a low billing figure is never mistaken for a
low month (Requirement 17.6). Deleted and open entries are excluded from both the billing and the
excluded-hours count: a deleted entry is not work, and an open one has no duration.

**Overtime is a per-employee-per-day fact (Requirement 17.2).** Whether a minute is overtime is
decided by the employee's day crossing the daily threshold, not by the site. So each employee's day
is classified on its own with `classify_day` keyed by site — exactly as payroll does — and the
regular/overtime minutes that fall to each site are then summed across employees for the site's month.
Billing then prices the site's regular minutes at the site's standard rate and its overtime minutes at
the site's overtime rate where one is configured (Requirement 17.2).

**A day carries its own site rate (Requirement 17.1).** Each work date is billed at the site's rate
in force on *that* date, so a mid-month site-rate change splits the month at the right day. The
service resolves the site rate per date with `app.services.site.resolve_rate` and pairs it with that
date's minutes; the pure module never resolves a rate itself.

**Cost from payroll, not recomputed (Requirement 17.4).** A site's cost is the cost payroll already
allocated to it in `payroll_site_allocations` for the month — the single source of the cost figure, so
billing's profit and payroll's cost cannot drift. Billing reads the allocations; it does not price
labour itself.

Like every other service, nothing here commits — the router owns the unit of work, so the records and
any read share one transaction (the pattern `app.services.payroll` follows).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.calculations.billing import (
    BillingComputation,
    DayInput,
    SiteBillingRates,
    compute_billing,
)
from app.calculations.hours import ClassificationSettings, DayEntry, classify_day
from app.calculations.payroll import allocate_largest_remainder
from app.models.billing import BillingRecord
from app.models.employee import Employee
from app.models.payroll import CalculationStatus, PayrollRecord, PayrollSiteAllocation
from app.models.site import Site
from app.models.time_entry import TimeEntry, TimeEntryStatus
from app.services import client as client_service
from app.services import period as period_service
from app.services import site as site_service
from app.services.payroll import _as_utc, build_classification_settings

# The statuses billing may read: Approved and Locked only (Requirement 17.6, 15.8). Draft and Review
# are still in flight and must not feed a billing figure.
_BILLABLE_STATUSES: frozenset[TimeEntryStatus] = frozenset({TimeEntryStatus.APPROVED, TimeEntryStatus.LOCKED})

# The statuses billing reports as *excluded*: Draft and Review, the unapproved hours a summary states
# the count of so a low figure is not mistaken for a low month (Requirement 17.6).
_EXCLUDED_STATUSES: frozenset[TimeEntryStatus] = frozenset({TimeEntryStatus.DRAFT, TimeEntryStatus.REVIEW})


# --------------------------------------------------------------------------- errors


class BillingError(Exception):
    """Base for a refused billing calculation or read."""

    code = "billing_error"


class MissingSiteRate(BillingError):
    """No billing rate covers a work date a site has billable hours on (Requirement 17.1).

    Every billable day must be priceable, so a site with hours but no billing rate in force on a date
    is a data error the caller must fix — a gap in the site's rate history — rather than a day
    silently billed at zero, which would understate the client's invoice without anyone noticing.
    """

    code = "missing_site_rate"

    def __init__(self, *, site_id: uuid.UUID, work_date: date) -> None:
        super().__init__(f"no billing rate for site {site_id} on {work_date}")
        self.site_id = site_id
        self.work_date = work_date


# --------------------------------------------------------------------------- reading entries


def billable_entries(session: Session, *, year: int, month: int) -> list[TimeEntry]:
    """Every site's Approved or Locked, completed, non-deleted entries in the month (Req 17.6, 15.8).

    Across all employees and all sites — billing is a whole-period figure, not per employee. Open
    entries (no check-out) have no duration and cannot be priced, so they are excluded here rather
    than reaching `classify_day`. Ordered by employee then work date then check-in so each employee's
    day assembles in the order it happened, which is what the chronological overtime split needs.
    """
    first, last = period_service.month_bounds(year, month)
    statement = (
        select(TimeEntry)
        .where(
            TimeEntry.work_date >= first,
            TimeEntry.work_date <= last,
            TimeEntry.deleted_at.is_(None),
            TimeEntry.check_out_at.is_not(None),
            TimeEntry.status.in_(_BILLABLE_STATUSES),
        )
        .order_by(TimeEntry.employee_id, TimeEntry.work_date, TimeEntry.check_in_at)
    )
    return list(session.scalars(statement))


@dataclass(frozen=True, slots=True)
class ExcludedHours:
    """The unapproved hours a billing summary leaves out, reported so a low figure is explained (17.6)."""

    entry_count: int = 0
    minutes: int = 0


def excluded_hours(session: Session, *, year: int, month: int) -> ExcludedHours:
    """Count and total-minute the Draft/Review, completed, non-deleted entries in the month (17.6).

    These are the hours billing did not include because they are not yet approved. Reporting their
    count and minutes is what keeps a low billing figure from being read as a low month. Open and
    deleted entries are not counted: an open entry has no duration, and a deleted one is not work.
    """
    first, last = period_service.month_bounds(year, month)
    row = session.execute(
        select(
            func.count(TimeEntry.id),
            func.coalesce(func.sum(TimeEntry.total_minutes), 0),
        ).where(
            TimeEntry.work_date >= first,
            TimeEntry.work_date <= last,
            TimeEntry.deleted_at.is_(None),
            TimeEntry.check_out_at.is_not(None),
            TimeEntry.status.in_(_EXCLUDED_STATUSES),
        )
    ).one()
    return ExcludedHours(entry_count=int(row[0] or 0), minutes=int(row[1] or 0))


# --------------------------------------------------------------------------- assembling the site-days


def _site_rates_for(site: Site, on_date: date) -> SiteBillingRates:
    """The site's billing rates in force on `on_date`, or raise `MissingSiteRate` (Requirement 17.1).

    Reuses `app.services.site.resolve_rate`, the same resolver the site screen uses, so a mid-month
    rate change is applied per date without billing owning a second copy of the rule. `None` overtime
    means the site bills overtime at its standard rate (Requirement 17.2), which the pure module's
    `SiteBillingRates.overtime_effective` handles.
    """
    rate = site_service.resolve_rate(site, on_date)
    if rate is None:
        raise MissingSiteRate(site_id=site.id, work_date=on_date)
    return SiteBillingRates(regular=rate.billing_rate, overtime=rate.overtime_billing_rate)


def build_day_inputs(
    session: Session,
    entries: Sequence[TimeEntry],
    classification: ClassificationSettings,
) -> list[DayInput]:
    """Turn the month's billable entries into the per-(site, date) input the pure module bills.

    Overtime is a per-employee-per-day fact, so the entries are grouped by (employee, work date) and
    each such day is classified on its own with `classify_day` keyed by site — the same split payroll
    uses. The regular and overtime minutes that fall to each site are then summed per (site, date)
    across all employees, and each (site, date) is paired with the site's billing rates in force on
    that date. Shabbat and holiday minutes are not billed (they are an employee-pay premium), so they
    are dropped here; only the regular and overtime buckets reach the pure module (design billing
    formula).
    """
    # Group each employee's entries by their work date, so a day is classified as one unit.
    by_employee_day: dict[tuple[uuid.UUID, date], list[TimeEntry]] = defaultdict(list)
    for entry in entries:
        by_employee_day[(entry.employee_id, entry.work_date)].append(entry)

    # Accumulate the billable minutes per (site, work date) across every employee.
    per_site_day: dict[tuple[uuid.UUID, date], dict[str, int]] = defaultdict(
        lambda: {"regular": 0, "overtime": 0}
    )
    for (_, work_date), day_entries in by_employee_day.items():
        classified = classify_day(
            (
                DayEntry(
                    key=entry.site_id,
                    check_in_at=_as_utc(entry.check_in_at),
                    check_out_at=_as_utc(entry.check_out_at),
                )
                for entry in day_entries
            ),
            classification,
        )
        for entry_class in classified.entries:
            bucket = per_site_day[(entry_class.key, work_date)]
            bucket["regular"] += entry_class.regular_minutes
            bucket["overtime"] += entry_class.overtime_minutes

    # Resolve the site's rate per (site, date), loading each site once. A day with only Shabbat or
    # holiday minutes (nothing to bill) is skipped, so a site is not priced for a date it earned no
    # billable minutes on.
    site_cache: dict[uuid.UUID, Site] = {}
    days: list[DayInput] = []
    for (site_id, work_date), bucket in per_site_day.items():
        if bucket["regular"] == 0 and bucket["overtime"] == 0:
            continue
        site = site_cache.get(site_id)
        if site is None:
            site = site_service.get_site(session, site_id)
            site_cache[site_id] = site
        days.append(
            DayInput(
                work_date=work_date,
                site_id=site_id,
                rates=_site_rates_for(site, work_date),
                regular_minutes=bucket["regular"],
                overtime_minutes=bucket["overtime"],
            )
        )
    # Stable order: by site then date, so the assembled input — and thus the emitted records — do not
    # depend on dict iteration order.
    days.sort(key=lambda day: (str(day.site_id), day.work_date))
    return days


def _site_client_map(session: Session, site_ids: set[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """Map each billed site to its owning client, for the per-client aggregation (Requirement 17.3)."""
    if not site_ids:
        return {}
    rows = session.execute(select(Site.id, Site.client_id).where(Site.id.in_(site_ids))).all()
    return dict(rows)


def _site_costs(session: Session, *, year: int, month: int) -> dict[uuid.UUID, Decimal]:
    """The cost payroll allocated to each site for the month (Requirement 17.4).

    Summed from `payroll_site_allocations` across every employee's payroll record for the month, so a
    site worked by several employees carries the sum of their costs. This is the single source of the
    cost figure — billing does not price labour — so billing's profit reconciles to payroll's cost.
    A site absent here has no allocated cost and its full billing is profit.
    """
    rows = session.execute(
        select(
            PayrollSiteAllocation.site_id,
            func.coalesce(func.sum(PayrollSiteAllocation.cost), 0),
        )
        .join(PayrollRecord, PayrollSiteAllocation.payroll_record_id == PayrollRecord.id)
        .where(PayrollRecord.year == year, PayrollRecord.month == month)
        .group_by(PayrollSiteAllocation.site_id)
    ).all()
    return {site_id: Decimal(cost) for site_id, cost in rows}


# --------------------------------------------------------------------------- the calculation


@dataclass(frozen=True, slots=True)
class BillingResult:
    """A computed month's billing: the persisted records, the pure computation, and the excluded hours.

    `records` are the upserted `billing_records` (one per billed site); `computation` carries the
    per-client and grand totals the router shapes into the summary; `excluded` is the unapproved-hours
    notice (Requirement 17.6).
    """

    records: Sequence[BillingRecord]
    computation: BillingComputation
    excluded: ExcludedHours


def calculate_billing(session: Session, *, year: int, month: int) -> BillingResult:
    """Compute and persist a month's per-site billing and profit, replacing any drafts (Requirement 17).

    The steps:

    1. Read the month's billable (Approved or Locked, completed) entries across all sites (17.6).
    2. Classify each employee's day to split regular from overtime, sum the billable minutes per
       (site, date), and pair each with the site's billing rate in force on that date (17.1, 17.2).
    3. Read the cost payroll allocated to each site for the month (17.4).
    4. Price everything with the pure `compute_billing` — per-site billing rounded `ROUND_HALF_UP`,
       profit as billing minus cost, and per-client and grand-total aggregation (17.3, 17.4).
    5. Upsert one `billing_records` row per billed site, so a recalculation replaces the drafts rather
       than duplicating them. Each record is `final` when the month is locked, `draft` otherwise.
    6. Count the unapproved hours left out, for the summary's excluded-hours notice (17.6).

    Nothing is committed; the router commits so the records land together.
    """
    first, last = period_service.month_bounds(year, month)
    classification = build_classification_settings(session, first=first, last=last)
    entries = billable_entries(session, year=year, month=month)
    days = build_day_inputs(session, entries, classification)

    site_ids = {day.site_id for day in days}
    site_clients = _site_client_map(session, site_ids)
    site_costs = _site_costs(session, year=year, month=month)

    computation = compute_billing(days, site_clients=site_clients, site_costs=site_costs)

    status = _status_for_month(session, year, month)
    records = _upsert_records(session, year=year, month=month, computation=computation, status=status)
    excluded = excluded_hours(session, year=year, month=month)
    return BillingResult(records=records, computation=computation, excluded=excluded)


def _status_for_month(session: Session, year: int, month: int) -> CalculationStatus:
    """Whether the records are `final` (the month is locked) or `draft` (still open).

    A locked month's entries are frozen (Requirement 15.4), so the billing computed from them is
    final; an open month's records are drafts a later recalculation overwrites — the same rule payroll
    follows.
    """
    period = period_service.get_period(session, year, month)
    if period is not None and period.is_locked:
        return CalculationStatus.FINAL
    return CalculationStatus.DRAFT


def _upsert_records(
    session: Session,
    *,
    year: int,
    month: int,
    computation: BillingComputation,
    status: CalculationStatus,
) -> list[BillingRecord]:
    """Create or replace one `billing_records` row per billed site (Requirement 17.1).

    The `(site_id, year, month)` unique constraint means at most one record exists per site-month; a
    recalculation finds it and overwrites its figures rather than inserting a second row. A site that
    was billed on a previous run but has no billable hours this run keeps its stale record untouched
    — billing is a per-site upsert, not a period-wide replace — which matches payroll leaving an
    untouched employee's record alone. Records are returned in the computation's site order.
    """
    records: list[BillingRecord] = []
    now = datetime.now(UTC)
    for site in computation.sites:
        record = session.scalars(
            select(BillingRecord).where(
                BillingRecord.site_id == site.site_id,
                BillingRecord.year == year,
                BillingRecord.month == month,
            )
        ).one_or_none()
        if record is None:
            record = BillingRecord(site_id=site.site_id, year=year, month=month)
            session.add(record)

        record.client_id = site.client_id
        record.regular_minutes = site.regular_minutes
        record.overtime_minutes = site.overtime_minutes
        record.billing_rate_applied = site.billing_rate_applied
        record.overtime_rate_applied = site.overtime_rate_applied
        record.total_amount = site.billing
        record.status = status
        record.calculated_at = now
        records.append(record)

    session.flush()
    return records


# --------------------------------------------------------------------------- reads


def list_records(session: Session, *, year: int, month: int) -> list[BillingRecord]:
    """Every stored billing record for a period, ordered by client then site (Requirement 17.3).

    Ordered so a period's records read in a stable order, and grouped naturally by client for the
    per-client aggregation the summary presents.
    """
    statement = (
        select(BillingRecord)
        .where(BillingRecord.year == year, BillingRecord.month == month)
        .order_by(BillingRecord.client_id, BillingRecord.site_id)
    )
    return list(session.scalars(statement))


@dataclass(frozen=True, slots=True)
class ClientTotals:
    """A client's summed billing, cost and profit, for the summary's per-client rows (17.3)."""

    client_id: uuid.UUID
    billing: Decimal
    cost: Decimal
    profit: Decimal


def summarise_records(
    records: Sequence[BillingRecord],
) -> tuple[list[ClientTotals], Decimal, Decimal, Decimal]:
    """Aggregate stored records into per-client totals and grand totals, for the `GET` read (17.3, 17.4).

    The stored records carry `total_amount` (billing) but not cost or profit, which are not columns on
    `billing_records`; a stored read therefore reports billing per client and in total. Cost and
    profit are computed live at calculation time and returned in that response; the stored read
    focuses on the billed amounts the invoices were cut from. This helper sums billing per client and
    overall, leaving cost and profit at zero for the stored view.
    """
    order: list[uuid.UUID] = []
    by_client: dict[uuid.UUID, Decimal] = {}
    total_billing = Decimal("0.00")
    for record in records:
        if record.client_id not in by_client:
            by_client[record.client_id] = Decimal("0.00")
            order.append(record.client_id)
        by_client[record.client_id] += record.total_amount
        total_billing += record.total_amount

    clients = [
        ClientTotals(
            client_id=client_id,
            billing=by_client[client_id],
            cost=Decimal("0.00"),
            profit=by_client[client_id],
        )
        for client_id in order
    ]
    return clients, total_billing, Decimal("0.00"), total_billing


# --------------------------------------------------------------------------- payment request (18.8)


@dataclass(frozen=True, slots=True)
class PaymentRequestLine:
    """One (site, employee) line of a payment request: the hours worked and the amount billed for them.

    `regular_minutes` and `overtime_minutes` are the employee's billable minutes at the site for the
    period; `hours` is their sum expressed in hours (a `Decimal`, minutes ÷ 60) for display. `rate` is
    the site's standard billing rate applied on the invoice (`billing_records.billing_rate_applied`),
    carried so the line reads as "hours × rate = amount" even though overtime may bill at a different
    rate inside the amount. `amount` is this employee's share of the site's billed total, distributed
    so the shares sum to the site total to the agora (Requirement 18.8, 17.3).
    """

    employee_id: uuid.UUID
    employee_name: str
    employee_name_en: str
    regular_minutes: int
    overtime_minutes: int
    rate: Decimal
    amount: Decimal

    @property
    def total_minutes(self) -> int:
        return self.regular_minutes + self.overtime_minutes

    @property
    def hours(self) -> Decimal:
        """The line's total minutes as hours, for the "hours" column (Requirement 18.8)."""
        return (Decimal(self.total_minutes) / Decimal("60")).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class PaymentRequestSite:
    """One site's section of a payment request: its employee lines and the site's billed total (18.8).

    `amount` is the site's `billing_records.total_amount` — the authoritative billed figure — and the
    `lines` amounts sum to it exactly (Requirement 17.3). `billing_rate_applied` and
    `overtime_rate_applied` are the rates the invoice was cut at.
    """

    site_id: uuid.UUID
    site_name: str
    site_number: str
    billing_rate_applied: Decimal
    overtime_rate_applied: Decimal | None
    lines: tuple[PaymentRequestLine, ...]
    amount: Decimal


@dataclass(frozen=True, slots=True)
class PaymentRequest:
    """A client's payment request for a period: its sites, their employee lines and the grand total.

    Contains the client, the period, and one section per billed site with per-employee hours, rate and
    amount, plus the client total (Requirement 18.8). The total equals the sum of the site amounts,
    which are the stored `billing_records.total_amount`, so it reconciles with the billing records for
    the same period by construction (Requirement 17.3).
    """

    client_id: uuid.UUID
    client_name: str
    year: int
    month: int
    sites: tuple[PaymentRequestSite, ...]
    total_amount: Decimal


def _employee_site_minutes(
    session: Session,
    entries: Sequence[TimeEntry],
    classification: ClassificationSettings,
) -> dict[tuple[uuid.UUID, uuid.UUID], dict[str, int]]:
    """The billable regular/overtime minutes per (site, employee) across the period (Requirement 18.8).

    The same classification the site-level billing uses (`build_day_inputs`): each employee's day is
    classified as one unit so overtime is the per-employee-per-day fact it is, then the minutes that
    fall to each site are accumulated — but keyed by (site, employee) rather than summed across
    employees, because a payment request breaks a site's billing down by who worked it. Shabbat and
    holiday minutes are not billed and are dropped, matching the site-level billing.
    """
    by_employee_day: dict[tuple[uuid.UUID, date], list[TimeEntry]] = defaultdict(list)
    for entry in entries:
        by_employee_day[(entry.employee_id, entry.work_date)].append(entry)

    per_site_employee: dict[tuple[uuid.UUID, uuid.UUID], dict[str, int]] = defaultdict(
        lambda: {"regular": 0, "overtime": 0}
    )
    for (employee_id, _work_date), day_entries in by_employee_day.items():
        classified = classify_day(
            (
                DayEntry(
                    key=entry.site_id,
                    check_in_at=_as_utc(entry.check_in_at),
                    check_out_at=_as_utc(entry.check_out_at),
                )
                for entry in day_entries
            ),
            classification,
        )
        for entry_class in classified.entries:
            bucket = per_site_employee[(entry_class.key, employee_id)]
            bucket["regular"] += entry_class.regular_minutes
            bucket["overtime"] += entry_class.overtime_minutes
    return per_site_employee


def payment_request(session: Session, *, client_id: uuid.UUID, year: int, month: int) -> PaymentRequest:
    """Build a client's payment request for a period, reconciled to the billing records (Req 18.8, 17.3).

    A payment request is the client's invoice backing: one section per billed site, each broken into
    the employees who worked it with their hours, the site rate and the amount, and the client total.
    It is *derived from the stored `billing_records`*, not a re-computation, so its site totals equal
    the billing figures the month was calculated at and its grand total reconciles with the billing
    records for the same period (Requirement 17.3) — the reconciliation the task asks tests to prove.

    The steps:

    1. Load the client (raising `ClientNotFound` if the id is unknown) and its billing records for the
       period; a client with no records yields an empty request rather than an error.
    2. Re-derive each employee's billable regular/overtime minutes per site from the period's billable
       entries — the same classification the site-level billing used — so the request can name *who*
       worked each site, which the per-site `billing_records` row does not store.
    3. For each billed site, distribute the record's authoritative `total_amount` across its employees
       in proportion to each employee's exact (unrounded) billing, using the largest-remainder method
       so the employee amounts sum to the site total to the agora (Requirement 17.3). The per-employee
       amount is thus a faithful split of the invoice figure, never a re-rounding that could drift.

    Nothing is committed; this is a pure read.
    """
    client = client_service.get_client(session, client_id)

    records = [
        record for record in list_records(session, year=year, month=month) if record.client_id == client_id
    ]
    if not records:
        return PaymentRequest(
            client_id=client.id,
            client_name=client.name,
            year=year,
            month=month,
            sites=(),
            total_amount=Decimal("0.00"),
        )

    first, last = period_service.month_bounds(year, month)
    classification = build_classification_settings(session, first=first, last=last)
    entries = billable_entries(session, year=year, month=month)
    per_site_employee = _employee_site_minutes(session, entries, classification)

    site_ids = {record.site_id for record in records}
    sites_by_id = {site.id: site for site in session.scalars(select(Site).where(Site.id.in_(site_ids)))}
    employee_ids = {employee_id for (_site, employee_id) in per_site_employee}
    employees_by_id = {
        employee.id: employee
        for employee in session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
    }

    sites: list[PaymentRequestSite] = []
    total_amount = Decimal("0.00")
    for record in records:
        site = sites_by_id.get(record.site_id)
        if site is None:  # pragma: no cover — a record's FK guarantees the site
            continue

        # The (employee, minutes) rows for this site, in a stable order by employee name.
        contributors = [
            (employee_id, buckets)
            for (site_id, employee_id), buckets in per_site_employee.items()
            if site_id == record.site_id and (buckets["regular"] > 0 or buckets["overtime"] > 0)
        ]
        contributors.sort(
            key=lambda item: (
                employees_by_id[item[0]].full_name if item[0] in employees_by_id else "",
                str(item[0]),
            )
        )

        overtime_rate = (
            record.overtime_rate_applied
            if record.overtime_rate_applied is not None
            else record.billing_rate_applied
        )
        # Each employee's exact, unrounded billing at this site, priced at the record's applied rates,
        # so the largest-remainder split distributes the record's total the same way the total was
        # earned. Priced with the applied rate rather than per-date rates because the record stored a
        # single rate pair for the invoice; the sum is then corrected to the record's total anyway.
        exact = [
            Decimal(buckets["regular"]) / Decimal("60") * record.billing_rate_applied
            + Decimal(buckets["overtime"]) / Decimal("60") * overtime_rate
            for _employee_id, buckets in contributors
        ]
        amounts = allocate_largest_remainder(exact, record.total_amount)

        lines = tuple(
            PaymentRequestLine(
                employee_id=employee_id,
                employee_name=(
                    employees_by_id[employee_id].full_name if employee_id in employees_by_id else ""
                ),
                employee_name_en=(
                    employees_by_id[employee_id].full_name_en if employee_id in employees_by_id else ""
                ),
                regular_minutes=buckets["regular"],
                overtime_minutes=buckets["overtime"],
                rate=record.billing_rate_applied,
                amount=amount,
            )
            for (employee_id, buckets), amount in zip(contributors, amounts, strict=True)
        )

        sites.append(
            PaymentRequestSite(
                site_id=site.id,
                site_name=site.name,
                site_number=site.site_number,
                billing_rate_applied=record.billing_rate_applied,
                overtime_rate_applied=record.overtime_rate_applied,
                lines=lines,
                amount=record.total_amount,
            )
        )
        total_amount += record.total_amount

    return PaymentRequest(
        client_id=client.id,
        client_name=client.name,
        year=year,
        month=month,
        sites=tuple(sites),
        total_amount=total_amount,
    )


__all__ = [
    "BillingError",
    "BillingResult",
    "ClientTotals",
    "ExcludedHours",
    "MissingSiteRate",
    "PaymentRequest",
    "PaymentRequestLine",
    "PaymentRequestSite",
    "billable_entries",
    "build_day_inputs",
    "calculate_billing",
    "excluded_hours",
    "list_records",
    "payment_request",
    "summarise_records",
]
