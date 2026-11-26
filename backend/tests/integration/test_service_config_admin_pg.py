"""The configuration service against real PostgreSQL (Requirements 9.3, 19.6, 19.7, 19.11, 19.12, 19.13).

The unit tests in `tests/test_service_config_admin.py` run on SQLite. What only PostgreSQL can show is here:

* the seed rows of migration 0011 as the form sees them, and `Category.seq` numbered by the identity
  column, so a new category comes last and renaming or reactivating never reorders;
* the real `RESTRICT` foreign key turning a delete of a used category into a deactivation inside a
  savepoint, with the transaction still usable afterwards;
* the last-active rule under concurrency: two requests that each take one of the last two active
  categories queue on `SELECT ... FOR UPDATE`, and the second is refused after the first commits. The test
  waits until PostgreSQL reports the second request blocked on a lock, so a missing lock fails the test
  instead of passing by luck;
* a unique violation from a concurrent writer of the same name, caught in a savepoint and reported as
  `duplicate_category`.

Skips without `TEST_DATABASE_URL`.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alembic import command
from alembic.config import Config
from app.service_tickets.grammars import fold_key
from app.service_tickets.models import FIELD_LABEL_KEYS, Category, Customer, Ticket
from app.service_tickets.services import config_admin as svc
from app.service_tickets.services.config_admin import (
    DEACTIVATED,
    REMOVED,
    DuplicateCategory,
    LastActiveCategory,
)

SEED_NAMES_EN = [
    "Leak or Blockage",
    "Dampness or Moisture",
    "Electrical",
    "Plumbing",
    "Walls and Finishing",
    "General",
]
SEED_NAMES_HE = ["דליפה / סתימה", "רטיבות / לחות", "חשמל", "אינסטלציה", "קירות וגמרים", "כללי"]


@pytest.fixture
def session(db: sa.Connection) -> Iterator[Session]:
    """A session inside the rolled-back test transaction; its commits release savepoints only."""
    with Session(bind=db, join_transaction_mode="create_savepoint", expire_on_commit=False) as opened:
        yield opened


def _ticket_for(session: Session, category: Category) -> Ticket:
    customer = Customer(
        phone="0501234567", full_name="Dana", full_name_key=fold_key("Dana"), password_hash="not-a-hash"
    )
    session.add(customer)
    session.flush()
    ticket = Ticket(
        ticket_number=10000,
        customer_id=customer.id,
        category_id=category.id,
        city="Haifa",
        city_key=fold_key("Haifa"),
        apartment="4",
        phone="0501234567",
        created_date=date(2025, 1, 1),
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    session.add(ticket)
    session.flush()
    return ticket


def _by_name(session: Session, name_en: str) -> Category:
    return session.execute(sa.select(Category).where(Category.name_en == name_en)).scalar_one()


# --- seeds and numbering ---------------------------------------------------------------------------------


def test_form_config_after_the_migration_has_the_seeded_data(session: Session) -> None:
    config = svc.get_form_config(session)
    assert [c.name_en for c in config.categories] == SEED_NAMES_EN
    assert [c.name_he for c in config.categories] == SEED_NAMES_HE
    assert list(config.labels) == list(FIELD_LABEL_KEYS)
    assert (config.labels["category"].he, config.labels["category"].en) == ("קטגוריית תקלה", "Issue category")
    assert (config.settings.description_required, config.settings.photo_upload_enabled) == (True, True)


def test_a_new_category_is_numbered_after_every_existing_one_and_order_is_stable(session: Session) -> None:
    created = svc.create_category(session, "גגות", "Roofs")
    session.flush()
    seqs = [c.seq for c in svc.list_categories(session)]
    assert len(seqs) == 7
    assert seqs == sorted(seqs) and len(set(seqs)) == 7
    assert created.seq == seqs[-1]

    # Renaming and a deactivate/reactivate round trip keep each category where it was.
    leak = _by_name(session, "Leak or Blockage")
    svc.rename_category(session, leak.id, "ZZZ אחרון בא\"ב", "Zzz last alphabetically")
    svc.deactivate_category(session, leak.id)
    svc.reactivate_category(session, leak.id)
    names = [c.name_en for c in svc.get_form_config(session).categories]
    assert names[0] == "Zzz last alphabetically"
    assert names[-1] == "Roofs"


def test_names_are_unique_per_language_by_folded_key(session: Session) -> None:
    with pytest.raises(DuplicateCategory) as raised:
        svc.create_category(session, "  חשמל ", "ELECTRICAL")
    assert raised.value.fields == ("name_he", "name_en")
    svc.create_category(session, "Привет", "Cyrillic")
    session.flush()
    with pytest.raises(DuplicateCategory):
        svc.create_category(session, "пРиВеТ", "Another")  # beyond ASCII, in a `--locale=C` database


# --- delete: the real foreign key ------------------------------------------------------------------------


def test_deleting_a_used_category_deactivates_it_and_the_transaction_stays_usable(session: Session) -> None:
    plumbing = _by_name(session, "Plumbing")
    ticket = _ticket_for(session, plumbing)
    assert svc.delete_category(session, plumbing.id) == DEACTIVATED
    session.commit()
    session.expire_all()
    assert session.get(Category, plumbing.id).is_active is False
    assert session.get(Ticket, ticket.id).category_id == plumbing.id
    assert "Plumbing" not in [c.name_en for c in svc.get_form_config(session).categories]
    assert [c.name_en for c in svc.list_categories(session)] == SEED_NAMES_EN  # still listed for filters


def test_deleting_an_unused_category_removes_it(session: Session) -> None:
    general = _by_name(session, "General")
    assert svc.delete_category(session, general.id) == REMOVED
    session.commit()
    assert session.get(Category, general.id) is None
    assert len(svc.list_categories(session)) == 5


def test_the_last_active_category_is_refused_on_postgresql(session: Session) -> None:
    for name in SEED_NAMES_EN[:-1]:
        svc.deactivate_category(session, _by_name(session, name).id)
    session.commit()
    general = _by_name(session, "General")
    with pytest.raises(LastActiveCategory):
        svc.deactivate_category(session, general.id)
    with pytest.raises(LastActiveCategory):
        svc.delete_category(session, general.id)
    assert [c.name_en for c in svc.get_form_config(session).categories] == ["General"]


# --- concurrency -----------------------------------------------------------------------------------------


@pytest.fixture
def engine(empty_database: str, make_alembic_config: Callable[[str], Config]) -> Iterator[Engine]:
    """A migrated scratch database whose transactions really commit, for tests with two sessions."""
    command.upgrade(make_alembic_config(empty_database), "head")
    created = sa.create_engine(empty_database, poolclass=sa.pool.NullPool)
    try:
        yield created
    finally:
        created.dispose()


def _blocked_on_lock(engine: Engine) -> int:
    with engine.connect() as connection:
        return connection.execute(
            sa.text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                "AND pid <> pg_backend_pid()"
            )
        ).scalar_one()


def _wait_until_blocked(engine: Engine, worker: threading.Thread) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _blocked_on_lock(engine) >= 1:
            return
        assert worker.is_alive(), "the second request finished without waiting for the first"
        time.sleep(0.05)
    pytest.fail("the second request never blocked on a lock; the active categories are not locked")


def _start(
    engine: Engine, operation: Callable[[Session], object], outcome: dict[str, object]
) -> threading.Thread:
    def run() -> None:
        with Session(engine, expire_on_commit=False) as second:
            try:
                outcome["result"] = operation(second)
                second.commit()
            except Exception as error:
                second.rollback()
                outcome["error"] = error

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker


def _keep_active_only(engine: Engine, keep: list[str]) -> dict[str, uuid.UUID]:
    """Deactivate every seeded category except those in `keep` (English names); return all ids by name."""
    with Session(engine) as setup:
        ids = {c.name_en: c.id for c in svc.list_categories(setup)}
        for name, category_id in ids.items():
            if name not in keep:
                svc.deactivate_category(setup, category_id)
        setup.commit()
    return ids


def _active_names(engine: Engine) -> list[str]:
    with Session(engine) as reader:
        return [c.name_en for c in svc.list_categories(reader, active_only=True)]


OPERATIONS = {
    "deactivate": svc.deactivate_category,
    "delete": svc.delete_category,
}


@pytest.mark.parametrize("second_operation", sorted(OPERATIONS))
@pytest.mark.parametrize("first_operation", sorted(OPERATIONS))
def test_two_requests_for_the_last_two_active_categories_cannot_both_succeed(
    engine: Engine, first_operation: str, second_operation: str
) -> None:
    ids = _keep_active_only(engine, keep=["Electrical", "Plumbing"])
    outcome: dict[str, object] = {}

    with Session(engine) as first:
        OPERATIONS[first_operation](first, ids["Electrical"])  # holds the locks, not yet committed
        worker = _start(engine, lambda s: OPERATIONS[second_operation](s, ids["Plumbing"]), outcome)
        _wait_until_blocked(engine, worker)
        assert not outcome  # still waiting
        first.commit()

    worker.join(timeout=10)
    assert not worker.is_alive()
    assert isinstance(outcome.get("error"), LastActiveCategory), outcome
    assert _active_names(engine) == ["Plumbing"]


def test_two_removals_with_a_third_category_left_both_succeed(engine: Engine) -> None:
    ids = _keep_active_only(engine, keep=["Electrical", "Plumbing", "General"])
    outcome: dict[str, object] = {}

    with Session(engine) as first:
        svc.deactivate_category(first, ids["Electrical"])
        worker = _start(engine, lambda s: svc.deactivate_category(s, ids["Plumbing"]), outcome)
        _wait_until_blocked(engine, worker)
        first.commit()

    worker.join(timeout=10)
    assert "error" not in outcome, outcome
    assert _active_names(engine) == ["General"]
    with Session(engine) as last, pytest.raises(LastActiveCategory):
        svc.deactivate_category(last, ids["General"])


def test_a_concurrent_writer_of_the_same_name_is_reported_as_a_duplicate(engine: Engine) -> None:
    outcome: dict[str, object] = {}

    with Session(engine) as first:
        svc.create_category(first, "גגות", "Roofs")  # inserted, not committed
        worker = _start(engine, lambda s: svc.create_category(s, " גגות", "ROOFS "), outcome)
        _wait_until_blocked(engine, worker)  # waits on the unique index entry of the first insert
        first.commit()

    worker.join(timeout=10)
    assert isinstance(outcome.get("error"), DuplicateCategory), outcome
    with Session(engine) as reader:
        assert [c.name_en for c in svc.list_categories(reader)].count("Roofs") == 1
        assert len(svc.list_categories(reader)) == 7
