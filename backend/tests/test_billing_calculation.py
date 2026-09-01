"""The billing arithmetic — the pure module every billing and profit figure is downstream of.

`app.calculations.billing` is pure, so these run against it with no database and no clock: plain
`DayInput`s with `SiteBillingRates`, a site→client map and a site→cost map go in, a
`BillingComputation` comes out, and the figures are asserted to the agora. The scenarios are the ones
named in the brief and the task:

* the brief's example — 4.5 h × 60 ₪ at Site A plus 5.0 h × 75 ₪ at Site B = 645 ₪, cost 332.50 ₪
  (the payroll split of 157.50 + 175.00), profit 312.50 ₪ (Requirement 17.1, 17.4);
* the site overtime billing rate used for overtime hours where configured, and the standard rate
  where not (Requirement 17.2);
* a mid-month site-rate change applied per work date (Requirement 17.1);
* per-client aggregation over a client's sites (Requirement 17.3);
* profit as billing minus allocated cost, per site, per client and in total (Requirement 17.4).

Validates: Requirements 17.1, 17.2, 17.3, 17.4
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.calculations.billing import (
    DayInput,
    SiteBillingRates,
    compute_billing,
    round_money,
)


def _rate(regular: str, overtime: str | None = None) -> SiteBillingRates:
    return SiteBillingRates(
        regular=Decimal(regular),
        overtime=Decimal(overtime) if overtime is not None else None,
    )


# --------------------------------------------------------------------------- the brief's example


def test_the_briefs_example_bills_645_costs_332_50_and_profits_312_50():
    """Requirement 17.1, 17.4: 4.5 h × 60 ₪ + 5.0 h × 75 ₪ = 645 ₪; cost 332.50; profit 312.50.

    Site A bills 4.5 h at 60 ₪ = 270.00; Site B bills 5.0 h at 75 ₪ = 375.00; the two sum to 645.00.
    The payroll cost allocated to the day was 157.50 at A and 175.00 at B, totalling 332.50, so the
    profit is 645.00 − 332.50 = 312.50. Both sites belong to one client, so the client total is the
    whole 645.00 / 332.50 / 312.50.
    """
    # 4.5 h = 270 min at Site A; 5.0 h = 300 min at Site B. Regular/overtime split is immaterial here
    # because each site's billing rate is flat, so all minutes are booked regular for the example.
    days = [
        DayInput(
            work_date=date(2025, 8, 4),
            site_id="A",
            rates=_rate("60.00"),
            regular_minutes=270,
        ),
        DayInput(
            work_date=date(2025, 8, 4),
            site_id="B",
            rates=_rate("75.00"),
            regular_minutes=300,
        ),
    ]
    site_clients = {"A": "client-1", "B": "client-1"}
    site_costs = {"A": Decimal("157.50"), "B": Decimal("175.00")}

    result = compute_billing(days, site_clients=site_clients, site_costs=site_costs)

    by_site = {site.site_id: site for site in result.sites}
    assert by_site["A"].billing == Decimal("270.00")
    assert by_site["B"].billing == Decimal("375.00")
    assert by_site["A"].profit == Decimal("112.50")  # 270.00 − 157.50
    assert by_site["B"].profit == Decimal("200.00")  # 375.00 − 175.00

    assert result.total_billing == Decimal("645.00")
    assert result.total_cost == Decimal("332.50")
    assert result.total_profit == Decimal("312.50")

    # One client owns both sites, so the client total is the grand total.
    assert len(result.clients) == 1
    client = result.clients[0]
    assert client.client_id == "client-1"
    assert client.billing == Decimal("645.00")
    assert client.cost == Decimal("332.50")
    assert client.profit == Decimal("312.50")


# --------------------------------------------------------------------------- overtime billing rate


def test_overtime_bills_at_the_site_overtime_rate_where_configured():
    """Requirement 17.2: overtime minutes bill at the site's overtime billing rate when one is set.

    A site with a 60 ₪ standard rate and a 90 ₪ overtime rate: 8 h regular (480 min) at 60 = 480.00,
    plus 2 h overtime (120 min) at 90 = 180.00, totalling 660.00. Proof the overtime bucket uses the
    overtime rate, not the standard one.
    """
    days = [
        DayInput(
            work_date=date(2025, 8, 4),
            site_id="A",
            rates=_rate("60.00", "90.00"),
            regular_minutes=480,
            overtime_minutes=120,
        )
    ]

    result = compute_billing(days, site_clients={"A": "c"}, site_costs={})

    assert result.sites[0].billing == Decimal("660.00")
    assert result.sites[0].billing_rate_applied == Decimal("60.00")
    assert result.sites[0].overtime_rate_applied == Decimal("90.00")


def test_overtime_bills_at_the_standard_rate_where_no_overtime_rate_is_configured():
    """Requirement 17.2: with no overtime rate, overtime bills at the standard rate.

    A site with only a 60 ₪ standard rate: 8 h regular + 2 h overtime, all at 60, is 10 h × 60 =
    600.00. The overtime bucket falls back to the standard rate because none is configured.
    """
    days = [
        DayInput(
            work_date=date(2025, 8, 4),
            site_id="A",
            rates=_rate("60.00"),  # no overtime rate
            regular_minutes=480,
            overtime_minutes=120,
        )
    ]

    result = compute_billing(days, site_clients={"A": "c"}, site_costs={})

    assert result.sites[0].billing == Decimal("600.00")
    assert result.sites[0].overtime_rate_applied is None


# --------------------------------------------------------------------------- mid-month rate change


def test_a_mid_month_site_rate_change_is_applied_per_date():
    """Requirement 17.1: days on either side of a site-rate change bill at the rate in force then.

    Ten 8-hour days at 60 ₪ (before the change) and ten at 70 ₪ (after): 10 × (8 × 60) + 10 ×
    (8 × 70) = 4,800 + 5,600 = 10,400 ₪ — proof the site rate is resolved per work date, not applied
    uniformly across the month.
    """
    days = [
        DayInput(
            work_date=date(2025, 8, day),
            site_id="A",
            rates=_rate("60.00"),
            regular_minutes=480,
        )
        for day in range(1, 11)
    ]
    days += [
        DayInput(
            work_date=date(2025, 8, day),
            site_id="A",
            rates=_rate("70.00"),
            regular_minutes=480,
        )
        for day in range(16, 26)
    ]

    result = compute_billing(days, site_clients={"A": "c"}, site_costs={})

    assert result.sites[0].billing == Decimal("10400.00")
    # The record stores the latest rate for the invoice — 70 ₪, the rate in force on the last day.
    assert result.sites[0].billing_rate_applied == Decimal("70.00")
    assert result.total_billing == Decimal("10400.00")


# --------------------------------------------------------------------------- client aggregation


def test_billing_aggregates_per_client_across_a_clients_sites():
    """Requirement 17.3: a client's billing is the sum over that client's sites.

    Two clients, three sites: client-1 owns A (600.00) and B (300.00); client-2 owns C (450.00). The
    per-client totals are 900.00 and 450.00, and the grand total is 1,350.00.
    """
    days = [
        DayInput(work_date=date(2025, 8, 1), site_id="A", rates=_rate("60.00"), regular_minutes=600),
        DayInput(work_date=date(2025, 8, 1), site_id="B", rates=_rate("60.00"), regular_minutes=300),
        DayInput(work_date=date(2025, 8, 1), site_id="C", rates=_rate("90.00"), regular_minutes=300),
    ]
    site_clients = {"A": "client-1", "B": "client-1", "C": "client-2"}

    result = compute_billing(days, site_clients=site_clients, site_costs={})

    by_client = {client.client_id: client for client in result.clients}
    assert by_client["client-1"].billing == Decimal("900.00")
    assert by_client["client-2"].billing == Decimal("450.00")
    assert result.total_billing == Decimal("1350.00")


# --------------------------------------------------------------------------- profit and totals


def test_profit_is_billing_minus_allocated_cost_per_site_and_in_total():
    """Requirement 17.4: profit is billing minus the cost payroll allocated to the site.

    Site A bills 600.00 against a cost of 350.00 (profit 250.00); Site B bills 300.00 against 320.00
    (a 20.00 loss). The total profit is 250.00 − 20.00 = 230.00, and it equals total billing 900.00
    minus total cost 670.00.
    """
    days = [
        DayInput(work_date=date(2025, 8, 1), site_id="A", rates=_rate("60.00"), regular_minutes=600),
        DayInput(work_date=date(2025, 8, 1), site_id="B", rates=_rate("60.00"), regular_minutes=300),
    ]
    site_clients = {"A": "c", "B": "c"}
    site_costs = {"A": Decimal("350.00"), "B": Decimal("320.00")}

    result = compute_billing(days, site_clients=site_clients, site_costs=site_costs)

    by_site = {site.site_id: site for site in result.sites}
    assert by_site["A"].profit == Decimal("250.00")
    assert by_site["B"].profit == Decimal("-20.00")
    assert result.total_billing == Decimal("900.00")
    assert result.total_cost == Decimal("670.00")
    assert result.total_profit == Decimal("230.00")


def test_a_site_with_no_allocated_cost_shows_full_billing_as_profit():
    """Requirement 17.4: a site absent from the cost map has zero cost, so its billing is its profit.

    An unpaid month — payroll not yet run — bills but carries no cost, so the profit equals the
    billing rather than raising or defaulting to a loss.
    """
    days = [
        DayInput(work_date=date(2025, 8, 1), site_id="A", rates=_rate("60.00"), regular_minutes=480)
    ]

    result = compute_billing(days, site_clients={"A": "c"}, site_costs={})

    assert result.sites[0].billing == Decimal("480.00")
    assert result.sites[0].cost == Decimal("0.00")
    assert result.sites[0].profit == Decimal("480.00")


# --------------------------------------------------------------------------- rounding


def test_billing_is_rounded_half_up_per_day():
    """Requirement 17.1: a fractional-agora product rounds half-up, at the smallest priced unit.

    One minute at 61 ₪/h is 1.01666… → 1.02. `round_money` rounds the half-agora up, matching the
    payroll module so a billing figure and the cost it is compared against round the same way.
    """
    assert round_money(Decimal("1.015")) == Decimal("1.02")

    days = [
        DayInput(work_date=date(2025, 8, 1), site_id="A", rates=_rate("61.00"), regular_minutes=1)
    ]
    result = compute_billing(days, site_clients={"A": "c"}, site_costs={})
    assert result.sites[0].billing == Decimal("1.02")
