"""The Israeli holiday calendar, computed from the Hebrew calendar rather than tabulated.

Israeli public holidays follow the Hebrew calendar, so their Gregorian dates move every year and
line up again only every nineteen years. A hand-typed table of Gregorian dates would therefore
cover a handful of years, be wrong the moment it fell out of date, and give a reader no way to tell
a transcription slip from a genuine calendar fact. Computing the dates from the Hebrew calendar
instead means the seed produces the correct dates for *any* year, including years nobody has looked
up yet, which is what "the current and next year" needs to keep meaning as time passes.

The arithmetic is the fixed (rule-based) Hebrew calendar — the same one every Jewish calendar app
uses for civil purposes — implemented from the standard algorithm in Dershowitz and Reingold's
*Calendrical Calculations*. It is exact for the civil calendar; it is not the astronomical sunset
calculation, which the requirements explicitly defer (assumption A7). A test pins the output against
the published 2025 national-holiday dates so a regression in the arithmetic is caught immediately.

Only the days that are *national holidays* in Israel are produced here — the days on which work
stops and which therefore carry the premium pay rate. The many observances that are not days off
(most of Hanukkah, the minor fasts, the intermediate festival days) are deliberately excluded: a
holiday row in this system means "this date is paid at the holiday rate", and applying that to a
working day would be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# --------------------------------------------------------------------------- Hebrew calendar core
#
# The fixed Hebrew calendar is arithmetic: every date maps to a "fixed day" (a running day count,
# Rata Die, with day 1 being 1 January of the proleptic Gregorian year 1), and back. Once both
# directions exist, a holiday is "the Nth day of Hebrew month M in Hebrew year Y", converted to the
# Gregorian date that shares its fixed day.

# Hebrew month numbers. Nisan is month 1 in the Torah's reckoning, but the civil year and the
# arithmetic below number months from Tishrei; these constants name the months the holidays fall in
# under that scheme, where Nisan .. Adar are 1 .. 12/13 counting from Nisan.
NISAN = 1
IYYAR = 2
SIVAN = 3
TAMMUZ = 4
AV = 5
ELUL = 6
TISHREI = 7
HESHVAN = 8
KISLEV = 9
TEVET = 10
SHEVAT = 11
ADAR = 12
ADAR_II = 13

_HEBREW_EPOCH = -1373427  # fixed day of 1 Tishrei, Hebrew year 1.


def _is_hebrew_leap_year(year: int) -> bool:
    """Whether a Hebrew year has a thirteenth month (Adar II).

    Seven years in every nineteen are leap years, in the Metonic cycle positions below.
    """
    return (7 * year + 1) % 19 < 7


def _last_month_of_hebrew_year(year: int) -> int:
    return ADAR_II if _is_hebrew_leap_year(year) else ADAR


def _hebrew_calendar_elapsed_days(year: int) -> int:
    """Days from the Hebrew epoch to the start of `year`, before the four postponement rules."""
    months_elapsed = (235 * year - 234) // 19  # whole months in all prior years
    parts_elapsed = 12084 + 13753 * months_elapsed
    day = 29 * months_elapsed + parts_elapsed // 25920
    # If the remainder puts the molad in the afternoon, Rosh Hashanah is pushed a day (rule "molad
    # zaken" folded into the parity test below).
    return day + 1 if (3 * (day + 1)) % 7 < 3 else day


def _hebrew_new_year_delay(year: int) -> int:
    """The dehiyyot (postponement) correction applied so Rosh Hashanah avoids forbidden weekdays."""
    ny0 = _hebrew_calendar_elapsed_days(year - 1)
    ny1 = _hebrew_calendar_elapsed_days(year)
    ny2 = _hebrew_calendar_elapsed_days(year + 1)
    if ny2 - ny1 == 356:  # this year would be too long
        return 2
    if ny1 - ny0 == 382:  # last year would be too long
        return 1
    return 0


def _hebrew_new_year(year: int) -> int:
    """Fixed day of 1 Tishrei (Rosh Hashanah) for a Hebrew year."""
    return _HEBREW_EPOCH + _hebrew_calendar_elapsed_days(year) + _hebrew_new_year_delay(year)


def _days_in_hebrew_year(year: int) -> int:
    return _hebrew_new_year(year + 1) - _hebrew_new_year(year)


def _hebrew_month_length(year: int, month: int) -> int:
    """Length of a Hebrew month, which depends on whether the year is short, regular or complete."""
    if month in (IYYAR, TAMMUZ, ELUL, TEVET, ADAR_II):
        return 29
    if month == ADAR and not _is_hebrew_leap_year(year):
        return 29
    if month == HESHVAN and _days_in_hebrew_year(year) % 10 != 5:  # not a "complete" year
        return 29
    if month == KISLEV and _days_in_hebrew_year(year) % 10 == 3:  # a "deficient" year
        return 29
    return 30


def hebrew_to_fixed(year: int, month: int, day: int) -> int:
    """Fixed day of a Hebrew date. Months run Tishrei .. Elul, wrapping the year as they do so."""
    if month < TISHREI:  # Nisan .. (Adar/Adar II): later in the same civil year, so after Tishrei
        span = sum(
            _hebrew_month_length(year, m) for m in range(TISHREI, _last_month_of_hebrew_year(year) + 1)
        )
        span += sum(_hebrew_month_length(year, m) for m in range(NISAN, month))
    else:  # Tishrei .. Elul: from the new year forward
        span = sum(_hebrew_month_length(year, m) for m in range(TISHREI, month))
    return _hebrew_new_year(year) + span + day - 1


# --------------------------------------------------------------------------- Gregorian core

_GREGORIAN_EPOCH = 1  # fixed day of 1 January, year 1.


def _gregorian_is_leap_year(year: int) -> bool:
    return year % 4 == 0 and year % 100 != 0 or year % 400 == 0


def fixed_to_gregorian(fixed: int) -> date:
    """Gregorian date sharing a fixed day. The inverse is Python's own `date.toordinal`."""
    # `date.fromordinal` uses exactly this fixed-day convention (1 == 0001-01-01), so the conversion
    # is a single call once a Hebrew date has been reduced to its fixed day. Kept behind this name so
    # the holiday code reads in calendar terms rather than in ordinals.
    return date.fromordinal(fixed)


