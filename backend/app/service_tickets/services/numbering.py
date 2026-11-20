"""Ticket numbers: allocation from the PostgreSQL sequence and the remaining count.

Requirements 10.1 and 10.3 (five digits, rising in allocation order), 10.5 and 10.6 (a number is
never handed out twice, a failed transaction burns its number), 10.7 (no wrap-around, a clear failure
when the space is used up) and 10.9 (the remaining count, so a caller can warn before the end).

`svc_ticket_number_seq` is created by migration 0011 as
`START 10000 MINVALUE 10000 MAXVALUE 99999 NO CYCLE CACHE 1`. `nextval` is not rolled back with the
transaction, which is what makes a number unique even when the ticket insert later fails. `NO CYCLE`
makes wrap-around impossible: past 99999 PostgreSQL raises SQLSTATE `2200H`
(`sequence_generator_limit_exceeded`), which `allocate_ticket_number` maps to `TicketNumbersExhausted`.

The driver exposes the SQLSTATE as `sqlstate` in psycopg 3 (the driver this project uses) and as
`pgcode` in psycopg 2. `sqlalchemy.exc.DBAPIError.orig` is the driver's own exception, so both names are
read. Any other database error propagates unchanged.

This module never commits and never raises HTTP errors. After `TicketNumbersExhausted` or any other
database error from the sequence, the transaction is in a failed state; the router owns the rollback,
answers 503 `ticket_numbers_exhausted` and stores nothing.

SQLite has no sequences. Unit tests pass an `allocator` (a callable taking the session and returning an
int) in place of `nextval`, and create a one-row table named `svc_ticket_number_seq` with the columns
`last_value` and `is_called` so `remaining_numbers` runs unchanged.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.service_tickets.error_codes import CODE_TICKET_NUMBERS_EXHAUSTED
from app.service_tickets.models import TICKET_NUMBER_MAX, TICKET_NUMBER_MIN

SEQUENCE_NAME = "svc_ticket_number_seq"

#: SQLSTATE of "reached the maximum value of sequence" (`sequence_generator_limit_exceeded`).
SEQUENCE_LIMIT_SQLSTATE = "2200H"

#: A caller that wants to warn staff does so when fewer numbers than this remain (Requirement 10.9).
#: The threshold is a caller decision; this module only provides the number and the constant.
LOW_NUMBER_WARNING_THRESHOLD = 1000

Allocator = Callable[[Session], int]


class TicketNumbersExhausted(Exception):
    """The sequence has no number left. `code` is the API error code (HTTP 503 at the router)."""

    code = CODE_TICKET_NUMBERS_EXHAUSTED


class TicketNumberOutOfRange(ValueError):
    """An allocator returned a value outside 10000 to 99999. This is a programming or setup error."""


def _nextval(session: Session) -> int:
    return int(session.execute(text(f"SELECT nextval('{SEQUENCE_NAME}')")).scalar_one())


def _sqlstate(error: DBAPIError) -> str | None:
    """The SQLSTATE of the driver error under `error`: `sqlstate` (psycopg 3) or `pgcode` (psycopg 2)."""
    original = error.orig
    for name in ("sqlstate", "pgcode"):
        value = getattr(original, name, None)
        if isinstance(value, str) and value:
            return value
    return None


def allocate_ticket_number(session: Session, allocator: Allocator | None = None) -> int:
    """Take the next ticket number. Raises `TicketNumbersExhausted` when the sequence is used up.

    `allocator` replaces `SELECT nextval('svc_ticket_number_seq')` for tests on a database without
    sequences. Whatever produced the number, it must lie in 10000 to 99999 (Requirement 10.1).
    """
    take = allocator if allocator is not None else _nextval
    try:
        number = take(session)
    except DBAPIError as error:
        if _sqlstate(error) == SEQUENCE_LIMIT_SQLSTATE:
            raise TicketNumbersExhausted from error
        raise
    if isinstance(number, bool) or not isinstance(number, int):
        raise TicketNumberOutOfRange(f"ticket number allocator returned {number!r}, not an integer")
    if not TICKET_NUMBER_MIN <= number <= TICKET_NUMBER_MAX:
        raise TicketNumberOutOfRange(
            f"ticket number {number} is outside {TICKET_NUMBER_MIN} to {TICKET_NUMBER_MAX}"
        )
    return number


def remaining_numbers(session: Session) -> int:
    """How many numbers `allocate_ticket_number` can still hand out, without consuming one.

    Reads `last_value` and `is_called` from the sequence relation. Once the sequence has been called,
    `last_value` is the last number handed out, so 99999 minus it remain. Before the first call the
    next number is `last_value` itself, so the count is 99999 minus (`last_value` minus 1), which is
    99999 - 9999 = 90000 for a new sequence.
    """
    row = session.execute(text(f"SELECT last_value, is_called FROM {SEQUENCE_NAME}")).one()
    last_value, is_called = int(row[0]), bool(row[1])
    last_taken = last_value if is_called else last_value - 1
    return max(TICKET_NUMBER_MAX - last_taken, 0)