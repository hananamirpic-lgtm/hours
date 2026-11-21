"""The customers service against real PostgreSQL (Requirements 3.4, 3.9, 3.10).

What SQLite cannot show: two connections racing for one phone number, a row lock held by a reset, a
SAVEPOINT that is a savepoint and not a commit, and the list order and search on a database created with
`--locale=C`, where `lower()` and `ILIKE` fold only ASCII. Skips without `TEST_DATABASE_URL`.

The races are made deterministic: a first transaction holds an uncommitted row, a second one is started
and observed waiting on its lock in `pg_stat_activity`, and only then does the first one commit.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.security import verify_password
from app.service_tickets.grammars import fold_key
from app.service_tickets.models import Customer, SigninFailure
from app.service_tickets.services.customers import (
    PhoneAlreadyRegistered,
    create_customer,
    deactivate_customer,
    list_customers,
    reset_password,
    update_customer,
)

PHONE = "0505556677"
OTHER_PHONE = "0521234567"


@pytest.fixture
def engine(migrated_engine: Engine) -> Iterator[Engine]:
    """The migrated scratch database, emptied of customers before and after the test."""

    def clean() -> None:
        with migrated_engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM svc_signin_failures"))
            connection.execute(sa.text("DELETE FROM svc_customers"))

    clean()
    yield migrated_engine
    clean()


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()


def _wait_until_blocked(engine: Engine, timeout: float = 10.0) -> None:
    """Wait until some backend of this database waits on a lock held by another transaction."""
    deadline = time.monotonic() + timeout
    query = sa.text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
    )
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            if connection.execute(query).scalar_one() > 0:
                return
        time.sleep(0.05)
    raise AssertionError("no transaction ever blocked on the lock")


def _in_thread(work: Callable[[], object]) -> tuple[threading.Thread, dict[str, object]]:
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["value"] = work()
        except BaseException as error:  # noqa: BLE001 - handed to the test thread
            outcome["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def test_two_requests_for_one_phone_leave_exactly_one_record(engine: Engine) -> None:
    holder = Session(engine)
    create_customer(holder, phone=PHONE, full_name="Winner")  # flushed, uncommitted: the unique entry is held

    def loser() -> str:
        with Session(engine) as other:
            # Earlier work in the same transaction must survive the lost race.
            create_customer(other, phone=OTHER_PHONE, full_name="Bystander")
            try:
                create_customer(other, phone="050-555-6677", full_name="Loser")
            except PhoneAlreadyRegistered as error:
                other.commit()
                return error.code
            other.commit()
            return "created"

    thread, outcome = _in_thread(loser)
    try:
        _wait_until_blocked(engine)
        holder.commit()
    finally:
        holder.close()
    thread.join(timeout=20)

    assert not thread.is_alive()
    assert outcome.get("error") is None, outcome
    assert outcome["value"] == "phone_already_registered"
    with Session(engine) as check:
        rows = check.execute(sa.select(Customer.phone, Customer.full_name).order_by(Customer.phone)).all()
    assert [tuple(row) for row in rows] == [(PHONE, "Winner"), (OTHER_PHONE, "Bystander")]


def test_a_phone_edit_that_loses_the_race_is_refused_and_changes_nothing(engine: Engine) -> None:
    with Session(engine) as setup:
        mover = create_customer(setup, phone=OTHER_PHONE, full_name="Mover")
        setup.commit()

    holder = Session(engine)
    create_customer(holder, phone=PHONE, full_name="Winner")

    def edit() -> str:
        with Session(engine) as other:
            try:
                update_customer(other, mover.id, phone=PHONE, full_name="Renamed")
            except PhoneAlreadyRegistered as error:
                other.commit()
                return error.code
            return "updated"

    thread, outcome = _in_thread(edit)
    try:
        _wait_until_blocked(engine)
        holder.commit()
    finally:
        holder.close()
    thread.join(timeout=20)

    assert outcome.get("error") is None, outcome
    assert outcome["value"] == "phone_already_registered"
    with Session(engine) as check:
        row = check.get(Customer, mover.id)
        assert (row.phone, row.full_name) == (OTHER_PHONE, "Mover")


def test_the_service_never_commits_even_with_its_savepoints(engine: Engine) -> None:
    with Session(engine) as session:
        created = create_customer(session, phone=PHONE, full_name="Avi")
        deactivate_customer(session, created.id)
        reset_password(session, created.id)
        update_customer(session, created.id, phone=OTHER_PHONE, full_name="Avi 2")
        session.rollback()
    assert _count(engine, "svc_customers") == 0


def test_two_concurrent_resets_do_not_lose_a_token_version_increment(engine: Engine) -> None:
    with Session(engine) as setup:
        created = create_customer(setup, phone=PHONE, full_name="Avi")
        setup.add(SigninFailure(phone=PHONE, failed_at=datetime.now(UTC)))
        setup.commit()

    first = Session(engine)
    reset_password(first, created.id)  # holds the row lock until commit

    def second_reset() -> None:
        with Session(engine) as other:
            reset_password(other, created.id)
            other.commit()

    thread, outcome = _in_thread(second_reset)
    try:
        _wait_until_blocked(engine)
        first.commit()
    finally:
        first.close()
    thread.join(timeout=20)

    assert outcome.get("error") is None, outcome
    with Session(engine) as check:
        row = check.get(Customer, created.id)
        assert row.token_version == 3
        assert verify_password("1234", row.password_hash)
        assert row.must_change_password is True
    assert _count(engine, "svc_signin_failures") == 0


def test_list_order_and_search_follow_the_python_fold_on_a_c_locale_database(engine: Engine) -> None:
    names = ["bravo", "Alpha", "ÉCLAIR", "éa", "Straße", "STRASSE x", "אבי", "100% sure", "a_b", "axb"]
    with Session(engine) as session:
        for number, name in enumerate(names):
            session.add(
                Customer(
                    id=uuid.uuid4(),
                    phone=f"05{number:08d}",
                    full_name=name,
                    full_name_key=fold_key(name),
                    password_hash="x" * 60,
                )
            )
        session.commit()

        listed = [item.full_name for item in list_customers(session).items]
        assert listed == sorted(names, key=fold_key)

        def found(search: str) -> set[str]:
            return {item.full_name for item in list_customers(session, search=search).items}

        assert found("éCL") == {"ÉCLAIR"}  # `lower()` on a C locale would not fold É
        assert found("strasse") == {"Straße", "STRASSE x"}  # casefold folds ß to ss
        assert found("%") == {"100% sure"}
        assert found("_") == {"a_b"}
        assert found("a_b") == {"a_b"}
        assert len(found("0000000")) == 10  # a phone substring that every seeded number contains
        assert found("(050) 000-0001") == {"Alpha"}