def gregorian_year_from_fixed(fixed: int) -> int:
    return date.fromordinal(fixed).year


# --------------------------------------------------------------------------- holiday definitions


@dataclass(frozen=True, slots=True)
class Holiday:
    """One national holiday on a specific Gregorian date, named in both languages."""

    date: date
    name_he: str
    name_en: str
    is_full_day: bool = True


@dataclass(frozen=True, slots=True)
class _HolidaySpec:
    """A holiday as a Hebrew-calendar rule, resolved to a Gregorian date per year."""

    name_he: str
    name_en: str
    month: int
    day: int


# The days work stops in Israel and pay is at the holiday rate. Rosh Hashanah and Sukkot's second
# festival day (Shemini Atzeret) fall in the Hebrew year that *starts* in the given Gregorian year;
# Passover, Independence Day and Shavuot fall in the Hebrew year that started the previous autumn.
# `_SPRING_SUMMER` and `_AUTUMN` capture that split so each is looked up in the right Hebrew year.
_AUTUMN: tuple[_HolidaySpec, ...] = (
    _HolidaySpec("ראש השנה", "Rosh Hashanah (Day 1)", TISHREI, 1),
    _HolidaySpec("ראש השנה ב׳", "Rosh Hashanah (Day 2)", TISHREI, 2),
    _HolidaySpec("יום כיפור", "Yom Kippur", TISHREI, 10),
    _HolidaySpec("סוכות", "Sukkot (Day 1)", TISHREI, 15),
    _HolidaySpec("שמיני עצרת/שמחת תורה", "Shemini Atzeret / Simchat Torah", TISHREI, 22),
)
_SPRING_SUMMER: tuple[_HolidaySpec, ...] = (
    _HolidaySpec("פסח", "Passover (Day 1)", NISAN, 15),
    _HolidaySpec("שביעי של פסח", "Passover (Day 7)", NISAN, 21),
    _HolidaySpec("יום העצמאות", "Independence Day", IYYAR, 5),
    _HolidaySpec("שבועות", "Shavuot", SIVAN, 6),
)


def israeli_holidays_for_gregorian_year(gregorian_year: int) -> list[Holiday]:
    """Every Israeli national holiday whose Gregorian date falls in `gregorian_year`.

    The Hebrew year that opens in the autumn of `gregorian_year` is `gregorian_year + 3761`; its
    spring and summer festivals land in `gregorian_year + 1`. So the autumn festivals of this
    Gregorian year come from the Hebrew year opening now, and the spring/summer ones come from the
    Hebrew year that opened last autumn. Resolving each spec and then filtering by Gregorian year
    keeps the boundary correct without special-casing it, and sorts the result into date order.
    """
    hebrew_year_opening_this_autumn = gregorian_year + 3761
    holidays: list[Holiday] = []

    # Autumn festivals land in the Gregorian year the Hebrew year opens in.
    for spec in _AUTUMN:
        holidays.append(_resolve(spec, hebrew_year_opening_this_autumn))

    # Spring and summer festivals of `gregorian_year` belong to the Hebrew year that opened the
    # *previous* autumn, so they are resolved in `hebrew_year - 1`. Resolving both Hebrew years and
    # then filtering by Gregorian year below keeps the boundary right without arithmetic on months.
    for spec in _SPRING_SUMMER:
        holidays.append(_resolve(spec, hebrew_year_opening_this_autumn - 1))

    # Independence Day is moved off Shabbat to protect its observance; the others are not adjusted.
    # Handled after resolution because the rule is about the weekday it lands on.
    holidays = [_adjust_independence_day(h) for h in holidays]

    kept = [h for h in holidays if h.date.year == gregorian_year]
    kept.sort(key=lambda h: h.date)
    return kept


def _resolve(spec: _HolidaySpec, hebrew_year: int) -> Holiday:
    fixed = hebrew_to_fixed(hebrew_year, spec.month, spec.day)
    return Holiday(
        date=fixed_to_gregorian(fixed),
        name_he=spec.name_he,
        name_en=spec.name_en,
        is_full_day=True,
    )


def _adjust_independence_day(holiday: Holiday) -> Holiday:
    """Move Independence Day so it neither falls on nor abuts Shabbat.

    The Knesset fixes Yom Ha'atzmaut on 5 Iyyar but shifts it to avoid desecrating Shabbat: if it
    would fall on Friday or Saturday it is brought earlier to Thursday, and since 2004 if it would
    fall on Monday it is pushed to Tuesday so the preceding memorial day does not touch Shabbat.
    Only Independence Day is adjusted; the other festivals are observed on their calendar date.
    """
    if holiday.name_en != "Independence Day":
        return holiday
    weekday = holiday.date.weekday()  # Monday == 0 .. Sunday == 6
    if weekday == 4:  # Friday → Thursday
        shifted = holiday.date.toordinal() - 1
    elif weekday == 5:  # Saturday → Thursday
        shifted = holiday.date.toordinal() - 2
    elif weekday == 0:  # Monday → Tuesday
        shifted = holiday.date.toordinal() + 1
    else:
        return holiday
    return Holiday(
        date=fixed_to_gregorian(shifted),
        name_he=holiday.name_he,
        name_en=holiday.name_en,
        is_full_day=holiday.is_full_day,
    )
