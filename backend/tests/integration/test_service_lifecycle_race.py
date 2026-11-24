"""Racing status changes on PostgreSQL (Requirements 13.8, 14.8).

The design sequence is "customer cancel racing a staff status change".

SQLite has no row locks, so only PostgreSQL can show that the guarded `UPDATE` of `lifecycle.change_status`
lets exactly one of two concurrent requests through. Each test upgrades a scratch database to head (never
the development database) and runs the requests from two threads on two connections. A barrier on the
guarded `UPDATE` makes both requests read the same stored status before either writes, which is the
interleaving the design sequence diagram shows: the first `UPDATE` takes the row lock, the second waits for
it, re-evaluates `status = :expected` once the first commits, and matches no row.

Skips without `TEST_DATABASE_URL`.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alembic import command
from alembic.config import Config
from app.models.user import UserRole
from app.service_tickets.services.lifecycle import (
    CustomerActor,
    LifecycleError,
    change_status,
)
from app.service_tickets.services.resolution import Resolution
from app.service_tickets.services.staff_identity import StaffPrincipal

BASE_TIME = datetime(2031, 1, 1, 8, 0, tzinfo=UTC)
LONG_AGO = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
ROUNDS = 5

#: A valid resolution, required by every change to `closed` (R29.1). Without parts.
RESOLUTION = Resolution(
    entry_text="Replaced the washer", technician_name="Dana", parts_replaced=False, part_ids=[]
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


def _staff(name: str) -> StaffPrincipal:
    return StaffPrincipal(uuid.uuid4(), name, UserRole.TECHNICIAN, False, False)


class Seed:
    """One customer and one category, and a way to add tickets in a given status."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._next_number = 10000
        with engine.begin() as connection:
            self.customer_id = connection.execute(
                sa.text(
                    "INSERT INTO svc_customers (phone, full_name, full_name_key, password_hash) "
                    "VALUES ('0501234567', 'Owner', 'owner', 'x') RETURNING id"
                )
            ).scalar_one()
            self.category_id = connection.execute(
                sa.text(
                    "INSERT INTO svc_categories (name_he, name_en, name_he_key, name_en_key) "
                    "VALUES ('קטגוריית בדיקה', 'Lifecycle test', 'קטגוריית בדיקה', 'lifecycle test') "
                    "RETURNING id"
                )
            ).scalar_one()

    def ticket(self, status: str = "received", name: str = "") -> int:
        number = self._next_number
        self._next_number += 1
        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO svc_tickets (ticket_number, customer_id, category_id, city, city_key, "
                    "apartment, phone, status, assigned_technician_name, created_at, created_date, "
                    "updated_at) "
                    "VALUES (:n, :c, :k, 'Haifa', 'haifa', '12', '0501234567', :s, :name, :t, :d, :t)"
                ),
                {
                    "n": number,
                    "c": self.customer_id,
                    "k": self.category_id,
                    "s": status,
                    "name": name,
                    "t": LONG_AGO,
                    "d": LONG_AGO.date(),
                },
            )
        return number

    def history_count(self, number: int) -> int:
        with self.engine.connect() as connection:
            return connection.execute(
                sa.text(
                    "SELECT count(*) FROM svc_history_entries h JOIN svc_tickets t ON t.id = h.ticket_id "
                    "WHERE t.ticket_number = :n"
                ),
                {"n": number},
            ).scalar_one()

    def row(self, number: int) -> sa.Row[Any]:
        with self.engine.connect() as connection:
            return connection.execute(
                sa.text(
                    "SELECT status, assigned_technician_name, updated_at FROM svc_tickets "
                    "WHERE ticket_number = :n"
                ),
                {"n": number},
            ).one()


