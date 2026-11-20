"""Pure grammars, query parsers and key helpers of the service-ticket system.

Every function here is pure: no database, no clock, no environment. The Ticket_API declares its list
and report query parameters and its path parameters as raw strings and hands them to these functions,
so every failure carries one of the package's own error codes instead of FastAPI's generic 422.

All digit grammars are ASCII only. `str.isdigit` and `int()` accept Arabic-Indic digits and other
Unicode digits, so they are never used to decide whether a value is well formed. Patterns are applied
with `fullmatch`, because `$` would also accept a trailing newline.

Two kinds of failure are handled differently on purpose:

* Query parameters (page, ticket search, dates, status filter) raise a `GrammarError` subclass whose
  `code` attribute is the error code the router answers with (HTTP 422).
* Path parameters (ticket number, customer id) never raise. A malformed value returns `None`, and the
  caller treats `None` exactly like a record that does not exist, so a malformed path and a missing
  record give the same 404 outcome (R12.3, R3.14).
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable
from datetime import date, datetime
from zoneinfo import ZoneInfo

# Error codes used by the parsers. They are defined here, not imported, so this module has no
# dependency on the rest of the package.
CODE_INVALID_PAGE = "invalid_page"
CODE_INVALID_TICKET_NUMBER = "invalid_ticket_number"
CODE_INVALID_DATE_RANGE = "invalid_date_range"
CODE_INVALID_FILTER = "invalid_filter"

# Page sizes are fixed by the API contract. `parse_page` takes the page number only.
STAFF_PAGE_SIZE = 25
CUSTOMER_PAGE_SIZE = 50

# A page number above this is clamped to it. Any page that large is past the last page, so the answer
# is the same empty list, and the row offset (page - 1) * page size stays inside a PostgreSQL bigint.
MAX_PAGE = 1_000_000_000

TICKET_NUMBER_MIN = 10000
TICKET_NUMBER_MAX = 99999

# The four ticket statuses, in lifecycle order.
STATUS_VALUES = ("received", "in_progress", "closed", "cancelled")

_PASSWORD = re.compile(r"[0-9]{4}")
_DIGITS = re.compile(r"[0-9]+")
_FIVE_DIGITS = re.compile(r"[0-9]{5}")
_ISO_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class GrammarError(ValueError):
    """A query parameter that does not match its grammar. `code` is the API error code."""

    code: str = ""

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class InvalidPage(GrammarError):
    code = CODE_INVALID_PAGE


class InvalidTicketNumber(GrammarError):
    code = CODE_INVALID_TICKET_NUMBER


class InvalidDateRange(GrammarError):
    code = CODE_INVALID_DATE_RANGE


class InvalidFilter(GrammarError):
    code = CODE_INVALID_FILTER


def is_valid_customer_password(value: object) -> bool:
    """True only for exactly four ASCII digits `0`-`9`.

    Any other type, any other length, signs, spaces, newlines and non-ASCII digits are rejected.
    """
    return isinstance(value, str) and _PASSWORD.fullmatch(value) is not None


def parse_page(raw: str | None) -> int:
    """The 1-based page number from a raw query value.

    An absent value (`None`) is page 1. Otherwise the value must be ASCII digits only and at least 1:
    an empty string, a sign, a decimal point, surrounding whitespace, `0` and non-ASCII digits raise
    `InvalidPage`. A page above the last page is valid (the caller answers with an empty list); values
    above `MAX_PAGE` are clamped to it.
    """
    if raw is None:
        return 1
    if _DIGITS.fullmatch(raw) is None:
        raise InvalidPage
    digits = raw.lstrip("0")
    if not digits:  # "0", "00": page numbers start at 1
        raise InvalidPage
    if len(digits) > len(str(MAX_PAGE)):
        return MAX_PAGE
    return min(int(digits), MAX_PAGE)


def parse_ticket_search(raw: str | None) -> int | None:
    """The ticket number named by a staff search box, or `None` when there is no search.

    Whitespace around the value is trimmed. Empty after trimming (or `None`) means no search. Anything
    else must be exactly five ASCII digits with a value from 10000 to 99999, otherwise
    `InvalidTicketNumber` is raised. Signs, decimal points, inner whitespace and non-ASCII digits fail.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if _FIVE_DIGITS.fullmatch(text) is None:
        raise InvalidTicketNumber
    number = int(text)
    if number < TICKET_NUMBER_MIN:  # 00000 to 09999: five digits, but not a ticket number
        raise InvalidTicketNumber
    return number


