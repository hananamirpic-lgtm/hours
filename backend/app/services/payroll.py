"""Payroll calculation and persistence (Requirement 16).

This is the database-and-transaction layer around the pure calculation in
`app.calculations.payroll`. The pure module knows arithmetic — bucket pay, monthly totals, and the
largest-remainder cost allocation — and nothing about where the numbers come from. This module is the
other half: it reads the month's payable entries, resolves the rates in force on each work date,
classifies each day with the existing `app.calculations.hours.classify_day`, hands the assembled days
to the pure module, and writes the result as a `payroll_records` row with its `payroll_site_allocations`.

The rules it owns, from Requirement 15.8 and 16.1–16.10:

**Payable entries only (Requirement 15.8).** Payroll reads only Approved or Locked entries. Draft and
Review entries can still move — the whole point of the approval workflow (Requirement 15) — so paying
from them would compute a figure that shifts underneath accounting. Deleted entries and open (not yet
checked-out) entries are excluded too: a deleted entry is not work, and an open one has no duration.

**Reuse, don't reimplement (design, "Calculation modules").** The daily four-bucket split is
`classify_day`, the same function the hours view and every test are pinned against; the rate in force
on a date is `app.services.employee.resolve_rate`, the same resolver the employee screen uses. This
module only *arranges* their outputs for the pure payroll calculation, so there is one place hour
classification lives and one place rates resolve, and payroll cannot drift from either.

**A day carries its own rates (Requirement 16.9).** Each work date is priced at the rate in force on
*that* date, so a mid-month rate change splits the month at the right day. The service resolves the
rate per date and pairs it with that date's minutes; the pure module never resolves a rate itself.

**Idempotent recalculation (Requirement 16.10).** Recalculating an employee's month replaces the
existing draft rather than adding a second record. The `(employee_id, year, month)` unique constraint
makes that an upsert: find the row, rebuild its figures and its allocations, or create it if absent.
A locked month's record is marked `final`; an open month's is a `draft` that a later run overwrites.

Like every other service, nothing here commits — the router owns the unit of work, so the record, its
allocations and any read all share one transaction (the pattern `app.services.period` follows).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import Column, Date, MetaData, Table, func, inspect, select
from sqlalchemy.orm import Session, selectinload

from app.calculations.hours import (
    ClassificationSettings,
    DayEntry,
    ShabbatWindow,
    classify_day,
)
from app.calculations.payroll import (
    BucketRates,
    DayInput,
    PayrollComputation,
    SiteDayMinutes,
    compute_payroll,
)
from app.core.config import get_settings
from app.models.employee import Employee
from app.models.payroll import CalculationStatus, PayrollRecord, PayrollSiteAllocation
from app.models.time_entry import TimeEntry, TimeEntryStatus
from app.services import employee as employee_service
from app.services import period as period_service
from app.services import settings as settings_service

# The statuses payroll may read: Approved and Locked only (Requirement 15.8). Draft and Review are
# still in flight and must not feed a payroll figure.
_PAYABLE_STATUSES: frozenset[TimeEntryStatus] = frozenset(
    {TimeEntryStatus.APPROVED, TimeEntryStatus.LOCKED}
)

def _as_utc(moment: datetime) -> datetime:
    """A stored timestamp as an aware UTC instant, so `classify_day` gets the tz-aware value it needs.

    Timestamps are always written in UTC. PostgreSQL returns them tz-aware; SQLite (the unit-test
    engine) has no native timezone and returns them naive, so a naive value read back is UTC by
    construction and is tagged as such — the same normalization `app.services.scan` and
    `app.services.time_entry` apply on read, so the calculation behaves identically on both engines.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# A standalone Core table for the holiday calendar. There is no `Holiday` ORM model — the calendar is
# reference data the seed writes and only the calculation reads — so a minimal table on its own
# metadata gives the one column the classification needs (`date`) without adding it to `Base.metadata`,
# where it would show up in Alembic autogenerate as an unmapped table to reconcile.
_HOLIDAYS = Table("holidays", MetaData(), Column("date", Date, nullable=False))


