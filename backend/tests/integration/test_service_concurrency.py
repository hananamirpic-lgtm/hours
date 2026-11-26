"""Concurrent writers of the service-ticket system on PostgreSQL (task 14.2; T11).

Requirements 3.4 (one customer per phone), 10.4 (ticket numbers are unique), 13.8 (exactly one of two
racing status changes is applied), 19.13 (the last active category cannot be deactivated or deleted by two
requests at once) and the sign-in lock of Requirement 5, with Properties 5 and 16 of the design.

SQLite has no sequences, row locks or advisory locks, so only PostgreSQL can show these. Every test upgrades a
scratch database to head (never the development database) and runs each writer on its own thread and its own
connection. Where the claim is "the guard decides", a barrier on the guarding statement makes both writers
get past their checks before either writes, so a missing guard fails the test instead of passing by luck.

Skips without `TEST_DATABASE_URL`.
"""

from __future__ import annotations

import math
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from alembic.config import Config
from app.core.config import Settings
from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.main import create_app
from app.models.user import User, UserRole
from app.service_tickets.config import ServiceConfig
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.services import config_admin
from app.service_tickets.services.config_admin import (
    FormSettings,
    LastActiveCategory,
)
from app.service_tickets.services.customers import (
    INITIAL_PASSWORD,
    PhoneAlreadyRegistered,
    create_customer,
)
from app.service_tickets.services.numbering import remaining_numbers
from app.service_tickets.services.signin import InvalidCredentials, Locked, Success, sign_in
from app.service_tickets.services.tickets import create_ticket, validate_submission

LONG_AGO = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
JOIN_TIMEOUT_SECONDS = 120


# --------------------------------------------------------------------------------------- fixtures


@pytest.fixture
def scratch_engine(
    empty_database: str, make_alembic_config: Callable[[str], Config]
) -> Iterator[Engine]:
    """A migrated scratch database whose transactions really commit."""
    command.upgrade(make_alembic_config(empty_database), "head")
    engine = sa.create_engine(empty_database, poolclass=sa.pool.NullPool)
    try:
        yield engine
    finally:
        engine.dispose()


# --------------------------------------------------------------------------------------- helpers


def _run_parallel(jobs: list[Callable[[], Any]]) -> list[Any]:
    """Run each job on its own thread and return the results in order.

    A job that raises contributes the exception object as its result, so a test can assert on it
    instead of losing it inside a thread.
    """
    results: list[Any] = [RuntimeError("did not finish")] * len(jobs)

    def run(index: int, job: Callable[[], Any]) -> None:
        try:
            results[index] = job()
        except BaseException as error:  # noqa: BLE001 - reported through the result, asserted by the test
            results[index] = error

    threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT_SECONDS)
    assert not any(t.is_alive() for t in threads), f"a writer is still waiting after {JOIN_TIMEOUT_SECONDS} s"
    return results


