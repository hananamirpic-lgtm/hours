"""Billing computation: per-site billing from hours and the site rate in force per date, and profit.

This is the third pure calculation module, after `app.calculations.hours` and
`app.calculations.payroll`, and like them everything it does is arithmetic over plain values — no
database, no HTTP, no clock. It takes the four-bucket split a day produced
(`app.calculations.hours.classify_day`), the *site's* billing rates in force on each work date, and
the cost already allocated to each site by payroll, and returns the per-site billing and profit plus
the client and grand totals. The service layer resolves the entries, the site rates and the costs and
hands them here; this module only computes, so the money logic is reviewable against the figures
named in the brief without standing a database up (design, "Calculation modules … pure functions
over plain values").

The rules it implements, from the design's "Billing and profit" section and Requirement 17.1–17.4:

**Per-site billing (17.1, 17.2).** A site's billing for a day is

    regular_hours × site_rate(date) + overtime_hours × (site_ot_rate or site_rate)

with `Decimal` and `ROUND_HALF_UP` to two places — never binary float. Regular minutes bill at the
site's standard billing rate; overtime minutes bill at the site's overtime billing rate where one is
configured, and at the standard rate otherwise (17.2). The rate used is the one in force on the work
date the minutes belong to, so a mid-month rate change splits the month at the right day (17.1).
Rounding is applied to each *day's* per-rate product before summing, so the monthly figure is the sum
of the daily amounts an invoice would list, not a re-rounding of a fractional running total — the same
discipline `app.calculations.payroll` follows so billing and cost reconcile the same way.

Only regular and overtime buckets bill: a site's client is billed for the site's own work, and the
Shabbat and holiday premiums are an employee-pay concept (Requirement 16.4), not a separate billing
rate the site carries. Where a shift falls on Shabbat or a holiday, those minutes are classified into
the Shabbat/holiday buckets for pay; the billing module bills the regular and overtime minutes a site
records and leaves premium-bucket minutes to payroll, matching the design's billing formula, which
names only `regular_hours` and `overtime_hours`.

**Profit (17.4).** `profit_site = billing_site − cost_site`, where `cost_site` is the cost payroll
already allocated to that site (`payroll_site_allocations.cost`). The service supplies the cost; this
module subtracts. A site with billing but no allocated cost — an unpaid month, say — shows its full
billing as profit, which is correct: nothing was spent on it yet.

**Client and total aggregation (17.3, 17.4).** Client billing, cost and profit are the sums over that
client's sites; the grand totals are the sums over every site. Because each site's figures are
already rounded to the agora, the client and grand totals are exact sums of rounded amounts and need
no further correction — there is no allocation-to-a-target step here as there is in payroll, because
billing is not split *down* to sites, it is summed *up* from them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

# Two-place quantum for every money figure the system stores (design, "money is NUMERIC(12,2)").
_CENTS = Decimal("0.01")
_MINUTES_PER_HOUR = Decimal("60")


def round_money(value: Decimal) -> Decimal:
    """Quantise a monetary amount to two decimal places with `ROUND_HALF_UP` (Requirement 17.1).

    The one rounding rule in the module, matching `app.calculations.payroll.round_money` so a billing
    figure and the cost it is compared against are rounded the same way — a half-agora always rounds
    up, not to-even, which is Python's `Decimal` default and would surprise an accountant reconciling
    an invoice by hand.
    """
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- rates


@dataclass(frozen=True, slots=True)
class SiteBillingRates:
    """The billing rates in force on one work date, from the site's rate history (17.1, 17.2).

    `regular` is the site's standard billing rate. `overtime` is the site's overtime billing rate
    where one is configured, or `None` where none is; when `None`, overtime minutes bill at the
    standard `regular` rate (Requirement 17.2). The service resolves these per date, so a day on
    either side of a mid-month rate change carries the rate that was in force when it was worked.
    """

    regular: Decimal
    overtime: Decimal | None = None

    @property
    def overtime_effective(self) -> Decimal:
        """The rate overtime minutes actually bill at: the overtime rate, or the standard one (17.2)."""
        return self.overtime if self.overtime is not None else self.regular


# --------------------------------------------------------------------------- daily input


@dataclass(frozen=True, slots=True)
class SiteDayHours:
    """One site's billable minute split on one day: regular and overtime, keyed by the site.

    The service builds one of these per (site, day) from `classify_day`'s per-entry output, summing
    the entries at the same site on the same day. Only the regular and overtime buckets bill; the
    Shabbat and holiday buckets are employee-pay premiums, not a site billing rate, so they are not
    carried here. `site_id` is opaque — the module totals by it but never interprets it.
    """

    site_id: object
    regular_minutes: int = 0
    overtime_minutes: int = 0


@dataclass(frozen=True, slots=True)
class DayInput:
    """Everything the module needs to bill one work date at one site: its minutes and its rates.

    A day carries its own `SiteBillingRates` because the site's rate in force can differ from day to
    day within a month (17.1). The service pairs each (site, day)'s minutes with the rate resolved for
    that site on that date and passes the list of days here.
    """

    work_date: date
    site_id: object
    rates: SiteBillingRates
    regular_minutes: int = 0
    overtime_minutes: int = 0


# --------------------------------------------------------------------------- outputs


@dataclass(frozen=True, slots=True)
class SiteBilling:
    """One site's billing, cost and profit for a month (Requirement 17.1, 17.4).

    `regular_minutes` and `overtime_minutes` are the month's billable minutes at the site; `billing`
    is the summed, rounded per-day billing; `cost` is the cost payroll allocated to the site; `profit`
    is `billing − cost`. `billing_rate_applied` and `overtime_rate_applied` are the rates the record
    stores for the invoice — the rate in force on the last billed day, kept so an already-sent invoice
    reads back the figure it was cut at rather than re-deriving history (design, `billing_records`).
    """

    site_id: object
    client_id: object
    regular_minutes: int = 0
    overtime_minutes: int = 0
    billing_rate_applied: Decimal = Decimal("0.00")
    overtime_rate_applied: Decimal | None = None
    billing: Decimal = Decimal("0.00")
    cost: Decimal = Decimal("0.00")
    profit: Decimal = Decimal("0.00")

    @property
    def total_minutes(self) -> int:
        return self.regular_minutes + self.overtime_minutes


@dataclass(frozen=True, slots=True)
class ClientBilling:
    """One client's billing, cost and profit for a month, summed over its sites (Requirement 17.3)."""

    client_id: object
    billing: Decimal = Decimal("0.00")
    cost: Decimal = Decimal("0.00")
    profit: Decimal = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class BillingComputation:
    """The computed billing for a month: per-site, per-client and grand totals (Requirement 17).

    `sites` is one `SiteBilling` per site with billable hours, in the order the service supplied them;
    `clients` aggregates them by client; `total_*` are the grand totals over every site. The grand
    totals equal the sum of the site figures and the sum of the client figures to the agora, because
    each site figure is already rounded and the totals are plain sums of them (no re-rounding).
    """

    sites: tuple[SiteBilling, ...] = ()
    clients: tuple[ClientBilling, ...] = ()
    total_billing: Decimal = Decimal("0.00")
    total_cost: Decimal = Decimal("0.00")
    total_profit: Decimal = Decimal("0.00")