# --------------------------------------------------------------------------- errors


class PayrollError(Exception):
    """Base for a refused payroll calculation or read."""

    code = "payroll_error"


class EmployeeNotFound(PayrollError):
    """No employee with the given id (Requirement 16.6).

    A calculation names an employee; if the id does not resolve, the caller gets a not-found rather
    than an empty record that would imply the employee exists and simply did not work.
    """

    code = "employee_not_found"

    def __init__(self, employee_id: uuid.UUID) -> None:
        super().__init__(f"no employee {employee_id}")
        self.employee_id = employee_id


class PayrollRecordNotFound(PayrollError):
    """No payroll record for the requested employee-month (used by the single-record read)."""

    code = "payroll_record_not_found"


class MissingRate(PayrollError):
    """No rate row covers a work date the employee has payable hours on (Requirement 16.9).

    Every payable day must be priceable, so a day with hours but no rate in force is a data error the
    caller must fix — a gap in the rate history — rather than a day silently priced at zero, which
    would understate the employee's pay without anyone noticing.
    """

    code = "missing_rate"

    def __init__(self, *, employee_id: uuid.UUID, work_date: date) -> None:
        super().__init__(f"no rate for employee {employee_id} on {work_date}")
        self.employee_id = employee_id
        self.work_date = work_date


# --------------------------------------------------------------------------- classification settings


def _shabbat_window(session: Session) -> ShabbatWindow:
    """The Shabbat premium window from the settings table (assumption A7)."""
    return ShabbatWindow(
        start_weekday=settings_service.get_int(session, "shabbat_start_weekday"),
        start_time=settings_service.get_time(session, "shabbat_start_time"),
        end_weekday=settings_service.get_int(session, "shabbat_end_weekday"),
        end_time=settings_service.get_time(session, "shabbat_end_time"),
    )


def _holiday_dates(session: Session, first: date, last: date) -> frozenset[date]:
    """The national-holiday dates falling in the month, from the `holidays` table (Requirement 16.4).

    Scoped to the month so a year of holidays is not loaded to classify one month. Read through the
    standalone `_HOLIDAYS` Core table, since the calendar has no ORM model.

    The `holidays` table is reference data the migration and seed own, not a mapped model, so the
    unit-test engine (SQLite, built from `Base.metadata`) does not have it. When the table is absent —
    only ever the case on that engine — the calendar is treated as empty rather than raising, which is
    the same "SQLite has no PostgreSQL-only object, so the check degrades" treatment the models
    document for their exclusion constraints. A day that should be a holiday is then classified as an
    ordinary day, which a unit test that needs a holiday must set up its own row for; production always
    has the table.
    """
    bind = session.get_bind()
    if not inspect(bind).has_table("holidays"):
        return frozenset()
    statement = select(_HOLIDAYS.c.date).where(
        _HOLIDAYS.c.date >= first, _HOLIDAYS.c.date <= last
    )
    return frozenset(session.execute(statement).scalars())


def build_classification_settings(
    session: Session, *, first: date, last: date
) -> ClassificationSettings:
    """Assemble the `ClassificationSettings` `classify_day` needs, from settings, holidays and config.

    The overtime threshold and the Shabbat window come from the `settings` table; the holiday dates
    come from the `holidays` table, scoped to the month under calculation; the business timezone comes
    from configuration (`Asia/Jerusalem`). Pure `classify_day` reads none of these itself — a pure
    function may not — so the service resolves them once here and hands them in.
    """
    return ClassificationSettings(
        timezone=ZoneInfo(get_settings().app_timezone),
        overtime_threshold_minutes=settings_service.get_int(
            session, "overtime_daily_threshold_minutes"
        ),
        shabbat_window=_shabbat_window(session),
        holiday_dates=_holiday_dates(session, first, last),
    )


# --------------------------------------------------------------------------- reading payable entries