@contextmanager
def _barrier_before(
    engine: Engine, parties: int, matches: Callable[[str], bool]
) -> Iterator[threading.Barrier]:
    """Hold each thread, at its first statement for which `matches` is true, until all `parties` arrive."""
    barrier = threading.Barrier(parties, timeout=30)
    seen = threading.local()

    def hook(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if getattr(seen, "waited", False) or not matches(" ".join(statement.split()).upper()):
            return
        seen.waited = True
        barrier.wait()

    event.listen(engine, "before_cursor_execute", hook)
    try:
        yield barrier
    finally:
        event.remove(engine, "before_cursor_execute", hook)


def _scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as connection:
        return connection.execute(sa.text(sql), params).scalar_one()


def _insert_customer(engine: Engine, phone: str = "0501234567", *, password: str | None = None) -> uuid.UUID:
    password_hash = hash_password(password) if password is not None else "x"
    with engine.begin() as connection:
        return connection.execute(
            sa.text(
                "INSERT INTO svc_customers (phone, full_name, full_name_key, password_hash, "
                "must_change_password) VALUES (:p, 'Owner', 'owner', :h, false) RETURNING id"
            ),
            {"p": phone, "h": password_hash},
        ).scalar_one()


def _category_id(engine: Engine, name_en: str) -> uuid.UUID:
    return _scalar(engine, "SELECT id FROM svc_categories WHERE name_en = :n", n=name_en)


@dataclass(frozen=True)
class _Owner:
    id: uuid.UUID


# ----------------------------------------------------------------- Property 5: ticket numbers


def test_100_parallel_submissions_get_100_distinct_numbers(scratch_engine: Engine) -> None:
    """Requirement 10.4, Property 5 on the real `svc_ticket_number_seq`.

    25 threads, each submitting four tickets through the real validation and creation path, all started
    together. Nothing fails, so the 100 numbers are also consecutive from the start of the sequence.
    """
    owner = _Owner(_insert_customer(scratch_engine))
    category_id = _category_id(scratch_engine, "General")
    form_settings = FormSettings(description_required=True, photo_upload_enabled=True)
    threads, per_thread = 25, 4
    start = threading.Barrier(threads, timeout=30)

    def submit_many(index: int) -> list[int]:
        start.wait()
        numbers: list[int] = []
        for n in range(per_thread):
            values = {
                "city": "Haifa",
                "apartment": str(index * per_thread + n),
                "phone": "050-123-4567",
                "category_id": str(category_id),
                "description": f"leak {index}/{n}",
                "urgent": "false",
            }
            clean = validate_submission(values, form_settings, {category_id})
            with Session(scratch_engine) as session:
                ticket = create_ticket(session, owner, clean)
                number = ticket.ticket_number
                session.commit()
            numbers.append(number)
        return numbers

    results = _run_parallel([lambda i=i: submit_many(i) for i in range(threads)])

    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, errors
    numbers = [n for batch in results for n in batch]
    assert len(numbers) == 100
    assert len(set(numbers)) == 100, "two submissions were given the same ticket number"
    assert set(numbers) == set(range(10000, 10100))  # no failure burned a number, so no gaps

    with scratch_engine.connect() as connection:
        stored = connection.execute(sa.text("SELECT ticket_number FROM svc_tickets")).scalars().all()
    assert sorted(stored) == sorted(numbers)  # every returned number is a stored row, and the reverse

    with Session(scratch_engine) as session:
        assert remaining_numbers(session) == 99999 - 10099


# ----------------------------------------------------------------- Requirement 3.4: one phone


def test_two_parallel_creates_of_one_phone_leave_exactly_one_record(scratch_engine: Engine) -> None:
    """Both requests pass the "phone is free" read; the unique constraint lets exactly one insert through.

    The two spellings below normalize to the same ten digits (Requirement 4.8). A barrier on the INSERT
    makes both requests do their duplicate check before either one writes.
    """
    spellings = [
        ("050-555-6677", "+972 50 555 6677"),
        ("0505556678", "(050) 555-6678"),
        ("972505556679", "050 555 6679"),
    ]
    for first, second in spellings:
        phone = "0" + "".join(ch for ch in first if ch.isdigit())[-9:]

        def create(raw: str) -> str:
            with Session(scratch_engine) as session:
                try:
                    create_customer(session, phone=raw, full_name="Dana Levi", city="Haifa", apartment="4")
                    session.commit()
                except PhoneAlreadyRegistered:
                    session.rollback()
                    return "duplicate"
                return "created"

        with _barrier_before(scratch_engine, 2, lambda sql: sql.startswith("INSERT INTO SVC_CUSTOMERS")):
            results = _run_parallel([lambda raw=first: create(raw), lambda raw=second: create(raw)])

        assert sorted(results) == ["created", "duplicate"], results
        assert _scalar(scratch_engine, "SELECT count(*) FROM svc_customers WHERE phone = :p", p=phone) == 1


# ----------------------------------------------------------------- Property 16: one winner


@dataclass
class _Api:
    client_factory: Callable[[], TestClient]
    customer_id: uuid.UUID
    customer_headers: dict[str, str]
    staff_headers: dict[str, str]
    category_id: uuid.UUID


@pytest.fixture
def api(settings: Settings, scratch_engine: Engine) -> Iterator[_Api]:
    """The real application, flag on, serving the scratch database, with a customer and a technician."""
    app = create_app(settings.model_copy(update={"service_tickets_enabled": "true"}))
    factory = sessionmaker(bind=scratch_engine, autoflush=False, expire_on_commit=False, future=True)

    def request_session() -> Iterator[Session]:
        with factory() as per_request:
            yield per_request

    app.dependency_overrides[get_db] = request_session

    customer_id = _insert_customer(scratch_engine)
    with Session(scratch_engine) as session:
        technician = User(
            username="tech.dana",
            password_hash="not-a-hash",
            role=UserRole.TECHNICIAN,
            must_change_password=False,
            is_2fa_enabled=True,
        )
        session.add(technician)
        session.commit()
        staff_token = create_access_token(
            user_id=technician.id, token_version=technician.token_version, role=technician.role.value
        )
    customer_token = mint_customer_token(customer_id, 1)
    try:
        yield _Api(
            client_factory=lambda: TestClient(app, raise_server_exceptions=False),
            customer_id=customer_id,
            customer_headers={"Authorization": f"Bearer {customer_token}"},
            staff_headers={"Authorization": f"Bearer {staff_token}"},
            category_id=_category_id(scratch_engine, "General"),
        )
    finally:
        app.dependency_overrides.clear()


def _new_ticket(engine: Engine, api: _Api, number: int, status: str = "received") -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO svc_tickets (ticket_number, customer_id, category_id, city, city_key, "
                "apartment, phone, status, assigned_technician_name, created_at, created_date, updated_at) "
                "VALUES (:n, :c, :k, 'Haifa', 'haifa', '12', '0501234567', :s, '', :t, :d, :t)"
            ),
            {
                "n": number,
                "c": api.customer_id,
                "k": api.category_id,
                "s": status,
                "t": LONG_AGO,
                "d": LONG_AGO.date(),
            },
        )


