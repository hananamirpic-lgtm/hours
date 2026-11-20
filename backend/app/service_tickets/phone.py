"""Phone number normalization for the service tickets.

Every place that handles a full phone number (customer create and edit, ticket submission, sign-in)
calls `normalize_phone`, so one phone number means one customer however it is typed. The functions
are pure: they read no environment and no database.

Only four characters count as separators: ASCII space, hyphen-minus and the two parentheses. A tab,
a dot, a non-breaking space or any other character is not removed, so it makes the value invalid.
Digits are matched with `[0-9]`, never `\\d` and never `str.isdigit`, which also accept Arabic-Indic
digits and superscripts.
"""

from __future__ import annotations

import re

MAX_PHONE_INPUT_LENGTH = 30

# ASCII space, hyphen-minus, opening and closing parenthesis only.
_SEPARATORS = str.maketrans("", "", " -()")
_INTERNATIONAL = re.compile(r"(?:\+972|972)([0-9]{9})")
_LOCAL = re.compile(r"0[0-9]{9}")


class InvalidPhone(ValueError):
    """The phone number is not a valid Israeli number in any accepted spelling."""


def normalize_phone(raw: str | None) -> str:
    """The Normalized_Phone of `raw`: exactly ten ASCII digits, the first one `0`.

    Spaces, hyphens and parentheses are removed wherever they occur. Then `+972` or `972` followed
    by exactly nine ASCII digits becomes `0` plus those digits. Raises `InvalidPhone` for an absent
    or empty input, for an input longer than 30 characters as received (before any removal), and
    for any value that is not ten ASCII digits starting with `0` after those steps.

    Normalizing an accepted result again returns it unchanged, because it starts with `0`, which
    neither the international prefix nor a separator matches.
    """
    if raw is None or raw == "" or len(raw) > MAX_PHONE_INPUT_LENGTH:
        raise InvalidPhone
    value = raw.translate(_SEPARATORS)
    match = _INTERNATIONAL.fullmatch(value)
    if match:
        value = "0" + match.group(1)
    if not _LOCAL.fullmatch(value):
        raise InvalidPhone
    return value


def strip_search_phone_chars(text: str) -> str:
    """`text` without spaces, hyphens and parentheses, for the customers screen phone search.

    The search text is a fragment, not a full number, so it is never validated and this function
    never raises. Any other character is kept as it is.
    """
    return text.translate(_SEPARATORS)