def payable_entries(
    session: Session, *, employee_id: uuid.UUID, year: int, month: int
) -> list[TimeEntry]:
    """The employee's Approved or Locked, completed, non-deleted entries in the month (Req 15.8).

    Only Approved and Locked entries feed payroll; Draft and Review are still in flight. Open entries
    (no check-out) have no duration and cannot be priced, so they are excluded here rather than
    reaching `classify_day`, which classifies only completed shifts. Ordered by check-in so the day
    assembly and the chronological split read the day the way it happened.
    """
    first, last = period_service.month_bounds(year, month)
    statement = (
        select(TimeEntry)
        .where(
            TimeEntry.employee_id == employee_id,
            TimeEntry.work_date >= first,
            TimeEntry.work_date <= last,
            TimeEntry.deleted_at.is_(None),
            TimeEntry.check_out_at.is_not(None),
            TimeEntry.status.in_(_PAYABLE_STATUSES),
        )
        .order_by(TimeEntry.work_date, TimeEntry.check_in_at)
    )
    return list(session.scalars(statement))


# --------------------------------------------------------------------------- assembling the days


def _rates_for(employee: Employee, on_date: date) -> BucketRates:
    """The three pay rates in force on `on_date`, or raise `MissingRate` (Requirement 16.9).

    Reuses `app.services.employee.resolve_rate`, the same resolver the employee screen uses, so a
    mid-month rate change is applied per date without payroll owning a second copy of the rule.
    """
    rate = employee_service.resolve_rate(employee, on_date)
    if rate is None:
        raise MissingRate(employee_id=employee.id, work_date=on_date)
    return BucketRates(
        regular=rate.hourly_wage,
        overtime=rate.overtime_rate,
        shabbat_holiday=rate.shabbat_holiday_rate,
    )


def _travel_for_month(employee: Employee, work_dates: Sequence[date]) -> Decimal:
    """The month's travel allowance: the daily allowance in force on each worked date, summed.

    Travel is a daily allowance (Requirement 3.3, 16.5), paid once per day the employee worked, at the
    rate in force on that date so a mid-month rate change moves it too. The days are the distinct work
    dates that had payable hours, so a day worked at two sites pays one travel allowance, not two.
    """
    total = Decimal("0")
    for work_date in sorted(set(work_dates)):
        rate = employee_service.resolve_rate(employee, work_date)
        if rate is not None:
            total += rate.travel_allowance_daily
    return total


def build_day_inputs(
    session: Session,
    employee: Employee,
    entries: Sequence[TimeEntry],
    classification: ClassificationSettings,
) -> list[DayInput]:
    """Turn the month's payable entries into the per-day, per-site input the pure module prices.

    For each work date: classify that date's completed entries with `classify_day` keyed by site id,
    fold the per-entry split into one `SiteDayMinutes` per site (a day can hold several entries at the
    same site), and pair the day's minutes with the `BucketRates` in force on that date. The result is
    a list of `DayInput`, one per work date, in date order — exactly what `compute_payroll` consumes.
    """
    by_date: dict[date, list[TimeEntry]] = defaultdict(list)
    for entry in entries:
        by_date[entry.work_date].append(entry)

    days: list[DayInput] = []
    for work_date in sorted(by_date):
        day_entries = by_date[work_date]
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

        # Fold the per-entry split into one bucket total per site for the day.
        per_site: dict[uuid.UUID, dict[str, int]] = defaultdict(
            lambda: {"regular": 0, "overtime": 0, "shabbat": 0, "holiday": 0}
        )
        for entry_class in classified.entries:
            bucket = per_site[entry_class.key]
            bucket["regular"] += entry_class.regular_minutes
            bucket["overtime"] += entry_class.overtime_minutes
            bucket["shabbat"] += entry_class.shabbat_minutes
            bucket["holiday"] += entry_class.holiday_minutes

        sites = tuple(
            SiteDayMinutes(
                site_id=site_id,
                regular_minutes=bucket["regular"],
                overtime_minutes=bucket["overtime"],
                shabbat_minutes=bucket["shabbat"],
                holiday_minutes=bucket["holiday"],
            )
            for site_id, bucket in per_site.items()
        )

        days.append(
            DayInput(
                work_date=work_date,
                rates=_rates_for(employee, work_date),
                sites=sites,
            )
        )
    return days