def _parse_iso_date(raw: str | None) -> date | None:
    if raw is None or not raw.strip():
        return None
    match = _ISO_DATE.fullmatch(raw)
    if match is None:
        raise InvalidDateRange
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:  # not a calendar date: month 13, day 0, 2025-02-29, year 0000
        raise InvalidDateRange from None


def parse_date_range(
    from_raw: str | None, to_raw: str | None
) -> tuple[date | None, date | None]:
    """An optional inclusive date range from two raw query values.

    Each value is `None`, blank (treated as absent), or an ISO `YYYY-MM-DD` calendar date with no
    surrounding whitespace. A leap day is valid only in a leap year. When both are present, `from`
    must not be after `to`; equal dates select that single day. Any violation raises
    `InvalidDateRange`.
    """
    start = _parse_iso_date(from_raw)
    end = _parse_iso_date(to_raw)
    if start is not None and end is not None and start > end:
        raise InvalidDateRange
    return start, end


def parse_status_filter(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    """The ticket statuses named by a filter, as a tuple without duplicates in first-seen order.

    `raw` is `None`, one string of comma-separated statuses, or an iterable of such strings (a
    repeated query parameter). `None` and a blank string name no status, so the filter is not applied
    and the result is empty. A name is matched exactly after trimming whitespace; an unknown, empty
    or wrongly cased name raises `InvalidFilter`.
    """
    if raw is None:
        return ()
    parts = [raw] if isinstance(raw, str) else list(raw)
    if all(isinstance(part, str) and not part.strip() for part in parts):
        return ()
    found: list[str] = []
    for part in parts:
        if not isinstance(part, str):
            raise InvalidFilter
        for name in part.split(","):
            status = name.strip()
            if status not in STATUS_VALUES:
                raise InvalidFilter
            if status not in found:
                found.append(status)
    return tuple(found)


def parse_ticket_number_path(raw: str) -> int | None:
    """The ticket number in a path segment, or `None` when it is not one.

    A ticket number is exactly five ASCII digits with a value from 10000 to 99999. This function
    never raises: a malformed value returns `None`, and the caller must answer it with the same 404 as
    a ticket that does not exist, so the two cases cannot be told apart (R12.3).
    """
    if not isinstance(raw, str) or _FIVE_DIGITS.fullmatch(raw) is None:
        return None
    number = int(raw)
    return number if number >= TICKET_NUMBER_MIN else None


def parse_uuid_path(raw: str) -> uuid.UUID | None:
    """The UUID in a path segment, or `None` when it is not one.

    Only the canonical hyphenated form (8-4-4-4-12 hexadecimal digits, either case) is accepted;
    `uuid.UUID` alone would also take braces, a `urn:uuid:` prefix and hyphen-less text. This function
    never raises: a malformed value returns `None`, and the caller must answer it with the same 404 as
    a record that does not exist (R3.14).
    """
    if not isinstance(raw, str) or _UUID.fullmatch(raw) is None:
        return None
    return uuid.UUID(raw)


def fold_key(value: str) -> str:
    """The comparison key for uniqueness checks: NFC, then trimmed, then case-folded.

    Normalising first makes a precomposed letter and its base-plus-combining-mark spelling equal;
    `casefold` (not `lower`) folds `ß` to `ss` and other full case foldings.
    """
    return unicodedata.normalize("NFC", value).strip().casefold()


def created_date_for(instant: datetime, tz_name: str) -> date:
    """The calendar date of `instant` in the zone `tz_name` (an IANA name such as `Asia/Jerusalem`).

    `instant` must be timezone-aware; a naive datetime raises `ValueError`, because it has no instant
    to convert. The tz database decides daylight-saving days, so the result can differ from the UTC
    date by one day in either direction.
    """
    if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
        raise ValueError("instant must be timezone-aware")
    return instant.astimezone(ZoneInfo(tz_name)).date()
