"""Example tests for the service-ticket grammars, parsers and key helpers (R4.6, R5.4, R6.4, R12.3,
R12.9, R15.3, R15.4, R15.6, R18.3 to R18.5)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.service_tickets import grammars as g

JERUSALEM = "Asia/Jerusalem"
ARABIC_INDIC_1234 = "\u0661\u0662\u0663\u0664"
ARABIC_INDIC_10000 = "\u0661\u0660\u0660\u0660\u0660"


# --- customer password ------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0000", "1234", "9999", "0007"])
def test_password_accepts_four_ascii_digits(value: str) -> None:
    assert g.is_valid_customer_password(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "123",
        "12345",
        "+123",
        "-123",
        "1.23",
        "12 4",
        " 123",
        "123 ",
        "1234\n",
        "12a4",
        ARABIC_INDIC_1234,
        "\u00b2\u00b2\u00b2\u00b2",  # superscript two: str.isdigit() is True
        "\uff11\uff12\uff13\uff14",  # fullwidth digits
    ],
)
def test_password_rejects_everything_else(value: str) -> None:
    assert not g.is_valid_customer_password(value)


def test_password_rejects_non_strings() -> None:
    assert not g.is_valid_customer_password(1234)
    assert not g.is_valid_customer_password(None)


# --- page -------------------------------------------------------------------------------------


def test_page_absent_is_one() -> None:
    assert g.parse_page(None) == 1


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("7", 7), ("007", 7), ("1000", 1000)])
def test_page_accepts_positive_integers(raw: str, expected: int) -> None:
    assert g.parse_page(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "0", "00", "-1", "+1", "1.0", "1.", " 1", "1 ", "1 2", "abc", "\u0661", "1e3"]
)
def test_page_rejects_with_invalid_page(raw: str) -> None:
    with pytest.raises(g.InvalidPage) as info:
        g.parse_page(raw)
    assert info.value.code == "invalid_page"


def test_page_far_beyond_any_list_is_clamped_not_rejected() -> None:
    assert g.parse_page("9" * 40) == g.MAX_PAGE
    assert g.parse_page(str(g.MAX_PAGE + 1)) == g.MAX_PAGE


def test_page_sizes() -> None:
    assert (g.STAFF_PAGE_SIZE, g.CUSTOMER_PAGE_SIZE) == (25, 50)


# --- ticket search ----------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [("10000", 10000), ("99999", 99999), ("54321", 54321)])
def test_search_accepts_range_ends(raw: str, expected: int) -> None:
    assert g.parse_ticket_search(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n "])
def test_search_empty_after_trim_means_no_search(raw: str | None) -> None:
    assert g.parse_ticket_search(raw) is None


def test_search_trims_leading_and_trailing_whitespace() -> None:
    assert g.parse_ticket_search("  12345\t") == 12345
    assert g.parse_ticket_search("\n10000 ") == 10000


@pytest.mark.parametrize(
    "raw",
    [
        "9999",
        "100000",
        "09999",
        "00000",
        "1234",
        "123456",
        "+1234",
        "+12345",
        "-12345",
        "12345.0",
        "1234.5",
        "12 345",
        "12\t345",
        "12345a",
        "abcde",
        ARABIC_INDIC_10000,
        "\u00b2\u00b2\u00b2\u00b2\u00b2",
        "1_000",
        "12345\x00",
    ],
)
def test_search_rejects_with_invalid_ticket_number(raw: str) -> None:
    with pytest.raises(g.InvalidTicketNumber) as info:
        g.parse_ticket_search(raw)
    assert info.value.code == "invalid_ticket_number"


# --- date range -------------------------------------------------------------------------------


def test_dates_both_absent() -> None:
    assert g.parse_date_range(None, None) == (None, None)
    assert g.parse_date_range("", "  ") == (None, None)


def test_dates_one_sided() -> None:
    assert g.parse_date_range("2025-03-01", None) == (date(2025, 3, 1), None)
    assert g.parse_date_range(None, "2025-03-31") == (None, date(2025, 3, 31))


def test_dates_leap_day_accepted_in_leap_year() -> None:
    assert g.parse_date_range("2024-02-29", None) == (date(2024, 2, 29), None)


def test_dates_single_day_when_from_equals_to() -> None:
    day = date(2025, 6, 15)
    assert g.parse_date_range("2025-06-15", "2025-06-15") == (day, day)


def test_dates_ordered_range_is_accepted() -> None:
    assert g.parse_date_range("2025-01-01", "2025-12-31") == (date(2025, 1, 1), date(2025, 12, 31))


@pytest.mark.parametrize(
    "raw",
    [
        "2025-02-29",
        "2025-13-01",
        "2025-00-10",
        "2025-01-00",
        "2025-04-31",
        "0000-01-01",
        "25-1-1",
        "2025-1-1",
        "2025/01/01",
        "20250101",
        "2025-01-01T00:00:00",
        " 2025-01-01",
        "2025-01-01\n",
        "\u0662\u0660\u0662\u0665-\u0660\u0661-\u0660\u0661",
        "yesterday",
    ],
)
@pytest.mark.parametrize("position", ["from", "to"])
def test_dates_reject_bad_values(raw: str, position: str) -> None:
    args = (raw, None) if position == "from" else (None, raw)
    with pytest.raises(g.InvalidDateRange) as info:
        g.parse_date_range(*args)
    assert info.value.code == "invalid_date_range"


def test_dates_reject_from_after_to() -> None:
    with pytest.raises(g.InvalidDateRange):
        g.parse_date_range("2025-03-02", "2025-03-01")


# --- status filter ----------------------------------------------------------------------------


def test_status_filter_absent_or_blank_names_nothing() -> None:
    assert g.parse_status_filter(None) == ()
    assert g.parse_status_filter("") == ()
    assert g.parse_status_filter("  ") == ()
    assert g.parse_status_filter([]) == ()


@pytest.mark.parametrize("status", ["received", "in_progress", "closed", "cancelled"])
def test_status_filter_accepts_each_status(status: str) -> None:
    assert g.parse_status_filter(status) == (status,)


def test_status_filter_accepts_several_in_either_form_and_drops_duplicates() -> None:
    assert g.parse_status_filter("received, closed") == ("received", "closed")
    assert g.parse_status_filter(["closed", "received,closed"]) == ("closed", "received")


@pytest.mark.parametrize(
    "raw",
    ["open", "Received", "in progress", "received,open", "received,,closed", ",", "received,"],
)
def test_status_filter_rejects_unknown_with_invalid_filter(raw: str) -> None:
    with pytest.raises(g.InvalidFilter) as info:
        g.parse_status_filter(raw)
    assert info.value.code == "invalid_filter"


def test_status_filter_rejects_unknown_in_a_repeated_parameter() -> None:
    with pytest.raises(g.InvalidFilter):
        g.parse_status_filter(["received", "bogus"])


# --- path parameters --------------------------------------------------------------------------


def test_ticket_number_path_valid() -> None:
    assert g.parse_ticket_number_path("10000") == 10000
    assert g.parse_ticket_number_path("99999") == 99999


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "9999",
        "100000",
        "09999",
        "+12345",
        "-12345",
        "1234.5",
        "12 345",
        " 12345",
        "12345 ",
        "12345\n",
        ARABIC_INDIC_10000,
        "abc",
    ],
)
def test_ticket_number_path_malformed_gives_none_not_an_exception(raw: str) -> None:
    assert g.parse_ticket_number_path(raw) is None


def test_uuid_path_valid_in_either_case() -> None:
    value = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert g.parse_uuid_path("12345678-1234-5678-1234-567812345678") == value
    assert g.parse_uuid_path("ABCDEF00-1234-5678-1234-567812345678") == uuid.UUID(
        "abcdef00-1234-5678-1234-567812345678"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not-a-uuid",
        "12345678123456781234567812345678",
        "{12345678-1234-5678-1234-567812345678}",
        "urn:uuid:12345678-1234-5678-1234-567812345678",
        "12345678-1234-5678-1234-56781234567",
        "12345678-1234-5678-1234-5678123456789",
        "12345678-1234-5678-1234-56781234567g",
        "12345678-1234-5678-1234-567812345678\n",
        "\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668-1234-5678-1234-567812345678",
    ],
)
def test_uuid_path_malformed_gives_none_not_an_exception(raw: str) -> None:
    assert g.parse_uuid_path(raw) is None


# --- fold_key ---------------------------------------------------------------------------------


def test_fold_key_trims_and_casefolds() -> None:
    assert g.fold_key("  Plumbing \t") == "plumbing"


def test_fold_key_sharp_s_folds_to_ss() -> None:
    assert g.fold_key("Stra\u00dfe") == g.fold_key("STRASSE") == "strasse"


def test_fold_key_unifies_precomposed_and_combining_accent() -> None:
    assert g.fold_key("caf\u00e9") == g.fold_key("cafe\u0301")


def test_fold_key_hebrew_combining_marks_are_normalised() -> None:
    # Shin + qamats + shin dot versus shin + shin dot + qamats: canonically equal, different code points.
    shin, qamats, shin_dot = "\u05e9", "\u05b8", "\u05c1"
    one = shin + qamats + shin_dot + "\u05dc\u05d5\u05dd"
    other = shin + shin_dot + qamats + "\u05dc\u05d5\u05dd"
    assert one != other
    assert g.fold_key(one) == g.fold_key(other)
    assert g.fold_key(" " + one + " ") == g.fold_key(other)


def test_fold_key_keeps_distinct_hebrew_words_distinct() -> None:
    assert g.fold_key("\u05e9\u05dc\u05d5\u05dd") != g.fold_key("\u05e9\u05dc\u05d5\u05dd\u05d4")


# --- created_date_for -------------------------------------------------------------------------


def test_created_date_late_evening_is_the_local_day() -> None:
    # 23:30 on 10 January, Israel standard time (UTC+2), is 21:30 UTC the same day.
    instant = datetime(2025, 1, 10, 21, 30, tzinfo=UTC)
    assert g.created_date_for(instant, JERUSALEM) == date(2025, 1, 10)


def test_created_date_after_midnight_is_the_next_day_though_utc_is_not() -> None:
    # 00:30 on 11 January local is 22:30 UTC on 10 January.
    instant = datetime(2025, 1, 10, 22, 30, tzinfo=UTC)
    assert g.created_date_for(instant, JERUSALEM) == date(2025, 1, 11)
    assert instant.date() == date(2025, 1, 10)


def test_created_date_summer_offset_is_three_hours() -> None:
    assert g.created_date_for(datetime(2025, 7, 10, 21, 30, tzinfo=UTC), JERUSALEM) == date(
        2025, 7, 11
    )
    assert g.created_date_for(datetime(2025, 7, 10, 20, 59, tzinfo=UTC), JERUSALEM) == date(
        2025, 7, 10
    )


def test_created_date_spring_forward_2025() -> None:
    # Clocks go from 02:00 to 03:00 on Friday 28 March 2025 (00:00 UTC).
    before = datetime(2025, 3, 27, 21, 59, tzinfo=UTC)
    assert g.created_date_for(before, JERUSALEM) == date(2025, 3, 27)
    at_midnight = datetime(2025, 3, 27, 22, 0, tzinfo=UTC)
    assert g.created_date_for(at_midnight, JERUSALEM) == date(2025, 3, 28)
    late_utc = datetime(2025, 3, 27, 23, 59, tzinfo=UTC)
    assert g.created_date_for(late_utc, JERUSALEM) == date(2025, 3, 28)
    # After the change the offset is +3: 21:00 UTC is already midnight of the next day.
    last_minute = datetime(2025, 3, 28, 20, 59, tzinfo=UTC)
    assert g.created_date_for(last_minute, JERUSALEM) == date(2025, 3, 28)
    next_midnight = datetime(2025, 3, 28, 21, 0, tzinfo=UTC)
    assert g.created_date_for(next_midnight, JERUSALEM) == date(2025, 3, 29)


def test_created_date_autumn_fall_back_2025() -> None:
    # Clocks go from 02:00 back to 01:00 on Sunday 26 October 2025 (23:00 UTC on the 25th).
    before = datetime(2025, 10, 25, 20, 59, tzinfo=UTC)
    assert g.created_date_for(before, JERUSALEM) == date(2025, 10, 25)
    at_midnight = datetime(2025, 10, 25, 21, 0, tzinfo=UTC)
    assert g.created_date_for(at_midnight, JERUSALEM) == date(2025, 10, 26)
    # After the change the offset is +2: 21:30 UTC is 23:30 on the 26th, not 00:30 on the 27th.
    evening = datetime(2025, 10, 26, 21, 30, tzinfo=UTC)
    assert g.created_date_for(evening, JERUSALEM) == date(2025, 10, 26)
    next_midnight = datetime(2025, 10, 26, 22, 0, tzinfo=UTC)
    assert g.created_date_for(next_midnight, JERUSALEM) == date(2025, 10, 27)


def test_created_date_accepts_any_aware_zone_and_returns_a_plain_date() -> None:
    plus_nine = timezone(timedelta(hours=9))
    result = g.created_date_for(datetime(2025, 1, 11, 6, 30, tzinfo=plus_nine), JERUSALEM)
    assert result == date(2025, 1, 10)  # 21:30 UTC
    assert type(result) is date


def test_created_date_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        g.created_date_for(datetime(2025, 1, 10, 12, 0), JERUSALEM)