# --------------------------------------------------------------------------- the calculation


@dataclass(frozen=True, slots=True)
class PayrollInputs:
    """The month's allowances that sit outside the hourly calculation (Requirement 16.5).

    `travel` defaults to the daily travel allowance summed over the days worked; `bonuses` and
    `deductions` default to nothing. A caller may override `travel` to record an adjusted figure, but
    the common path lets the service derive it from the rate history so a mid-month change is honoured.
    """

    travel: Decimal | None = None
    bonuses: Decimal | None = None
    deductions: Decimal | None = None


def calculate_payroll(
    session: Session,
    *,
    employee_id: uuid.UUID,
    year: int,
    month: int,
    inputs: PayrollInputs | None = None,
) -> PayrollRecord:
    """Compute and persist one employee's payroll for a month, replacing any existing draft (Req 16).

    The steps:

    1. Load the employee with its rate history, or raise `EmployeeNotFound`.
    2. Read the month's payable (Approved or Locked, completed) entries (Requirement 15.8).
    3. Assemble the per-day, per-site minutes and the rate in force on each date (Requirement 16.9),
       reusing `classify_day` and `resolve_rate`.
    4. Derive the month's travel allowance from the days worked unless the caller supplied one, and
       take bonuses and deductions from the inputs (default zero).
    5. Price everything with the pure `compute_payroll` — bucket pay rounded `ROUND_HALF_UP`, and the
       per-site cost allocation corrected so it sums to the worked pay exactly (Requirement 16.7, 16.8).
    6. Upsert the `payroll_records` row and rebuild its allocations, so a recalculation replaces the
       draft rather than duplicating it (Requirement 16.10). The record is `final` when the month is
       locked, `draft` otherwise.

    Nothing is committed; the router commits so the record and its allocations land together.
    """
    employee = session.scalars(
        select(Employee).options(selectinload(Employee.rates)).where(Employee.id == employee_id)
    ).one_or_none()
    if employee is None:
        raise EmployeeNotFound(employee_id)

    first, last = period_service.month_bounds(year, month)
    classification = build_classification_settings(session, first=first, last=last)
    entries = payable_entries(session, employee_id=employee_id, year=year, month=month)
    days = build_day_inputs(session, employee, entries, classification)

    given = inputs or PayrollInputs()
    worked_dates = [entry.work_date for entry in entries]
    travel = given.travel if given.travel is not None else _travel_for_month(employee, worked_dates)
    bonuses = given.bonuses if given.bonuses is not None else Decimal("0")
    deductions = given.deductions if given.deductions is not None else Decimal("0")

    computation = compute_payroll(
        days, travel=travel, bonuses=bonuses, deductions=deductions
    )

    status = _status_for_month(session, year, month)
    return _upsert_record(
        session,
        employee_id=employee_id,
        year=year,
        month=month,
        computation=computation,
        status=status,
    )


def _status_for_month(session: Session, year: int, month: int) -> CalculationStatus:
    """Whether the record is `final` (the month is locked) or a `draft` (still open).

    A locked month's entries are frozen (Requirement 15.4), so the payroll computed from them is final;
    an open month's record is a draft a later recalculation overwrites (Requirement 16.10).
    """
    period = period_service.get_period(session, year, month)
    if period is not None and period.is_locked:
        return CalculationStatus.FINAL
    return CalculationStatus.DRAFT


