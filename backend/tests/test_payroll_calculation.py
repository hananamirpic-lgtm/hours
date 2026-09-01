"""The payroll arithmetic — the pure module every monthly pay figure is downstream of.

`app.calculations.payroll` is pure, so these run against it with no database and no clock: plain
`SiteDayMinutes`, `BucketRates` and allowances go in, a `PayrollComputation` comes out, and the
figures are asserted to the agora. The scenarios are the ones named in the brief and the task:

* 35 ₪ × 212 h = 7,420 ₪ — a flat month priced at the base wage;
* the brief's per-site cost split of 157.50 ₪ and 175.00 ₪ totalling 332.50 ₪;
* the allocation sums equal the total to the agora, including a case engineered so independent
  rounding would drift, proving the largest-remainder correction (Requirement 16.8);
* a mid-month rate change applied per date (Requirement 16.9);
* `ROUND_HALF_UP` to two places (Requirement 16.7).

Validates: Requirements 16.5, 16.7, 16.8, 16.9
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.calculations.payroll import (
    BucketRates,
    DayInput,
    SiteDayMinutes,
    allocate_largest_remainder,
    compute_payroll,
    round_money,
)

# A flat rate card where every bucket pays the same 35 ₪/h, so a cost is exactly hours × 35 and the
# brief's figures fall straight out without an overtime premium confusing the arithmetic.
FLAT_35 = BucketRates(
    regular=Decimal("35.00"), overtime=Decimal("35.00"), shabbat_holiday=Decimal("35.00")
)


def _regular_day(work_date: date, site_id: str, minutes: int, rates: BucketRates = FLAT_35) -> DayInput:
    """A day with all its minutes regular at one site — the shape the flat-rate scenarios use."""
    return DayInput(
        work_date=work_date,
        rates=rates,
        sites=(SiteDayMinutes(site_id=site_id, regular_minutes=minutes),),
    )


# --------------------------------------------------------------------------- 35 ₪ × 212 h = 7,420 ₪


def test_a_flat_month_of_212_hours_at_35_is_7420():
    """Requirement 16.5, 16.7: 212 hours at 35 ₪, all regular, is 7,420.00 ₪.

    212 h is 12,720 minutes. Spread over a plausible month — 20 days, some 636 minutes each — the sum
    is the same whether it is one day or twenty, because each day's product is rounded to the agora and
    35 divides the hours cleanly. The worked pay and the total (no allowances) are both 7,420.00.
    """
    # 20 days × 636 minutes = 12,720 minutes = 212 hours.
    days = [_regular_day(date(2025, 8, day), "A", 636) for day in range(1, 21)]

    result = compute_payroll(days, travel=Decimal("0"), bonuses=Decimal("0"), deductions=Decimal("0"))

    assert result.regular_minutes == 12_720
    assert result.regular_pay == Decimal("7420.00")
    assert result.worked_pay == Decimal("7420.00")
    assert result.total_pay == Decimal("7420.00")


def test_a_single_day_of_212_hours_is_also_7420():
    """The same 7,420 ₪ from one 212-hour block, so the figure does not depend on how days are cut."""
    days = [_regular_day(date(2025, 8, 1), "A", 12_720)]

    result = compute_payroll(days, travel=Decimal("0"), bonuses=Decimal("0"), deductions=Decimal("0"))

    assert result.worked_pay == Decimal("7420.00")


# --------------------------------------------------------------------------- the brief's per-site split


def test_the_briefs_per_site_cost_split_is_157_50_and_175_00():
    """The brief's day: 4.5 h at Site A and 5.0 h at Site B, priced at 35 ₪, splits 157.50 / 175.00.

    Site A is 270 minutes (4.5 h) → 157.50 ₪; Site B is 300 minutes (5.0 h) → 175.00 ₪; the two sum to
    332.50 ₪, the cost figure the billing example (task 26) subtracts from billing to get profit. The
    allocation carries the cost back to the site it was worked at (Requirement 16.8).
    """
    days = [
        DayInput(
            work_date=date(2025, 8, 4),
            rates=FLAT_35,
            sites=(
                SiteDayMinutes(site_id="A", regular_minutes=270),
                SiteDayMinutes(site_id="B", regular_minutes=300),
            ),
        )
    ]

    result = compute_payroll(days, travel=Decimal("0"), bonuses=Decimal("0"), deductions=Decimal("0"))

    assert result.worked_pay == Decimal("332.50")
    by_site = {alloc.site_id: alloc.cost for alloc in result.allocations}
    assert by_site["A"] == Decimal("157.50")
    assert by_site["B"] == Decimal("175.00")
    assert sum(by_site.values()) == Decimal("332.50")


# --------------------------------------------------------------------------- allocation sums to the agora


def test_allocations_sum_exactly_to_the_worked_pay():
    """Requirement 16.8: the per-site costs sum to the worked pay to the agora, always.

    A split of the flat month across two sites: the two allocations must add back to the total the
    payslip shows, with no stray agora between them.
    """
    days = [
        DayInput(
            work_date=date(2025, 8, 4),
            rates=FLAT_35,
            sites=(
                SiteDayMinutes(site_id="A", regular_minutes=270),
                SiteDayMinutes(site_id="B", regular_minutes=300),
            ),
        )
    ]

    result = compute_payroll(days, travel=Decimal("0"), bonuses=Decimal("0"), deductions=Decimal("0"))

    allocated = sum((alloc.cost for alloc in result.allocations), Decimal("0.00"))
    assert allocated == result.worked_pay


def test_largest_remainder_corrects_a_rounding_drift_to_the_agora():
    """Requirement 16.8: three sites whose independent rounding would drift are corrected exactly.

    One minute at each of three sites at 35 ₪/h is 35/60 = 0.58333… ₪ each — 0.58 rounded down, with a
    remainder of 0.00333. Three of them floor to 0.58, summing to 1.74, while the exact total 1.75
    rounds to 1.75; the one-agora shortfall is handed to the site with the largest remainder (a tie
    broken by order), so the corrected costs sum to 1.75 exactly rather than 1.74.
    """
    exact = [Decimal("35") / 60] * 3  # 0.58333… each
    target = round_money(sum(exact, Decimal("0")))  # 1.75

    corrected = allocate_largest_remainder(exact, target)

    assert sum(corrected, Decimal("0.00")) == target
    assert target == Decimal("1.75")
    # Two sites floor to 0.58, one gets the extra agora → 0.59.
    assert sorted(corrected) == [Decimal("0.58"), Decimal("0.58"), Decimal("0.59")]


def test_the_allocation_is_deterministic_across_reruns():
    """Requirement 16.8, 16.10: the same input allocates the leftover agorot the same way every time.

    A stable allocation is what lets a recalculation replace a draft without the per-site figures
    shuffling. The tie-break is original order, so two runs of the same drifting input agree.
    """
    exact = [Decimal("35") / 60] * 3
    target = round_money(sum(exact, Decimal("0")))

    first = allocate_largest_remainder(exact, target)
    second = allocate_largest_remainder(exact, target)

    assert first == second


# --------------------------------------------------------------------------- mid-month rate change


def test_a_mid_month_rate_change_is_applied_per_date():
    """Requirement 16.9: days on either side of a rate change carry the rate in force on that date.

    Ten days at 35 ₪ (before the change) and ten at 40 ₪ (after), one 8-hour day each. The month's pay
    is 10 × (8 × 35) + 10 × (8 × 40) = 2,800 + 3,200 = 6,000 ₪ — proof the rate is resolved per day,
    not applied uniformly across the month.
    """
    before = BucketRates(
        regular=Decimal("35.00"), overtime=Decimal("35.00"), shabbat_holiday=Decimal("35.00")
    )
    after = BucketRates(
        regular=Decimal("40.00"), overtime=Decimal("40.00"), shabbat_holiday=Decimal("40.00")
    )
    days = [_regular_day(date(2025, 8, day), "A", 480, before) for day in range(1, 11)]
    days += [_regular_day(date(2025, 8, day), "A", 480, after) for day in range(16, 26)]

    result = compute_payroll(days, travel=Decimal("0"), bonuses=Decimal("0"), deductions=Decimal("0"))

    assert result.regular_pay == Decimal("6000.00")
    assert result.worked_pay == Decimal("6000.00")


# --------------------------------------------------------------------------- rounding and allowances


def test_pay_is_rounded_half_up_to_two_places():
    """Requirement 16.7: a fractional-agora product rounds half-up, not half-to-even.

    One minute at 35 ₪/h is 0.58333… → 0.58. Seven minutes is 4.08333… → 4.08. A value landing exactly
    on the half-agora rounds up, which is Python's `ROUND_HALF_UP`, not the `Decimal` default.
    """
    assert round_money(Decimal("0.585")) == Decimal("0.59")  # half rounds up
    assert round_money(Decimal("0.584")) == Decimal("0.58")

    day = [_regular_day(date(2025, 8, 1), "A", 1)]
    result = compute_payroll(day, travel=Decimal("0"), bonuses=Decimal("0"), deductions=Decimal("0"))
    assert result.regular_pay == Decimal("0.58")


def test_total_pay_adds_travel_and_bonuses_and_subtracts_deductions():
    """Requirement 16.5: total = worked pay + travel + bonuses − deductions, allowances outside sites.

    A flat 8-hour day at 35 ₪ is 280.00 worked pay. With 20 travel, 50 bonus and 30 deduction the
    total is 280 + 20 + 50 − 30 = 320.00, while the per-site allocation stays at the worked 280.00 —
    a site bears the cost of the hours worked there, not the allowances.
    """
    days = [_regular_day(date(2025, 8, 1), "A", 480)]

    result = compute_payroll(
        days, travel=Decimal("20.00"), bonuses=Decimal("50.00"), deductions=Decimal("30.00")
    )

    assert result.worked_pay == Decimal("280.00")
    assert result.total_pay == Decimal("320.00")
    assert result.allocations[0].cost == Decimal("280.00")
