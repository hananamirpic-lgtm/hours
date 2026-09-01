"""Payroll computation: bucket pay, monthly totals, and per-site cost allocation.

This is the second pure calculation module (after `app.calculations.hours`), and everything it does
is arithmetic over plain values — no database, no HTTP, no clock. It takes the four-bucket split a
day produced (`app.calculations.hours.classify_day`), the rates in force on each work date, and the
month's allowances and deductions, and returns the monthly pay figures plus the per-site cost
allocation. The service layer resolves the entries, the rates and the settings and hands them here;
this module only computes, so the money logic is reviewable against the figures named in the brief
without standing a database up (design, "Calculation modules … pure functions over plain values").

The rules it implements, from the design's "Payroll" section and Requirement 16.5–16.9:

**Bucket pay (16.5, 16.7).** Each bucket's pay is `minutes / 60 × rate_for_bucket_on_that_date`,
computed with `Decimal` and rounded to two places with `ROUND_HALF_UP` — never binary float, which
drifts. Regular minutes pay at the hourly wage, overtime at the overtime rate, and Shabbat and
holiday minutes at the shabbat/holiday rate; the rate used is the one in force on the work date the
minutes belong to, so a mid-month rate change splits the month at the right day (16.9). Rounding is
applied to each *day's* bucket product before summing, so the monthly figure is the sum of the daily
amounts a payslip would list, not a re-rounding of a fractional running total.

**Monthly pay (16.5).** Total pay is the sum of the four bucket pays, plus the travel allowance and
any bonuses, minus deductions. Travel, bonuses and deductions are allowances, not hours, so they sit
outside the per-site cost allocation below.

**Per-site cost allocation (16.8).** Each site bears the cost of the hours worked there: the same
bucket-times-rate arithmetic, restricted to that site's minutes. The catch is that the independently
rounded per-site sums need not add up to the independently rounded grand total — two numbers each
rounded to the agora can differ from their rounded sum by an agora or two. A stray agora between the
payroll total and the sum of the site costs would undermine every profit figure downstream, so the
allocation is corrected with the *largest-remainder method*: compute each site's exact (unrounded)
cost, round each down, then hand the leftover agorot one at a time to the sites whose fractional part
was largest. The corrected costs sum to the rounded grand total exactly, by construction.

The grand total the site costs must reconcile to is the *worked pay* — the four bucket pays summed —
not `total_pay`, which carries the allowances a site does not bear. This matches the design's
`payroll_site_allocations.cost … must sum to the parent's pay excluding allowances`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

# Two-place quantum for every money figure the system stores (design, "money is NUMERIC(12,2)").
_CENTS = Decimal("0.01")
_MINUTES_PER_HOUR = Decimal("60")


def round_money(value: Decimal) -> Decimal:
    """Quantise a monetary amount to two decimal places with `ROUND_HALF_UP` (Requirement 16.7).

    The one rounding rule in the module, so a half-agora always rounds the same way — up — rather
    than to-even, which is Python's `Decimal` default and would surprise an accountant reconciling by
    hand. Everything monetary passes through here.
    """
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- rates


@dataclass(frozen=True, slots=True)
class BucketRates:
    """The three pay rates in force on one work date (from the employee's rate history, 16.9).

    `regular` is the hourly wage, `overtime` the overtime rate, and `shabbat_holiday` the premium for
    Shabbat and holiday minutes, which share one rate. The service resolves these per date, so a day
    on either side of a mid-month rate change carries the rate that was in force when it was worked.
    """

    regular: Decimal
    overtime: Decimal
    shabbat_holiday: Decimal


# --------------------------------------------------------------------------- daily input


@dataclass(frozen=True, slots=True)
class SiteDayMinutes:
    """One site's four-bucket minute split on one day, keyed by the site it was worked at.

    The service builds one of these per (site, day) from `classify_day`'s per-entry output, summing
    the entries at the same site on the same day. `site_id` is opaque here — the module totals by it
    for allocation but never interprets it.
    """

    site_id: object
    regular_minutes: int = 0
    overtime_minutes: int = 0
    shabbat_minutes: int = 0
    holiday_minutes: int = 0


@dataclass(frozen=True, slots=True)
class DayInput:
    """Everything the module needs to price one work date: its per-site minutes and its rates.

    A day carries its own `BucketRates` because the rate in force can differ from day to day within a
    month (16.9). The service pairs each day's minutes with the rate resolved for that day's date and
    passes the list of days here.
    """

    work_date: date
    rates: BucketRates
    sites: Sequence[SiteDayMinutes]


# --------------------------------------------------------------------------- outputs


@dataclass(frozen=True, slots=True)
class SiteAllocation:
    """One site's share of the monthly cost: its minutes per bucket and its corrected cost.

    `cost` is the largest-remainder-corrected figure, so the sum of `cost` across every allocation
    equals the record's worked pay to the agora (Requirement 16.8). The minute totals are the month's
    minutes at this site, carried so the allocation row records what the cost was computed from.
    """

    site_id: object
    regular_minutes: int = 0
    overtime_minutes: int = 0
    shabbat_minutes: int = 0
    holiday_minutes: int = 0
    cost: Decimal = Decimal("0.00")

    @property
    def total_minutes(self) -> int:
        return self.regular_minutes + self.overtime_minutes + self.shabbat_minutes + self.holiday_minutes


@dataclass(frozen=True, slots=True)
class PayrollComputation:
    """The computed payroll for one employee-month: bucket minutes, bucket pay, totals, allocations.

    `worked_pay` is the sum of the four bucket pays — the figure the per-site `allocations` reconcile
    to (Requirement 16.8). `total_pay` adds travel and bonuses and subtracts deductions
    (Requirement 16.5). The minute totals are the month's minutes per bucket, for the payroll record's
    columns without re-summing the allocations.
    """

    regular_minutes: int = 0
    overtime_minutes: int = 0
    shabbat_minutes: int = 0
    holiday_minutes: int = 0

    regular_pay: Decimal = Decimal("0.00")
    overtime_pay: Decimal = Decimal("0.00")
    shabbat_pay: Decimal = Decimal("0.00")
    holiday_pay: Decimal = Decimal("0.00")

    travel: Decimal = Decimal("0.00")
    bonuses: Decimal = Decimal("0.00")
    deductions: Decimal = Decimal("0.00")

    worked_pay: Decimal = Decimal("0.00")
    total_pay: Decimal = Decimal("0.00")

    allocations: tuple[SiteAllocation, ...] = field(default_factory=tuple)

    @property
    def total_minutes(self) -> int:
        return self.regular_minutes + self.overtime_minutes + self.shabbat_minutes + self.holiday_minutes


# --------------------------------------------------------------------------- pricing helpers


def _minutes_pay(minutes: int, rate: Decimal) -> Decimal:
    """The pay for `minutes` at `rate` per hour, rounded to the agora (Requirement 16.5, 16.7).

    `minutes / 60 × rate`, all in `Decimal`. Rounded here, at the smallest unit the payslip prices —
    one bucket on one day — so the monthly and per-site figures are sums of amounts that were each
    already rounded, which is what makes them reconcile without a second rounding introducing a drift.
    """
    if minutes == 0:
        return Decimal("0.00")
    return round_money(Decimal(minutes) / _MINUTES_PER_HOUR * rate)


def _bucket_pay_for_site(site: SiteDayMinutes, rates: BucketRates) -> Decimal:
    """The unrounded... no: the *rounded* worked pay for one site on one day, summed over its buckets.

    Each bucket is priced and rounded independently, then summed, so a site's daily cost is the sum of
    the four amounts a payslip would show for that day. Shabbat and holiday minutes share the premium
    rate but are kept as separate buckets so the record can report the split.
    """
    return (
        _minutes_pay(site.regular_minutes, rates.regular)
        + _minutes_pay(site.overtime_minutes, rates.overtime)
        + _minutes_pay(site.shabbat_minutes, rates.shabbat_holiday)
        + _minutes_pay(site.holiday_minutes, rates.shabbat_holiday)
    )


# --------------------------------------------------------------------------- the computation


def compute_payroll(
    days: Iterable[DayInput],
    *,
    travel: Decimal,
    bonuses: Decimal,
    deductions: Decimal,
) -> PayrollComputation:
    """Compute one employee's monthly payroll from their priced days and allowances (Requirement 16).

    Pure: no database, no clock. `days` is the month's work dates, each carrying its per-site minute
    split and the rates in force on that date. `travel`, `bonuses` and `deductions` are the month's
    allowances, applied once to the total and excluded from the per-site allocation.

    The steps:

    1. Price each day's minutes bucket by bucket at that day's rates, rounding each product to the
       agora, and accumulate the four bucket pays and the four minute totals across the month.
    2. Accumulate each site's exact (unrounded) cost — the sum of its daily rounded bucket pays —
       so the allocation can be corrected against the grand total without a rounding drift.
    3. `worked_pay` is the sum of the four bucket pays; `total_pay` adds travel and bonuses and
       subtracts deductions (Requirement 16.5).
    4. Allocate `worked_pay` across the sites with the largest-remainder method, so the site costs
       sum to `worked_pay` exactly (Requirement 16.8).
    """
    regular_minutes = overtime_minutes = shabbat_minutes = holiday_minutes = 0
    regular_pay = overtime_pay = shabbat_pay = holiday_pay = Decimal("0.00")

    # Per-site accumulators. `_SiteAccumulator.cost` is the sum of the site's daily rounded bucket
    # pays — the exact per-site figure the allocation rounds and corrects.
    site_order: list[object] = []
    site_acc: dict[object, _SiteAccumulator] = {}

    for day in days:
        for site in day.sites:
            regular_minutes += site.regular_minutes
            overtime_minutes += site.overtime_minutes
            shabbat_minutes += site.shabbat_minutes
            holiday_minutes += site.holiday_minutes

            regular_pay += _minutes_pay(site.regular_minutes, day.rates.regular)
            overtime_pay += _minutes_pay(site.overtime_minutes, day.rates.overtime)
            shabbat_pay += _minutes_pay(site.shabbat_minutes, day.rates.shabbat_holiday)
            holiday_pay += _minutes_pay(site.holiday_minutes, day.rates.shabbat_holiday)

            acc = site_acc.get(site.site_id)
            if acc is None:
                acc = _SiteAccumulator(site_id=site.site_id)
                site_acc[site.site_id] = acc
                site_order.append(site.site_id)
            acc.add(site, _bucket_pay_for_site(site, day.rates))

    worked_pay = regular_pay + overtime_pay + shabbat_pay + holiday_pay
    total_pay = round_money(worked_pay + travel + bonuses - deductions)

    allocations = _allocate_cost(
        [site_acc[site_id] for site_id in site_order], target_total=worked_pay
    )

    return PayrollComputation(
        regular_minutes=regular_minutes,
        overtime_minutes=overtime_minutes,
        shabbat_minutes=shabbat_minutes,
        holiday_minutes=holiday_minutes,
        regular_pay=regular_pay,
        overtime_pay=overtime_pay,
        shabbat_pay=shabbat_pay,
        holiday_pay=holiday_pay,
        travel=round_money(travel),
        bonuses=round_money(bonuses),
        deductions=round_money(deductions),
        worked_pay=worked_pay,
        total_pay=total_pay,
        allocations=allocations,
    )


# --------------------------------------------------------------------------- allocation


@dataclass(slots=True)
class _SiteAccumulator:
    """A mutable running total of one site's minutes and its exact cost across the month."""

    site_id: object
    regular_minutes: int = 0
    overtime_minutes: int = 0
    shabbat_minutes: int = 0
    holiday_minutes: int = 0
    cost: Decimal = Decimal("0.00")

    def add(self, site: SiteDayMinutes, day_cost: Decimal) -> None:
        self.regular_minutes += site.regular_minutes
        self.overtime_minutes += site.overtime_minutes
        self.shabbat_minutes += site.shabbat_minutes
        self.holiday_minutes += site.holiday_minutes
        self.cost += day_cost


def allocate_largest_remainder(
    exact_costs: Sequence[Decimal], target_total: Decimal
) -> list[Decimal]:
    """Round each cost to the agora so the rounded costs sum exactly to `target_total` (Req 16.8).

    The largest-remainder method. Each exact cost is split into a whole number of agorot (rounded
    *down*) and a fractional remainder. Rounding down alone leaves the sum short of the target by some
    whole number of agorot; that shortfall is distributed one agora at a time to the entries with the
    largest fractional remainders, which is the allocation that moves each figure the least while
    making the total come out exactly. Ties are broken by original order, so the result is
    deterministic and a re-run allocates the leftover agorot the same way.

    `target_total` is itself expected to be a two-place figure — the worked pay, already a sum of
    rounded amounts. The method guarantees `sum(result) == round_money(target_total)` exactly, which
    is the property the allocation exists to hold: the per-site costs reconcile to the total to the
    agora, so no profit figure downstream inherits a stray agora.
    """
    target = round_money(target_total)
    if not exact_costs:
        # Nothing to allocate to. A non-zero target with no sites is a caller error, but returning an
        # empty list rather than raising keeps the module total: the service guarantees at least one
        # site whenever there is pay, because pay only exists where minutes were worked.
        return []

    # Floor each cost to a whole number of agorot, tracking the fractional remainder for the sort.
    floors: list[Decimal] = []
    remainders: list[tuple[Decimal, int]] = []
    for index, cost in enumerate(exact_costs):
        floored = cost.quantize(_CENTS, rounding="ROUND_DOWN")
        floors.append(floored)
        remainders.append((cost - floored, index))

    allocated = sum(floors, Decimal("0.00"))
    # The shortfall, in whole agorot, to hand out. Positive in the normal case (flooring undershot);
    # it is a count of one-agora steps, computed in integer agorot to avoid any Decimal drift.
    shortfall_agorot = int(((target - allocated) / _CENTS).to_integral_value(rounding=ROUND_HALF_UP))

    if shortfall_agorot > 0:
        # Largest fractional remainder first; original index breaks ties so the result is stable.
        order = sorted(remainders, key=lambda item: (-item[0], item[1]))
        for _, index in order[:shortfall_agorot]:
            floors[index] += _CENTS

    return floors


def _allocate_cost(
    accumulators: Sequence[_SiteAccumulator], *, target_total: Decimal
) -> tuple[SiteAllocation, ...]:
    """Turn the per-site exact costs into corrected `SiteAllocation`s summing to `target_total`."""
    corrected = allocate_largest_remainder([acc.cost for acc in accumulators], target_total)
    return tuple(
        SiteAllocation(
            site_id=acc.site_id,
            regular_minutes=acc.regular_minutes,
            overtime_minutes=acc.overtime_minutes,
            shabbat_minutes=acc.shabbat_minutes,
            holiday_minutes=acc.holiday_minutes,
            cost=cost,
        )
        for acc, cost in zip(accumulators, corrected, strict=True)
    )


__all__ = [
    "BucketRates",
    "DayInput",
    "PayrollComputation",
    "SiteAllocation",
    "SiteDayMinutes",
    "allocate_largest_remainder",
    "compute_payroll",
    "round_money",
]