def _upsert_record(
    session: Session,
    *,
    employee_id: uuid.UUID,
    year: int,
    month: int,
    computation: PayrollComputation,
    status: CalculationStatus,
) -> PayrollRecord:
    """Create or replace the employee-month record and rebuild its allocations (Requirement 16.10).

    The `(employee_id, year, month)` unique constraint means at most one record exists per
    employee-month; a recalculation finds it, overwrites its figures, and clears and rebuilds its
    allocations rather than inserting a second row. The allocations are deleted through the
    relationship (cascade delete-orphan) so a site that no longer has hours does not leave a stale row.
    """
    record = session.scalars(
        select(PayrollRecord)
        .options(selectinload(PayrollRecord.allocations))
        .where(
            PayrollRecord.employee_id == employee_id,
            PayrollRecord.year == year,
            PayrollRecord.month == month,
        )
    ).one_or_none()

    if record is None:
        record = PayrollRecord(employee_id=employee_id, year=year, month=month)
        session.add(record)

    record.regular_minutes = computation.regular_minutes
    record.overtime_minutes = computation.overtime_minutes
    record.shabbat_minutes = computation.shabbat_minutes
    record.holiday_minutes = computation.holiday_minutes
    record.regular_pay = computation.regular_pay
    record.overtime_pay = computation.overtime_pay
    record.shabbat_pay = computation.shabbat_pay
    record.holiday_pay = computation.holiday_pay
    record.travel = computation.travel
    record.bonuses = computation.bonuses
    record.deductions = computation.deductions
    record.total_pay = computation.total_pay
    record.status = status
    record.calculated_at = datetime.now(UTC)

    # Replace the allocations wholesale: clearing the collection deletes the old rows (delete-orphan),
    # and appending the fresh ones inserts them, so a recalculation is a clean rebuild.
    record.allocations.clear()
    session.flush()
    for allocation in computation.allocations:
        record.allocations.append(
            PayrollSiteAllocation(
                site_id=allocation.site_id,
                regular_minutes=allocation.regular_minutes,
                overtime_minutes=allocation.overtime_minutes,
                shabbat_minutes=allocation.shabbat_minutes,
                holiday_minutes=allocation.holiday_minutes,
                cost=allocation.cost,
            )
        )
    session.flush()
    return record


# --------------------------------------------------------------------------- reads


def get_record(
    session: Session, *, employee_id: uuid.UUID, year: int, month: int
) -> PayrollRecord:
    """One employee-month payroll record with its allocations, or raise `PayrollRecordNotFound`."""
    record = session.scalars(
        select(PayrollRecord)
        .options(selectinload(PayrollRecord.allocations))
        .where(
            PayrollRecord.employee_id == employee_id,
            PayrollRecord.year == year,
            PayrollRecord.month == month,
        )
    ).one_or_none()
    if record is None:
        raise PayrollRecordNotFound(f"no payroll record for {employee_id} {year}-{month:02d}")
    return record


@dataclass(frozen=True, slots=True)
class PayrollPage:
    """A page of payroll records plus the unfiltered-by-paging total, for list rendering."""

    items: Sequence[PayrollRecord]
    total: int


def list_records(
    session: Session,
    *,
    year: int | None = None,
    month: int | None = None,
    employee_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> PayrollPage:
    """A stable-sorted page of payroll records, filtered by period or employee (Requirement 16.6).

    Ordered by year then month then employee so a period's records read in a stable order across
    pages. The allocations are eager-loaded so the list can show the per-site breakdown without a
    query per row.
    """
    conditions = []
    if year is not None:
        conditions.append(PayrollRecord.year == year)
    if month is not None:
        conditions.append(PayrollRecord.month == month)
    if employee_id is not None:
        conditions.append(PayrollRecord.employee_id == employee_id)

    total = session.scalar(
        select(func.count()).select_from(PayrollRecord).where(*conditions)
    )
    statement = (
        select(PayrollRecord)
        .options(selectinload(PayrollRecord.allocations))
        .where(*conditions)
        .order_by(
            PayrollRecord.year.desc(),
            PayrollRecord.month.desc(),
            PayrollRecord.employee_id,
        )
        .limit(limit)
        .offset(offset)
    )
    return PayrollPage(items=list(session.scalars(statement)), total=int(total or 0))


__all__ = [
    "EmployeeNotFound",
    "MissingRate",
    "PayrollError",
    "PayrollInputs",
    "PayrollPage",
    "PayrollRecordNotFound",
    "build_classification_settings",
    "build_day_inputs",
    "calculate_payroll",
    "get_record",
    "list_records",
    "payable_entries",
]
