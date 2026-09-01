"""Daily hour classification and chronological overtime allocation.

This is the calculation the whole business case rests on: an employee works several sites in one day,
is paid one wage, and each site bills its client at its own rate, so every money figure downstream is
wrong unless the day's minutes are split correctly — per site, and into the four pay buckets. The
function that does it is pure. It takes plain values and returns plain values, reads neither the
database nor the clock, and so can be tested against the worked example in the brief in isolation
(design, "Calculation modules … pure functions over plain values"; Requirement 24.4).

The rules it implements, from the design's "Daily hour classification and chronological allocation"
and Requirement 16.1–16.4:

1. Sort the day's entries by check-in.
2. Split each entry at the Shabbat-window and holiday-date boundaries. Minutes inside the Shabbat
   window or on a holiday date are classified as Shabbat/holiday, paid at the premium rate, and are
   *excluded from the overtime threshold accumulator* — they are already at premium, so counting them
   toward the 8-hour threshold would push ordinary minutes into overtime that were not worked as such
   (Requirement 16.4).
3. Walk the remaining (non-premium) minutes in chronological order, filling the regular bucket up to
   the daily threshold (default 480 minutes = 8 hours); everything after is overtime (Requirement
   16.2, 16.3).
4. Attribute each bucket's minutes back to the entry, and therefore the site, they came from
   (Requirement 11.1, 11.2).

Time. Durations are whole minutes computed from the stored UTC timestamps (Requirement 10.2); the
inputs are timezone-aware UTC datetimes. *Which* minutes are Shabbat or holiday, and which local date
an entry belongs to, is decided after converting to the business timezone (design, "Time"); that zone
and the day's holiday dates are passed in, because a pure function may not look them up itself. A
shift crossing midnight is attributed to the local date of its check-in (Requirement 10.4), which is
the caller's `work_date`; this module classifies each real minute at the premium or ordinary rate it
actually falls in, so the tail of a Friday shift that runs past 16:00 is Shabbat even though the
entry's work date is Friday.

Only *completed* entries are classified. An open entry has no end and no duration, so it cannot be
split; the caller filters those out before calling here (an open shift is not yet payable).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class DayEntry:
    """One completed shift to classify, reduced to the plain values the split needs.

    `key` is whatever the caller uses to attribute minutes back — a time-entry id, a site id, or a
    ``(site_id, entry_id)`` pair. This module never interprets it; it only hands it back on the
    result, so the caller can total by site for cost allocation and billing. `check_in_at` and
    `check_out_at` are timezone-aware UTC datetimes, check-out strictly after check-in.
    """

    key: object
    check_in_at: datetime
    check_out_at: datetime

    def __post_init__(self) -> None:
        if self.check_in_at.tzinfo is None or self.check_out_at.tzinfo is None:
            raise ValueError("check_in_at and check_out_at must be timezone-aware (UTC)")
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be strictly after check_in_at")


@dataclass(frozen=True, slots=True)
class ShabbatWindow:
    """The weekly Shabbat premium window, as local weekday/time boundaries (assumption A7).

    Weekdays are Python's convention, Monday = 0 … Sunday = 6, matching the seeded settings
    (`shabbat_start_weekday` = 4 for Friday, `shabbat_end_weekday` = 5 for Saturday). The window runs
    from `start_time` on `start_weekday` to `end_time` on `end_weekday` in the same week; the default
    is Friday 16:00 → Saturday 20:00 local. It is an approximation of sunset, configurable, and
    replaceable by exact halachic times later without touching this module.
    """

    start_weekday: int
    start_time: time
    end_weekday: int
    end_time: time


@dataclass(frozen=True, slots=True)
class ClassificationSettings:
    """Everything `classify_day` needs that would otherwise be read from the database or the clock.

    Passed in as plain values so the function stays pure. `timezone` is the business zone the day is
    classified in (`Asia/Jerusalem`); `holiday_dates` is the set of local dates that are national
    holidays, taken from the `holidays` table; `shabbat_window` and `overtime_threshold_minutes` come
    from `settings`. The default threshold is 480 minutes (8 hours, Requirement 16.2).
    """

    timezone: tzinfo
    overtime_threshold_minutes: int
    shabbat_window: ShabbatWindow
    holiday_dates: frozenset[date] = frozenset()


# --------------------------------------------------------------------------- outputs


@dataclass(frozen=True, slots=True)
class EntryClassification:
    """The four-bucket split of one entry's minutes, carrying back the caller's `key`.

    The four buckets sum to the entry's total worked minutes. `regular` and `overtime` are the
    ordinary minutes below and above the daily threshold; `shabbat` and `holiday` are the premium
    minutes, which never enter the threshold accumulator.
    """

    key: object
    regular_minutes: int = 0
    overtime_minutes: int = 0
    shabbat_minutes: int = 0
    holiday_minutes: int = 0

    @property
    def total_minutes(self) -> int:
        return self.regular_minutes + self.overtime_minutes + self.shabbat_minutes + self.holiday_minutes


@dataclass(frozen=True, slots=True)
class DayClassification:
    """The classification of a whole day: one `EntryClassification` per entry, plus day totals.

    `entries` preserves the chronological order the split walked, so a caller rendering the day reads
    it the way it happened. The `*_minutes` totals are the sum across entries, convenient for the
    payroll record's day row without re-summing.
    """

    entries: tuple[EntryClassification, ...] = ()
    regular_minutes: int = 0
    overtime_minutes: int = 0
    shabbat_minutes: int = 0
    holiday_minutes: int = 0

    @property
    def total_minutes(self) -> int:
        return self.regular_minutes + self.overtime_minutes + self.shabbat_minutes + self.holiday_minutes


# --------------------------------------------------------------------------- the classification


#: A single minute expressed as a whole-minute index from an arbitrary origin, tagged with whether it
#: is premium (Shabbat/holiday) and which entry it belongs to. The split reduces the day to a stream
#: of these and walks them once; representing time as whole minutes is what keeps the arithmetic
#: integer throughout, never fractional hours (design, "Money").
@dataclass(slots=True)
class _MinuteRun:
    key: object
    minutes: int
    is_premium: bool
    is_holiday: bool


def classify_day(
    entries: Iterable[DayEntry],
    settings: ClassificationSettings,
) -> DayClassification:
    """Split one employee's completed entries for one day into the four pay buckets.

    Pure: no database, no HTTP, no clock. `entries` are the completed shifts for a single employee on
    a single work date; `settings` carries the timezone, the overtime threshold, the Shabbat window
    and the day's holiday dates. Returns a `DayClassification` whose per-entry splits attribute every
    worked minute to the site it came from, with Shabbat and holiday minutes at premium and the
    remaining minutes divided regular-then-overtime in chronological order.
    """
    ordered = sorted(entries, key=lambda e: e.check_in_at)
    if not ordered:
        return DayClassification()

    # Step 2: reduce each entry, in order, to a run of whole minutes tagged premium/ordinary. Each run
    # is a maximal stretch of minutes of one kind within one entry, so a Friday shift that crosses
    # 16:00 becomes an ordinary run followed by a Shabbat run, both keyed to the same entry.
    runs: list[_MinuteRun] = []
    for entry in ordered:
        runs.extend(_split_entry_into_runs(entry, settings))

    # Steps 3 and 4: walk the runs once, in chronological order, accumulating the four buckets per
    # entry. Only ordinary minutes count toward the threshold; premium minutes are booked to their
    # bucket and skipped by the accumulator.
    per_entry: dict[object, dict[str, int]] = {}
    order: list[object] = []
    ordinary_so_far = 0
    threshold = settings.overtime_threshold_minutes

    for run in runs:
        bucket = per_entry.setdefault(
            run.key, {"regular": 0, "overtime": 0, "shabbat": 0, "holiday": 0}
        )
        if run.key not in order:
            order.append(run.key)

        if run.is_premium:
            bucket["holiday" if run.is_holiday else "shabbat"] += run.minutes
            continue

        # Ordinary minutes: fill regular up to the threshold, the remainder is overtime. Split the
        # run itself when the threshold falls inside it, so an entry can hold both regular and
        # overtime minutes — which is exactly the brief's Site B (210 regular + 90 overtime).
        remaining = run.minutes
        if ordinary_so_far < threshold:
            regular_here = min(remaining, threshold - ordinary_so_far)
            bucket["regular"] += regular_here
            ordinary_so_far += regular_here
            remaining -= regular_here
        if remaining:
            bucket["overtime"] += remaining
            ordinary_so_far += remaining

    classifications = tuple(
        EntryClassification(
            key=key,
            regular_minutes=per_entry[key]["regular"],
            overtime_minutes=per_entry[key]["overtime"],
            shabbat_minutes=per_entry[key]["shabbat"],
            holiday_minutes=per_entry[key]["holiday"],
        )
        for key in order
    )
    return DayClassification(
        entries=classifications,
        regular_minutes=sum(c.regular_minutes for c in classifications),
        overtime_minutes=sum(c.overtime_minutes for c in classifications),
        shabbat_minutes=sum(c.shabbat_minutes for c in classifications),
        holiday_minutes=sum(c.holiday_minutes for c in classifications),
    )


def _split_entry_into_runs(
    entry: DayEntry, settings: ClassificationSettings
) -> list[_MinuteRun]:
    """Reduce one entry to consecutive same-kind minute runs, classifying in the business timezone.

    Duration is whole minutes from the UTC timestamps (Requirement 10.2). Each minute is then tested
    for premium status at its own local wall-clock position, so the boundary between ordinary and
    premium minutes falls exactly on the Shabbat window edge or the holiday midnight, wherever inside
    the entry that lands. Adjacent minutes of the same kind are coalesced into one run so the
    accumulator walks a handful of runs, not hundreds of single minutes.
    """
    total_minutes = _whole_minutes(entry.check_in_at, entry.check_out_at)
    local_start = entry.check_in_at.astimezone(settings.timezone)

    runs: list[_MinuteRun] = []
    current_premium: bool | None = None
    current_holiday = False
    current_length = 0

    for offset in range(total_minutes):
        # The local wall-clock instant at the *start* of this minute. Classifying by the minute's
        # start means a minute is premium iff its opening instant is inside the window, which puts the
        # regular/premium boundary on the window edge to the minute.
        local_instant = local_start + timedelta(minutes=offset)
        is_holiday = local_instant.date() in settings.holiday_dates
        is_premium = is_holiday or _in_shabbat_window(local_instant, settings.shabbat_window)

        if current_premium is None:
            current_premium, current_holiday, current_length = is_premium, is_holiday, 1
        elif is_premium == current_premium and is_holiday == current_holiday:
            current_length += 1
        else:
            runs.append(_MinuteRun(entry.key, current_length, current_premium, current_holiday))
            current_premium, current_holiday, current_length = is_premium, is_holiday, 1

    if current_length:
        runs.append(
            _MinuteRun(entry.key, current_length, bool(current_premium), current_holiday)
        )
    return runs


def whole_minutes(start: datetime, end: datetime) -> int:
    """Whole minutes between two UTC instants, computed from the timestamps (Requirement 10.2).

    Truncated, not rounded: a duration is the number of complete minutes worked. Callers store this
    same integer as `total_minutes`, so the buckets a day splits into sum to the stored durations.
    The check-out path (`app.services.scan`) stores `total_minutes` through this same function, so a
    day's per-entry durations and the buckets they split into are computed identically — 4 h 29 min
    is 269 minutes both here and on the entry it is stored on.
    """
    return int((end - start).total_seconds()) // 60


#: Kept for the internal callers below that pre-date the public name; both truncate identically.
_whole_minutes = whole_minutes


def _in_shabbat_window(local_instant: datetime, window: ShabbatWindow) -> bool:
    """Whether a local instant falls inside this week's Shabbat window (assumption A7).

    The window is a weekly interval running from `start_time` on `start_weekday` to `end_time` on
    `end_weekday`. Anchoring both edges to the Friday of the instant's own week and comparing the
    instant against that interval handles the Saturday side without special-casing the day rollover,
    because the end anchor is simply a later weekday in the same week.
    """
    # Monday of the instant's week, so both weekday anchors land in one concrete week.
    monday = (local_instant - timedelta(days=local_instant.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = monday + timedelta(days=window.start_weekday)
    start = start.replace(hour=window.start_time.hour, minute=window.start_time.minute)
    end = monday + timedelta(days=window.end_weekday)
    end = end.replace(hour=window.end_time.hour, minute=window.end_time.minute)
    return start <= local_instant < end


__all__ = [
    "ClassificationSettings",
    "DayClassification",
    "DayEntry",
    "EntryClassification",
    "ShabbatWindow",
    "classify_day",
    "whole_minutes",
]