# --------------------------------------------------------------------------- pricing helpers


def _minutes_billing(minutes: int, rate: Decimal) -> Decimal:
    """The billing for `minutes` at `rate` per hour, rounded to the agora (Requirement 17.1).

    `minutes / 60 × rate`, all in `Decimal`. Rounded here, at the smallest unit an invoice prices —
    one rate on one day — so the monthly and per-client figures are sums of amounts that were each
    already rounded, which is what makes them reconcile without a second rounding introducing a drift.
    """
    if minutes == 0:
        return Decimal("0.00")
    return round_money(Decimal(minutes) / _MINUTES_PER_HOUR * rate)


# --------------------------------------------------------------------------- the computation


@dataclass(slots=True)
class _SiteAccumulator:
    """A mutable running total of one site's billable minutes, billing and applied rates."""

    site_id: object
    client_id: object
    regular_minutes: int = 0
    overtime_minutes: int = 0
    billing: Decimal = Decimal("0.00")
    #: The rates in force on the latest day billed so far — what the record stores for the invoice.
    billing_rate_applied: Decimal = Decimal("0.00")
    overtime_rate_applied: Decimal | None = None
    _latest_date: date | None = None

    def add(self, day: DayInput) -> None:
        self.regular_minutes += day.regular_minutes
        self.overtime_minutes += day.overtime_minutes
        self.billing += _minutes_billing(day.regular_minutes, day.rates.regular)
        self.billing += _minutes_billing(day.overtime_minutes, day.rates.overtime_effective)
        # Keep the rate from the latest work date, so a mid-month change leaves the most recent rate
        # as the one the invoice reads back — the earlier periods are already summed into `billing`.
        if self._latest_date is None or day.work_date >= self._latest_date:
            self._latest_date = day.work_date
            self.billing_rate_applied = day.rates.regular
            self.overtime_rate_applied = day.rates.overtime


