"""The real `svc_ticket_number_seq` on PostgreSQL (Requirements 10.1, 10.3, 10.5, 10.7, 10.9).

Migration 0011 creates the sequence with `START 10000 MINVALUE 10000 MAXVALUE 99999 NO CYCLE
CACHE 1`. This test upgrades a scratch database to head and checks, against the real sequence, that
numbers start at 10000 and rise by one, that `remaining_numbers` follows without consuming a number,
and that nothing wraps around once 99999 has been handed out. Skips without `TEST_DATABASE_URL`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alembic import command
from alembic.config import Config
from app.service_tickets.services.numbering import (
    TicketNumbersExhausted,
    allocate_ticket_number,
    remaining_numbers,
)


@pytest.fixture
def scratch_engine(
    empty_database: str, make_alembic_config: Callable[[str], Config]
) -> Iterator[Engine]:
    command.upgrade(make_alembic_config(empty_database), "head")
    engine = sa.create_engine(empty_database, poolclass=sa.pool.NullPool)
    try:
        yield engine
    finally:
        engine.dispose()


def test_real_sequence_allocates_in_order_and_never_wraps(scratch_engine: Engine) -> None:
    with Session(scratch_engine) as session:
        assert remaining_numbers(session) == 90000
        session.rollback()

        assert allocate_ticket_number(session) == 10000
        assert allocate_ticket_number(session) == 10001
        assert remaining_numbers(session) == 99999 - 10001
        session.commit()

        # A rolled-back transaction still burns its number (Requirement 10.5).
        assert allocate_ticket_number(session) == 10002
        session.rollback()
        assert allocate_ticket_number(session) == 10003
        session.commit()

        session.execute(sa.text("SELECT setval('svc_ticket_number_seq', 99999)"))
        session.commit()
        assert remaining_numbers(session) == 0
        session.rollback()

        with pytest.raises(TicketNumbersExhausted) as raised:
            allocate_ticket_number(session)
        assert raised.value.code == "ticket_numbers_exhausted"
        session.rollback()

        # No wrap-around: the sequence still refuses and still reports nothing left.
        with pytest.raises(TicketNumbersExhausted):
            allocate_ticket_number(session)
        session.rollback()
        assert remaining_numbers(session) == 0