def test_customer_cancel_and_staff_status_change_over_http_apply_exactly_one(
    scratch_engine: Engine, api: _Api
) -> None:
    """Property 16 through the real routers: one 200, one 409 `ticket_status_changed`, and a coherent row.

    The barrier sits on the guarded `UPDATE svc_tickets`, so both requests have read `received` and passed
    the transition check when they reach it; the row lock and the `status = :expected` guard decide.
    """
    winners: set[str] = set()
    for round_number in range(5):
        number = 30000 + round_number
        _new_ticket(scratch_engine, api, number)

        def cancel(n: int = number) -> tuple[int, Any]:
            response = api.client_factory().post(
                f"/api/service/tickets/{n}/cancel",
                json={"expected_status": "received"},
                headers=api.customer_headers,
            )
            return response.status_code, response.json()

        def approve(n: int = number) -> tuple[int, Any]:
            response = api.client_factory().post(
                f"/api/service/staff/tickets/{n}/status",
                json={
                    "expected_status": "received",
                    "target_status": "in_progress",
                    "assigned_technician_name": "Dana Levi",
                },
                headers=api.staff_headers,
            )
            return response.status_code, response.json()

        with _barrier_before(scratch_engine, 2, lambda sql: sql.startswith("UPDATE SVC_TICKETS")):
            results = _run_parallel([cancel, approve])

        assert not [r for r in results if isinstance(r, BaseException)], results
        statuses = [status for status, _ in results]
        assert sorted(statuses) == [200, 409], results
        winner = statuses.index(200)
        loser = 1 - winner
        assert results[loser][1]["detail"]["error"]["code"] == "ticket_status_changed"

        stored = _row(scratch_engine, number)
        expected_status = ("cancelled", "in_progress")[winner]
        assert stored.status == expected_status
        assert results[winner][1]["status"] == expected_status
        # The loser changed nothing: the technician name is set only by the approval that won.
        assert stored.assigned_technician_name == ("Dana Levi" if winner == 1 else "")
        winners.add(expected_status)

    assert winners <= {"cancelled", "in_progress"}


def _row(engine: Engine, number: int) -> sa.Row[Any]:
    with engine.connect() as connection:
        return connection.execute(
            sa.text("SELECT status, assigned_technician_name FROM svc_tickets WHERE ticket_number = :n"),
            {"n": number},
        ).one()


# ----------------------------------------------------------------- Requirement 19.13: last category


def _only_active(engine: Engine, names: list[str]) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE svc_categories SET is_active = (name_en = ANY(:names))"), {"names": names}
        )


def _active_names(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                sa.text("SELECT name_en FROM svc_categories WHERE is_active ORDER BY seq")
            ).scalars()
        )


def _remove_in_session(
    engine: Engine, operation: Callable[[Session, uuid.UUID], object], target: uuid.UUID
) -> str:
    with Session(engine) as session:
        try:
            operation(session, target)
            session.commit()
        except LastActiveCategory:
            session.rollback()
            return "refused"
        return "applied"


def _takes_locks_on_active_categories(sql: str) -> bool:
    return "SVC_CATEGORIES" in sql and sql.startswith("SELECT") and sql.endswith("FOR UPDATE")


def test_two_parallel_deactivations_of_the_last_two_categories_leave_one_active(
    scratch_engine: Engine,
) -> None:
    last_two = ["Plumbing", "General"]
    ids = {name: _category_id(scratch_engine, name) for name in last_two}
    survivors: set[str] = set()

    for _ in range(3):
        _only_active(scratch_engine, last_two)
        with _barrier_before(scratch_engine, 2, _takes_locks_on_active_categories):
            results = _run_parallel(
                [
                    lambda name=name: _remove_in_session(
                        scratch_engine, config_admin.deactivate_category, ids[name]
                    )
                    for name in last_two
                ]
            )

        assert sorted(results) == ["applied", "refused"], results
        remaining = _active_names(scratch_engine)
        assert len(remaining) == 1, remaining
        # The refused request is the one whose category is the survivor.
        assert remaining == [last_two[results.index("refused")]]
        survivors.add(remaining[0])

    assert survivors <= set(last_two)


