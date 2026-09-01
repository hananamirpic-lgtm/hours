"""The Israeli holiday calendar is computed, so it needs pinning against known dates.

The arithmetic in `app.core.hebrew_calendar` is exact for the civil Hebrew calendar, but "exact"
is only worth anything if a regression in it is caught. So the 2025 national-holiday dates — which
are published fact — are asserted directly: if a refactor breaks the conversion, these fail with a
concrete wrong date rather than a vague "looks off".
"""

from __future__ import annotations

from datetime import date

from app.core.hebrew_calendar import Holiday, israeli_holidays_for_gregorian_year

# The published national holidays of 2025 (the days work stops), in date order. Cross-checked against
# the Israeli civil calendar; Independence Day is 1 May, already off Shabbat, so no shift applies.
_EXPECTED_2025 = [
    (date(2025, 4, 13), "Passover (Day 1)"),
    (date(2025, 4, 19), "Passover (Day 7)"),
    (date(2025, 5, 1), "Independence Day"),
    (date(2025, 6, 2), "Shavuot"),
    (date(2025, 9, 23), "Rosh Hashanah (Day 1)"),
    (date(2025, 9, 24), "Rosh Hashanah (Day 2)"),
    (date(2025, 10, 2), "Yom Kippur"),
    (date(2025, 10, 7), "Sukkot (Day 1)"),
    (date(2025, 10, 14), "Shemini Atzeret / Simchat Torah"),
]


def test_2025_national_holidays_match_the_published_dates() -> None:
    holidays = israeli_holidays_for_gregorian_year(2025)
    assert [(h.date, h.name_en) for h in holidays] == _EXPECTED_2025


def test_every_holiday_carries_both_languages() -> None:
    for holiday in israeli_holidays_for_gregorian_year(2026):
        assert holiday.name_he, "a holiday must have a Hebrew name"
        assert holiday.name_en, "a holiday must have an English name"
        assert isinstance(holiday, Holiday)


def test_results_fall_only_in_the_requested_year_and_are_ordered() -> None:
    for year in (2024, 2025, 2026, 2027, 2028):
        holidays = israeli_holidays_for_gregorian_year(year)
        assert all(h.date.year == year for h in holidays)
        assert [h.date for h in holidays] == sorted(h.date for h in holidays)


def test_dates_are_unique_within_a_year() -> None:
    # A duplicate date would violate the holidays table's unique constraint on insert, so a clash in
    # the generator is a bug worth catching before it reaches the database.
    for year in (2025, 2026, 2027):
        dates = [h.date for h in israeli_holidays_for_gregorian_year(year)]
        assert len(dates) == len(set(dates))


def test_independence_day_never_falls_on_shabbat() -> None:
    # The whole point of the adjustment: across a decade of computed dates, Yom Ha'atzmaut must never
    # land on a Friday or Saturday.
    for year in range(2024, 2035):
        independence = next(
            h for h in israeli_holidays_for_gregorian_year(year) if h.name_en == "Independence Day"
        )
        assert independence.date.weekday() not in (4, 5), independence.date