def _race(engine: Engine, number: int, requests: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Run each request on its own thread and connection; return `(outcome, detail)` per request.

    Both threads wait at a barrier right before the guarded `UPDATE`, so both have read the stored status
    by then. An outcome is `applied` (detail: the stored status the call returned), `rejected` (detail: the
    error code) or `crashed`.
    """
    barrier = threading.Barrier(len(requests), timeout=20)
    results: list[tuple[str, str]] = [("crashed", "did not finish")] * len(requests)

    def hook(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if statement.lstrip().upper().startswith("UPDATE SVC_TICKETS"):
            barrier.wait()

    def worker(index: int, request: dict[str, Any]) -> None:
        with Session(engine) as session:
            try:
                ticket = change_status(session, number, **request)
                session.commit()
                results[index] = ("applied", ticket.status)
            except LifecycleError as error:
                session.rollback()
                results[index] = ("rejected", error.code)
            except BaseException as error:  # noqa: BLE001 - reported through the result, asserted by the test
                session.rollback()
                results[index] = ("crashed", repr(error))

    event.listen(engine, "before_cursor_execute", hook)
    try:
        threads = [threading.Thread(target=worker, args=(i, r)) for i, r in enumerate(requests)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not any(thread.is_alive() for thread in threads), "a request is still waiting after 60 s"
    finally:
        event.remove(engine, "before_cursor_execute", hook)
    return results


def _applied(results: list[tuple[str, str]]) -> list[int]:
    return [index for index, (outcome, _) in enumerate(results) if outcome == "applied"]


def test_customer_cancel_racing_a_staff_approval_applies_exactly_one(scratch_engine: Engine) -> None:
    seed = Seed(scratch_engine)
    owner = CustomerActor(seed.customer_id)
    staff = _staff("tech.dana")
    winners: set[str] = set()

    for round_number in range(ROUNDS):
        number = seed.ticket("received")
        cancel_time = BASE_TIME + timedelta(minutes=2 * round_number)
        approve_time = cancel_time + timedelta(minutes=1)
        requests = [
            {"expected": "received", "target": "cancelled", "actor": owner, "now": cancel_time},
            {
                "expected": "received",
                "target": "in_progress",
                "actor": staff,
                "technician_name": "Dana Levi",
                "now": approve_time,
            },
        ]
        results = _race(scratch_engine, number, requests)

        assert not [r for r in results if r[0] == "crashed"], results
        (winner,) = _applied(results)
        loser = 1 - winner
        assert results[loser] == ("rejected", "ticket_status_changed")

        stored = seed.row(number)
        assert stored.status == requests[winner]["target"]
        assert stored.updated_at == requests[winner]["now"]  # the loser wrote no time
        if requests[winner]["target"] == "in_progress":
            assert stored.assigned_technician_name == "Dana Levi"
        else:
            assert stored.assigned_technician_name == ""  # the loser wrote no name
        winners.add(str(requests[winner]["target"]))

    assert winners <= {"cancelled", "in_progress"}


def test_two_staff_approvals_with_different_names_apply_exactly_one(scratch_engine: Engine) -> None:
    seed = Seed(scratch_engine)
    for round_number in range(ROUNDS):
        number = seed.ticket("received")
        base = BASE_TIME + timedelta(minutes=2 * round_number)
        requests = [
            {
                "expected": "received",
                "target": "in_progress",
                "actor": _staff("first"),
                "technician_name": "First Tech",
                "now": base,
            },
            {
                "expected": "received",
                "target": "in_progress",
                "actor": _staff("second"),
                "technician_name": "Second Tech",
                "now": base + timedelta(minutes=1),
            },
        ]
        results = _race(scratch_engine, number, requests)

        assert not [r for r in results if r[0] == "crashed"], results
        (winner,) = _applied(results)
        assert results[1 - winner] == ("rejected", "ticket_status_changed")
        stored = seed.row(number)
        assert stored.status == "in_progress"
        assert stored.assigned_technician_name == requests[winner]["technician_name"]
        assert stored.updated_at == requests[winner]["now"]


def test_staff_close_racing_a_customer_cancel_applies_exactly_one(scratch_engine: Engine) -> None:
    seed = Seed(scratch_engine)
    owner = CustomerActor(seed.customer_id)
    for round_number in range(ROUNDS):
        number = seed.ticket("in_progress", name="Dana")
        base = BASE_TIME + timedelta(minutes=2 * round_number)
        requests = [
            {
                "expected": "in_progress",
                "target": "closed",
                "actor": _staff("tech"),
                "resolution": RESOLUTION,
                "now": base,
            },
            {
                "expected": "in_progress",
                "target": "cancelled",
                "actor": owner,
                "now": base + timedelta(minutes=1),
            },
        ]
        results = _race(scratch_engine, number, requests)

        assert not [r for r in results if r[0] == "crashed"], results
        (winner,) = _applied(results)
        assert results[1 - winner] == ("rejected", "ticket_status_changed")
        stored = seed.row(number)
        assert stored.status == requests[winner]["target"]
        assert stored.assigned_technician_name == "Dana"
        assert stored.updated_at == requests[winner]["now"]
        # The resolution rides with the closing change: a lost race leaves no history entry behind (R29.2).
        assert seed.history_count(number) == (1 if requests[winner]["target"] == "closed" else 0)


def test_status_changes_on_postgresql_follow_the_name_and_time_rules(scratch_engine: Engine) -> None:
    """The SQL itself (`COALESCE` over a NULL bind, the `status_valid` check) on the real database."""
    seed = Seed(scratch_engine)
    number = seed.ticket("received")
    staff = _staff("tech.dana")
    owner = CustomerActor(seed.customer_id)

    def run(expected: str, target: str, actor: Any, name: str | None, at: datetime) -> None:
        resolution = RESOLUTION if target == "closed" else None
        with Session(scratch_engine) as session:
            change_status(session, number, expected, target, actor, name, resolution, now=at)
            session.commit()

    run("received", "in_progress", staff, None, BASE_TIME)
    assert seed.row(number).assigned_technician_name == "tech.dana"  # defaulted to the acting username
    run("in_progress", "closed", staff, "Ignored", BASE_TIME + timedelta(hours=1))
    run("closed", "in_progress", staff, "Ignored", BASE_TIME + timedelta(hours=2))  # reopen
    stored = seed.row(number)
    assert (stored.status, stored.assigned_technician_name) == ("in_progress", "tech.dana")
    assert stored.updated_at == BASE_TIME + timedelta(hours=2)

    run("in_progress", "cancelled", owner, None, BASE_TIME + timedelta(hours=3))
    stored = seed.row(number)
    assert (stored.status, stored.assigned_technician_name) == ("cancelled", "tech.dana")

    with Session(scratch_engine) as session, pytest.raises(LifecycleError) as raised:
        change_status(session, number, "cancelled", "in_progress", staff, now=BASE_TIME + timedelta(hours=4))
    assert raised.value.code == "invalid_transition"
    assert seed.row(number).updated_at == BASE_TIME + timedelta(hours=3)
