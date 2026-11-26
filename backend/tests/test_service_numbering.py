"""Example tests for ticket numbering (Requirements 10.1, 10.3, 10.5, 10.6, 10.7, 10.9).

SQLite has no sequences, so `allocate_ticket_number` gets an injected allocator and
`remaining_numbers` reads a one-row table named like the sequence relation, with the same columns
(`last_value`, `is_called`). The real sequence is covered by
`tests/integration/test_service_numbering_smoke.py`.

SQLSTATE attribute names: psycopg 3, which this project uses, exposes `sqlstate` on its errors;
psycopg 2 exposes `pgcode`. The mapping reads both and a test covers each.
"""

from __future__ import annotations

import psycopg
import psycopg.errors
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from app.service_tickets.error_codes import CODE_TICKET_NUMBERS_EXHAUSTED
from app.service_tickets.models import Ticket
from app.service_tickets.services.numbering import (
    LOW_NUMBER_WARNING_THRESHOLD,
    TicketNumberOutOfRange,
    TicketNumbersExhausted,
    allocate_ticket_number,
    remaining_numbers,
)

STATEMENT = "SELECT nextval('svc_ticket_number_seq')"


def _wrap(original: BaseException) -> DBAPIError:
    """Wrap a driver error the way SQLAlchemy does for a failed statement.

    A real psycopg error goes through `DBAPIError.instance`, which picks the subclass from the driver's
    error class. A stand-in that is not a driver error (psycopg 2 style, or no SQLSTATE) is wrapped
    directly, because `instance` would turn it into a `StatementError`.
    """
    if isinstance(original, psycopg.Error):
        return DBAPIError.instance(STATEMENT, None, original, psycopg.Error)  # type: ignore[arg-type]
    return DBAPIError(STATEMENT, None, original)  # type: ignore[arg-type]


class Psycopg2Style(Exception):
    """A driver error that carries the SQLSTATE as `pgcode`, as psycopg 2 does."""

    pgcode = "2200H"


def _raising(error: BaseException):
    def allocator(_session: Session) -> int:
        raise error

    return allocator


def _counting(first: int = 10000):
    numbers = iter(range(first, 100000))

    def allocator(_session: Session) -> int:
        return next(numbers)

    return allocator


# --------------------------------------------------------------------------------- exhaustion


def test_real_psycopg_error_exposes_the_sqlstate_as_sqlstate_not_pgcode() -> None:
    original = psycopg.errors.SequenceGeneratorLimitExceeded("reached maximum value of sequence")
    assert original.sqlstate == "2200H"
    assert not hasattr(original, "pgcode")


def test_exhaustion_maps_to_ticket_numbers_exhausted_and_creates_nothing(session: Session) -> None:
    original = psycopg.errors.SequenceGeneratorLimitExceeded("reached maximum value of sequence")
    error = _wrap(original)
    assert error.orig is original

    with pytest.raises(TicketNumbersExhausted) as raised:
        allocate_ticket_number(session, _raising(error))

    assert raised.value.code == CODE_TICKET_NUMBERS_EXHAUSTED == "ticket_numbers_exhausted"
    assert raised.value.__cause__ is error
    assert not session.new and not session.dirty
    assert session.execute(select(func.count()).select_from(Ticket)).scalar_one() == 0


def test_exhaustion_is_recognised_by_the_pgcode_attribute_too(session: Session) -> None:
    error = _wrap(Psycopg2Style("sequence limit"))
    with pytest.raises(TicketNumbersExhausted):
        allocate_ticket_number(session, _raising(error))


# --------------------------------------------------------------------------------- other errors


@pytest.mark.parametrize(
    "original",
    [
        psycopg.errors.UniqueViolation("duplicate key"),  # SQLSTATE 23505
        psycopg.errors.InsufficientPrivilege("permission denied for sequence"),  # 42501
        psycopg.errors.NumericValueOutOfRange("out of range"),  # 22003, near 2200H but not equal
        psycopg.OperationalError("connection lost"),  # class-level sqlstate is None
        Exception("a driver error without any SQLSTATE"),
    ],
)
def test_other_database_errors_propagate_unchanged(session: Session, original: BaseException) -> None:
    error = _wrap(original)
    with pytest.raises(DBAPIError) as raised:
        allocate_ticket_number(session, _raising(error))
    assert raised.value is error


def test_an_error_that_is_not_a_database_error_propagates_unchanged(session: Session) -> None:
    error = RuntimeError("boom")
    with pytest.raises(RuntimeError) as raised:
        allocate_ticket_number(session, _raising(error))
    assert raised.value is error


def test_operational_error_with_the_exhaustion_state_is_still_exhaustion(session: Session) -> None:
    """The mapping is by SQLSTATE, not by which SQLAlchemy subclass the driver error became."""
    error = OperationalError(STATEMENT, None, psycopg.errors.SequenceGeneratorLimitExceeded("x"))
    with pytest.raises(TicketNumbersExhausted):
        allocate_ticket_number(session, _raising(error))


# --------------------------------------------------------------------------------- allocation order


def test_numbers_follow_the_allocator_order(session: Session) -> None:
    allocator = _counting()
    taken = [allocate_ticket_number(session, allocator) for _ in range(5)]
    assert taken == [10000, 10001, 10002, 10003, 10004]
    assert taken == sorted(taken) and len(set(taken)) == len(taken)


@pytest.mark.parametrize("number", [10000, 54321, 99999])
def test_numbers_inside_the_range_are_returned(session: Session, number: int) -> None:
    assert allocate_ticket_number(session, lambda _s: number) == number


@pytest.mark.parametrize("number", [0, 9999, 100000, -10000, True, "10000", 10000.0, None])
def test_values_outside_the_range_raise_a_clear_error(session: Session, number: object) -> None:
    with pytest.raises(TicketNumberOutOfRange) as raised:
        allocate_ticket_number(session, lambda _s: number)  # type: ignore[arg-type, return-value]
    assert isinstance(raised.value, ValueError)
    assert "ticket number" in str(raised.value)


# --------------------------------------------------------------------------------- remaining


def _set_sequence(session: Session, last_value: int, is_called: bool) -> None:
    session.execute(text("DROP TABLE IF EXISTS svc_ticket_number_seq"))
    session.execute(text("CREATE TABLE svc_ticket_number_seq (last_value INTEGER, is_called BOOLEAN)"))
    session.execute(
        text("INSERT INTO svc_ticket_number_seq VALUES (:last_value, :is_called)"),
        {"last_value": last_value, "is_called": is_called},
    )


@pytest.mark.parametrize(
    ("last_value", "is_called", "expected"),
    [
        (10000, False, 90000),  # unused sequence: 99999 - 9999
        (10000, True, 89999),
        (10001, True, 89998),
        (99998, True, 1),
        (99999, True, 0),
    ],
)
def test_remaining_numbers_at_the_boundaries(
    session: Session, last_value: int, is_called: bool, expected: int
) -> None:
    _set_sequence(session, last_value, is_called)
    assert remaining_numbers(session) == expected


def test_remaining_numbers_does_not_consume_a_number(session: Session) -> None:
    _set_sequence(session, 10000, True)
    assert [remaining_numbers(session) for _ in range(3)] == [89999] * 3
    row = session.execute(text("SELECT last_value, is_called FROM svc_ticket_number_seq")).one()
    assert (row[0], bool(row[1])) == (10000, True)


def test_the_low_number_warning_threshold_is_one_thousand() -> None:
    assert LOW_NUMBER_WARNING_THRESHOLD == 1000