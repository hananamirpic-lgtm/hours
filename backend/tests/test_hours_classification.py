"""The daily hour split — the calculation every money figure in the system is downstream of.

`classify_day` is pure, so these run against it with no database and no clock: local wall-clock times
are converted to the UTC instants the function receives, exactly as the storage layer would hold them,
and the split is asserted to the minute. The scenarios are the ones named in the brief and the design's
testing strategy: the multi-site worked example, a Shabbat-spanning shift, a holiday date, a shift
crossing midnight, and a day at three sites.

Validates: Requirements 10.2, 10.4, 11.1, 11.2, 16.1, 16.2, 16.3, 16.4, 24.4
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.calculations.hours import (
    ClassificationSettings,
    DayEntry,
    ShabbatWindow,
    classify_day,
)

# The business timezone (assumption A4). Every scenario is written in local wall-clock time and
# converted to the UTC instant storage holds, so the tests read the way the brief states them.
JERUSALEM = ZoneInfo("Asia/Jerusalem")

# The seeded Shabbat window: Friday 16:00 → Saturday 20:00 local (assumption A7, seed defaults).
SHABBAT = ShabbatWindow(
    start_weekday=4, start_time=time(16, 0), end_weekday=5, end_time=time(20, 0)
)


def _settings(*, threshold: int = 480, holidays: frozenset[date] = frozenset()) -> ClassificationSettings:
    return ClassificationSettings(
        timezone=JERUSALEM,
        overtime_threshold_minutes=threshold,
        shabbat_window=SHABBAT,
        holiday_dates=holidays,
    )


def _at(day: date, hh: int, mm: int, *, plus_days: int = 0) -> datetime:
    """The UTC instant of a local Jerusalem wall-clock time, as storage would hold it."""
    from datetime import timedelta

    local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=JERUSALEM) + timedelta(days=plus_days)
    return local.astimezone(ZoneInfo("UTC"))


# A plain mid-week working day, clear of the Shabbat window and of every holiday.
WEDNESDAY = date(2025, 8, 13)


# --------------------------------------------------------------------------- the brief's worked example


def test_the_briefs_two_site_day_splits_8h_regular_and_1h30_overtime_at_site_b():
    """The multi-site worked example (design table; Requirement 11.1, 11.2, 16.2, 16.3, 24.4).

    07:00–11:30 at Site A (270 min) then 12:00–17:00 at Site B (300 min) is 9h30 total. With the
    8-hour threshold the first 480 minutes are regular — all of Site A and the first 210 of Site B —
    and the last 90 minutes, all at Site B, are overtime. The overtime lands where it was worked.
    """
    entries = [
        DayEntry("A", _at(WEDNESDAY, 7, 0), _at(WEDNESDAY, 11, 30)),
        DayEntry("B", _at(WEDNESDAY, 12, 0), _at(WEDNESDAY, 17, 0)),
    ]

    day = classify_day(entries, _settings())

    assert day.total_minutes == 570  # 9h30
    assert day.regular_minutes == 480
    assert day.overtime_minutes == 90
    assert day.shabbat_minutes == 0
    assert day.holiday_minutes == 0

    site_a, site_b = day.entries
    assert (site_a.key, site_a.regular_minutes, site_a.overtime_minutes) == ("A", 270, 0)
    assert (site_b.key, site_b.regular_minutes, site_b.overtime_minutes) == ("B", 210, 90)


# --------------------------------------------------------------------------- Shabbat-spanning shift


def test_a_friday_shift_crossing_16_00_splits_ordinary_before_and_shabbat_after():
    """Requirement 16.4: minutes inside the Shabbat window are premium, not regular or overtime.

    Friday 2025-08-15 14:00–18:00. The window opens at 16:00, so 14:00–16:00 (120 min) is ordinary
    and 16:00–18:00 (120 min) is Shabbat. The Shabbat minutes do not enter the overtime accumulator.
    """
    friday = date(2025, 8, 15)
    assert friday.weekday() == 4

    entries = [DayEntry("A", _at(friday, 14, 0), _at(friday, 18, 0))]

    day = classify_day(entries, _settings())

    assert day.total_minutes == 240
    assert day.regular_minutes == 120
    assert day.shabbat_minutes == 120
    assert day.overtime_minutes == 0


def test_shabbat_minutes_do_not_push_ordinary_minutes_into_overtime():
    """Requirement 16.4: premium minutes are excluded from the threshold accumulator.

    A long Friday: 08:00–20:00, twelve hours. 08:00–16:00 is eight ordinary hours (480 min), exactly
    the threshold, so none of it is overtime; 16:00–20:00 is four Shabbat hours (240 min). Had the
    Shabbat minutes counted toward the threshold, the ordinary block would have been pushed partly
    into overtime — the bug this rule prevents.
    """
    friday = date(2025, 8, 15)
    entries = [DayEntry("A", _at(friday, 8, 0), _at(friday, 20, 0))]

    day = classify_day(entries, _settings())

    assert day.regular_minutes == 480
    assert day.overtime_minutes == 0
    assert day.shabbat_minutes == 240


# --------------------------------------------------------------------------- holiday date


def test_a_shift_on_a_holiday_date_is_entirely_holiday_and_excluded_from_overtime():
    """Requirement 16.4: work on a holiday date is at the holiday rate, not regular or overtime.

    2025-05-01 is Independence Day. A full 09:00–19:00 shift (600 min) on it is all holiday minutes,
    none regular and none overtime, even though ten hours would otherwise breach the eight-hour
    threshold — because premium minutes never enter the accumulator.
    """
    independence_day = date(2025, 5, 1)
    entries = [DayEntry("A", _at(independence_day, 9, 0), _at(independence_day, 19, 0))]

    day = classify_day(entries, _settings(holidays=frozenset({independence_day})))

    assert day.total_minutes == 600
    assert day.holiday_minutes == 600
    assert day.regular_minutes == 0
    assert day.overtime_minutes == 0
    assert day.shabbat_minutes == 0


# --------------------------------------------------------------------------- midnight crossing


def test_a_shift_crossing_midnight_is_classified_across_both_local_dates():
    """Requirement 10.4: a shift crossing midnight is one entry; each minute is classified in place.

    Wednesday 22:00 to Thursday 02:00 is 240 minutes. On plain dates it is all regular, and the
    duration is computed from the UTC instants across the day boundary without loss.
    """
    entries = [DayEntry("A", _at(WEDNESDAY, 22, 0), _at(WEDNESDAY, 2, 0, plus_days=1))]

    day = classify_day(entries, _settings())

    assert day.total_minutes == 240
    assert day.regular_minutes == 240
    assert day.overtime_minutes == 0


def test_a_shift_crossing_into_a_holiday_pays_holiday_only_for_the_minutes_after_midnight():
    """Requirement 10.4 with 16.4: classification follows the wall clock, not the entry's work date.

    A shift starting the evening before Independence Day and running past midnight into it: the
    pre-midnight minutes are ordinary and the post-midnight minutes are holiday, even though the whole
    entry's work date is the day before. 2025-04-30 22:00 → 2025-05-01 02:00 is 120 + 120 minutes.
    """
    eve = date(2025, 4, 30)
    independence_day = date(2025, 5, 1)
    entries = [DayEntry("A", _at(eve, 22, 0), _at(eve, 2, 0, plus_days=1))]

    day = classify_day(entries, _settings(holidays=frozenset({independence_day})))

    assert day.total_minutes == 240
    assert day.regular_minutes == 120
    assert day.holiday_minutes == 120
    assert day.overtime_minutes == 0


# --------------------------------------------------------------------------- three sites in a day


def test_a_day_at_three_sites_allocates_overtime_to_the_last_site_chronologically():
    """Requirement 11.1, 11.2, 16.3: the daily total is the sum of per-site entries, overtime last.

    Three sites on a plain day: 06:00–10:00 at A (240 min), 10:30–15:30 at B (300 min), 16:00–19:00
    at C (180 min) — 720 minutes total. The first 480 ordinary minutes are regular (all of A, and the
    first 240 of B); the remaining 60 of B and all 180 of C are overtime — split across the two later
    sites where they were worked, which is the split a client querying an invoice can be shown.
    """
    entries = [
        DayEntry("A", _at(WEDNESDAY, 6, 0), _at(WEDNESDAY, 10, 0)),
        DayEntry("B", _at(WEDNESDAY, 10, 30), _at(WEDNESDAY, 15, 30)),
        DayEntry("C", _at(WEDNESDAY, 16, 0), _at(WEDNESDAY, 19, 0)),
    ]

    day = classify_day(entries, _settings())

    assert day.total_minutes == 720
    assert day.regular_minutes == 480
    assert day.overtime_minutes == 240

    a, b, c = day.entries
    assert (a.key, a.regular_minutes, a.overtime_minutes) == ("A", 240, 0)
    assert (b.key, b.regular_minutes, b.overtime_minutes) == ("B", 240, 60)
    assert (c.key, c.regular_minutes, c.overtime_minutes) == ("C", 0, 180)


def test_entries_are_sorted_by_check_in_before_allocation():
    """Requirement 16.3: allocation is chronological regardless of the order entries arrive in.

    The same three-site day as above, handed in reverse, must produce the identical split — the
    function sorts by check-in first, so the earliest hours are regular whatever order the caller
    passes.
    """
    entries = [
        DayEntry("C", _at(WEDNESDAY, 15, 0), _at(WEDNESDAY, 19, 0)),
        DayEntry("A", _at(WEDNESDAY, 6, 0), _at(WEDNESDAY, 10, 0)),
        DayEntry("B", _at(WEDNESDAY, 10, 30), _at(WEDNESDAY, 14, 30)),
    ]

    day = classify_day(entries, _settings())

    assert [c.key for c in day.entries] == ["A", "B", "C"]
    assert day.regular_minutes == 480
    assert day.overtime_minutes == 240


# --------------------------------------------------------------------------- invariants and edges


def test_buckets_always_sum_to_the_total_worked_minutes():
    """The four buckets partition the day's minutes: nothing is lost or double-counted.

    A day that touches all four buckets — a long Friday spanning the Shabbat opening after eight
    ordinary hours — is split so the bucket totals equal the summed durations exactly.
    """
    friday = date(2025, 8, 15)
    entries = [
        DayEntry("A", _at(friday, 6, 0), _at(friday, 12, 0)),  # 360 ordinary
        DayEntry("B", _at(friday, 12, 30), _at(friday, 18, 0)),  # to 16:00 ordinary, then Shabbat
    ]

    day = classify_day(entries, _settings())

    summed = sum(e.total_minutes for e in day.entries)
    assert day.total_minutes == summed
    for c in day.entries:
        parts = c.regular_minutes + c.overtime_minutes + c.shabbat_minutes + c.holiday_minutes
        assert parts == c.total_minutes


def test_no_entries_yields_an_empty_classification():
    day = classify_day([], _settings())
    assert day.entries == ()
    assert day.total_minutes == 0


def test_duration_is_whole_minutes_truncated_from_the_timestamps():
    """Requirement 10.2: duration is the count of complete minutes, computed from the UTC instants.

    A 90-second-short shift — 09:00:00 to 10:29:30 — is 89 whole minutes, not 89.5, because a partial
    minute is not a worked minute.
    """
    from datetime import timedelta

    start = _at(WEDNESDAY, 9, 0)
    end = start + timedelta(minutes=89, seconds=30)
    day = classify_day([DayEntry("A", start, end)], _settings())
    assert day.total_minutes == 89


def test_an_entry_ending_at_or_before_its_start_is_rejected():
    """A completed entry must have a strictly positive duration; the caller filters open entries."""
    with pytest.raises(ValueError, match="strictly after"):
        DayEntry("A", _at(WEDNESDAY, 10, 0), _at(WEDNESDAY, 9, 0))


def test_naive_timestamps_are_rejected():
    """Inputs are the UTC instants storage holds; a naive datetime is a caller bug, caught early."""
    with pytest.raises(ValueError, match="timezone-aware"):
        DayEntry("A", datetime(2025, 8, 13, 7, 0), datetime(2025, 8, 13, 8, 0))
