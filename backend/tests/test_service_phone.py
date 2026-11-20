"""Example tests for the service-ticket phone normalization.

Requirements 4.1 to 4.4 (grammar, length and rejection), 4.6 (search text), 4.7 (idempotence)
and 4.8 (the seven spellings of one number).
"""

from __future__ import annotations

import pytest

from app.service_tickets.phone import (
    InvalidPhone,
    normalize_phone,
    strip_search_phone_chars,
)

ARABIC_INDIC_NUMBER = "\u0660\u0665\u0660\u0665\u0665\u0665\u0666\u0666\u0667\u0667"

SEVEN_SPELLINGS = [
    "0505556677",
    "050-555-6677",
    "050 555 6677",
    "(050) 555-6677",
    "+972505556677",
    "+972 50-555-6677",
    "972505556677",
]


@pytest.mark.parametrize("raw", SEVEN_SPELLINGS)
def test_seven_spellings_give_one_normalized_phone(raw: str) -> None:
    assert normalize_phone(raw) == "0505556677"


@pytest.mark.parametrize(
    "raw",
    [
        "050-555-667",  # 9 digits
        "05055566778",  # 11 digits
        "+9720505556677",  # international prefix followed by a leading zero
        "9720505556677",
        "1505556677",  # does not start with 0
        "050.555.6677",  # a dot is not a separator
        "050\t555\t6677",  # a tab is not a separator
        "050 555 66a7",  # a letter
        "050\u00a0555\u00a06677",  # non-breaking space is not ASCII space
        "0505556677\n",  # trailing newline
        ARABIC_INDIC_NUMBER,  # Arabic-Indic digits
        "050555667\u00b2",  # superscript two
        "\uff10\uff15\uff10\uff15\uff15\uff15\uff16\uff16\uff17\uff17",  # full-width digits
        "0" * 31,
        "0505556677" + " " * 21,  # 31 characters as received
    ],
)
def test_invalid_values_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidPhone):
        normalize_phone(raw)


@pytest.mark.parametrize("raw", [None, "", " ", "   ", "-", "()", " - ( ) "])
def test_absent_empty_and_separator_only_inputs_are_rejected(raw: str | None) -> None:
    with pytest.raises(InvalidPhone):
        normalize_phone(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "0+505556677",
        "050555667+7",
        "+0505556677",
        "++972505556677",
        "+972+505556677",
        "+ 972505556677 +",
        "+505556677",
    ],
)
def test_plus_is_accepted_only_in_the_international_prefix(raw: str) -> None:
    with pytest.raises(InvalidPhone):
        normalize_phone(raw)


def test_972_followed_by_nine_digits_is_accepted() -> None:
    assert normalize_phone("972505556677") == "0505556677"
    assert normalize_phone("+972505556677") == "0505556677"


@pytest.mark.parametrize("raw", ["97250555667", "9725055566778", "+97250555667", "+9725055566778"])
def test_972_followed_by_eight_or_ten_digits_is_rejected(raw: str) -> None:
    with pytest.raises(InvalidPhone):
        normalize_phone(raw)


def test_length_limit_is_checked_on_the_input_as_received() -> None:
    at_limit = "0505556677" + " " * 20
    over_limit = "0505556677" + " " * 21
    assert len(at_limit) == 30
    assert len(over_limit) == 31
    assert normalize_phone(at_limit) == "0505556677"
    with pytest.raises(InvalidPhone):
        normalize_phone(over_limit)


def test_length_limit_applies_to_the_international_spelling_too() -> None:
    at_limit = "+972" + " " * 17 + "505556677"
    over_limit = "+972" + " " * 18 + "505556677"
    assert len(at_limit) == 30
    assert len(over_limit) == 31
    assert normalize_phone(at_limit) == "0505556677"
    with pytest.raises(InvalidPhone):
        normalize_phone(over_limit)


def test_separators_are_removed_wherever_they_occur() -> None:
    assert normalize_phone(" ( 0 5 0 ) - 5 5 5 - 6 6 7 7 ") == "0505556677"
    assert normalize_phone("(+972) 50 555 6677") == "0505556677"


@pytest.mark.parametrize("raw", SEVEN_SPELLINGS + ["  0505556677  ", "0-5-0-5-5-5-6-6-7-7"])
def test_normalizing_an_accepted_value_again_returns_it_unchanged(raw: str) -> None:
    once = normalize_phone(raw)
    assert normalize_phone(once) == once


def test_invalid_phone_is_a_value_error() -> None:
    assert issubclass(InvalidPhone, ValueError)


def test_strip_search_phone_chars_removes_the_four_characters() -> None:
    assert strip_search_phone_chars("050-555") == "050555"
    assert strip_search_phone_chars("(050) 555-66") == "05055566"
    assert strip_search_phone_chars(" - ( ) ") == ""


@pytest.mark.parametrize(
    "text",
    [
        "",
        "050.555",
        "a\tb\n",
        "\u05d9\u05e9\u05e8\u05d0\u05dc \u05d9\u05e9\u05e8\u05d0\u05dc\u05d9 (3)",
        ARABIC_INDIC_NUMBER,
        "+972 50",
        "x" * 1000,
    ],
)
def test_strip_search_phone_chars_never_raises_and_returns_a_string(text: str) -> None:
    result = strip_search_phone_chars(text)
    assert isinstance(result, str)
    assert not any(ch in result for ch in " -()")


def test_strip_search_phone_chars_keeps_other_characters() -> None:
    assert strip_search_phone_chars("\u05d9\u05e9 (3)-4") == "\u05d9\u05e934"
    assert strip_search_phone_chars("050.555\t6") == "050.555\t6"