def test_parallel_deactivation_of_every_category_leaves_exactly_one_active(scratch_engine: Engine) -> None:
    with scratch_engine.connect() as connection:
        ids = list(connection.execute(sa.text("SELECT id FROM svc_categories ORDER BY seq")).scalars())
    assert len(ids) == 6

    with _barrier_before(scratch_engine, len(ids), _takes_locks_on_active_categories):
        results = _run_parallel(
            [
                lambda target=target: _remove_in_session(
                    scratch_engine, config_admin.deactivate_category, target
                )
                for target in ids
            ]
        )

    assert sorted(results) == ["applied"] * 5 + ["refused"], results
    assert len(_active_names(scratch_engine)) == 1


def test_two_parallel_deletions_of_the_last_two_categories_leave_one(scratch_engine: Engine) -> None:
    last_two = ["Plumbing", "General"]
    ids = {name: _category_id(scratch_engine, name) for name in last_two}
    _only_active(scratch_engine, last_two)

    with _barrier_before(scratch_engine, 2, _takes_locks_on_active_categories):
        results = _run_parallel(
            [
                lambda name=name: _remove_in_session(scratch_engine, config_admin.delete_category, ids[name])
                for name in last_two
            ]
        )

    assert sorted(results) == ["applied", "refused"], results
    assert len(_active_names(scratch_engine)) == 1


# ----------------------------------------------------------------- Requirement 5: sign-in lock


def test_parallel_wrong_guesses_record_exactly_the_lock_attempts_and_the_rest_are_locked(
    settings: Settings, scratch_engine: Engine
) -> None:
    """The advisory lock serializes guesses on one phone, so the count each one reads is up to date.

    `signin.sign_in` promises: parallel guesses queue up behind each other, the first `signin_lock_attempts`
    of them are verified and recorded, and every later one is answered as locked with nothing recorded
    (R5.6, R5.13). Without the lock, all of them read "not locked" before any wrote its row, and the table
    would hold one row per guess.
    """
    config = ServiceConfig.from_settings(settings)
    limit = config.signin_lock_attempts
    guesses = limit * 4
    phone = "0501234567"
    _insert_customer(scratch_engine, phone, password=INITIAL_PASSWORD)
    start = threading.Barrier(guesses, timeout=30)

    def guess(password: object) -> tuple[object, datetime]:
        start.wait()
        # Taken before the request queues on the phone's lock, as the router does (`now=_now()`).
        now = datetime.now(UTC)
        with Session(scratch_engine) as session:
            outcome = sign_in(session, phone_raw=phone, password_raw=password, now=now, settings=config)
            session.commit()  # the router commits every outcome, so the failure row survives
            return outcome, now

    results = _run_parallel([lambda: guess("9999") for _ in range(guesses)])

    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, errors
    verified = [outcome for outcome, _ in results if isinstance(outcome, InvalidCredentials)]
    locked = [(outcome, now) for outcome, now in results if isinstance(outcome, Locked)]
    assert len(verified) == limit, f"{len(verified)} guesses were verified, expected {limit}"
    assert len(locked) == guesses - limit
    rows = _scalar(scratch_engine, "SELECT count(*) FROM svc_signin_failures WHERE phone = :p", p=phone)
    assert rows == limit, f"{rows} failure rows recorded for {guesses} parallel guesses"

    # `Retry-After` is the whole seconds, rounded up, until the lock window of the newest failure ends.
    newest = _scalar(
        scratch_engine, "SELECT max(failed_at) FROM svc_signin_failures WHERE phone = :p", p=phone
    )
    window = timedelta(seconds=config.signin_lock_seconds)
    for outcome, now in locked:
        expected = max(1, math.ceil((newest + window - now).total_seconds()))
        assert outcome.retry_after == expected, (outcome.retry_after, expected)

    # The lock holds against the right password too, and a locked request records nothing.
    start = threading.Barrier(1, timeout=30)
    (after,) = _run_parallel([lambda: guess(INITIAL_PASSWORD)])
    assert isinstance(after, tuple) and isinstance(after[0], Locked), after
    assert not isinstance(after[0], Success)
    rows = _scalar(scratch_engine, "SELECT count(*) FROM svc_signin_failures WHERE phone = :p", p=phone)
    assert rows == limit