def compute_billing(
    days: Iterable[DayInput],
    *,
    site_clients: dict[object, object],
    site_costs: dict[object, Decimal],
) -> BillingComputation:
    """Compute a month's billing and profit from its priced site-days and the allocated costs (Req 17).

    Pure: no database, no clock. `days` is the month's (site, work date) billable minutes, each
    carrying the site's rates in force on that date. `site_clients` maps a site id to its client id,
    so billing can aggregate per client (Requirement 17.3). `site_costs` maps a site id to the cost
    payroll allocated to it (`payroll_site_allocations.cost`), so profit is billing minus that cost
    (Requirement 17.4); a site absent from the map has zero cost.

    The steps:

    1. Accumulate each site's billing across its days — regular at the standard rate, overtime at the
       overtime rate or the standard one (Requirement 17.2), each day's product rounded to the agora.
    2. Subtract the site's allocated cost to get its profit (Requirement 17.4).
    3. Aggregate the site figures per client (Requirement 17.3) and over all sites for the totals.

    Sites are emitted in first-seen order and clients in first-seen order, so the result is stable and
    a re-run produces the identical structure.
    """
    site_order: list[object] = []
    site_acc: dict[object, _SiteAccumulator] = {}

    for day in days:
        acc = site_acc.get(day.site_id)
        if acc is None:
            acc = _SiteAccumulator(
                site_id=day.site_id, client_id=site_clients.get(day.site_id)
            )
            site_acc[day.site_id] = acc
            site_order.append(day.site_id)
        acc.add(day)

    sites: list[SiteBilling] = []
    for site_id in site_order:
        acc = site_acc[site_id]
        billing = round_money(acc.billing)
        cost = round_money(site_costs.get(site_id, Decimal("0.00")))
        sites.append(
            SiteBilling(
                site_id=site_id,
                client_id=acc.client_id,
                regular_minutes=acc.regular_minutes,
                overtime_minutes=acc.overtime_minutes,
                billing_rate_applied=acc.billing_rate_applied,
                overtime_rate_applied=acc.overtime_rate_applied,
                billing=billing,
                cost=cost,
                profit=billing - cost,
            )
        )

    clients = _aggregate_clients(sites)
    total_billing = sum((site.billing for site in sites), Decimal("0.00"))
    total_cost = sum((site.cost for site in sites), Decimal("0.00"))

    return BillingComputation(
        sites=tuple(sites),
        clients=clients,
        total_billing=total_billing,
        total_cost=total_cost,
        total_profit=total_billing - total_cost,
    )


def _aggregate_clients(sites: Sequence[SiteBilling]) -> tuple[ClientBilling, ...]:
    """Sum the site figures per client, in first-seen client order (Requirement 17.3)."""
    order: list[object] = []
    by_client: dict[object, dict[str, Decimal]] = {}
    for site in sites:
        totals = by_client.get(site.client_id)
        if totals is None:
            totals = {"billing": Decimal("0.00"), "cost": Decimal("0.00")}
            by_client[site.client_id] = totals
            order.append(site.client_id)
        totals["billing"] += site.billing
        totals["cost"] += site.cost
    return tuple(
        ClientBilling(
            client_id=client_id,
            billing=by_client[client_id]["billing"],
            cost=by_client[client_id]["cost"],
            profit=by_client[client_id]["billing"] - by_client[client_id]["cost"],
        )
        for client_id in order
    )


__all__ = [
    "BillingComputation",
    "ClientBilling",
    "DayInput",
    "SiteBilling",
    "SiteBillingRates",
    "SiteDayHours",
    "compute_billing",
    "round_money",
]
