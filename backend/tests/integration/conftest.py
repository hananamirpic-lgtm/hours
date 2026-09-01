"""Fixtures for tests that need a real PostgreSQL 16 instance.

These tests assert things only a database can tell us: that the migration applies and reverses, that
an exclusion constraint actually rejects an overlap, that a privilege revocation actually denies an
UPDATE. A fake or an in-memory substitute would assert nothing.

Point `TEST_DATABASE_URL` at any PostgreSQL the caller may create databases on — the local
docker-compose instance is the obvious one:

    $env:TEST_DATABASE_URL = "postgresql+psycopg://hours:...@127.0.0.1:5432/hours"
    pytest tests/integration

Each fixture works on a freshly created, uniquely named database and drops it afterwards, so the
migration always meets an empty schema and a failed run never leaves a half-migrated database
behind. Without the variable, or without a reachable server, these tests skip rather than fail:
a developer working on the front end should not be blocked by a stopped container.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError

from alembic import command
from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parents[2]
INTEGRATION_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything collected from this directory `integration`, so `-m "not integration"` skips it."""
    for item in items:
        if INTEGRATION_DIR in Path(str(item.fspath)).parents:
            item.add_marker("integration")


def _admin_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; integration tests need a live PostgreSQL")
    return url


def _alembic_config(url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    # Absolute, so the tests do not depend on the working directory pytest was started from.
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # env.py prefers a URL already on the config over application settings, which is how a test
    # targets a throwaway database without touching the environment the application reads.
    config.set_main_option("sqlalchemy.url", url)
    return config


@contextmanager
def _scratch_database(admin_url: str) -> Iterator[str]:
    """Create an empty database, yield its URL, then drop it."""
    name = f"hours_test_{uuid.uuid4().hex[:12]}"
    admin_engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    except OperationalError as error:  # server down, wrong port, wrong credentials
        admin_engine.dispose()
        pytest.skip(f"PostgreSQL at TEST_DATABASE_URL is not reachable: {error.__class__.__name__}")
    try:
        # `str(url)` masks the password as `***`, which produces a URL that looks right and fails
        # authentication, so the password has to be rendered explicitly. Alembic escapes any `%`
        # in it when the URL reaches `set_main_option`, so no further quoting is needed here.
        yield make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin_engine.connect() as connection:
            # A lingering connection would make the drop fail and leave rubbish behind.
            connection.exec_driver_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %(name)s AND pid <> pg_backend_pid()",
                {"name": name},
            )
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')
        admin_engine.dispose()


@pytest.fixture
def make_alembic_config() -> Callable[[str], Config]:
    """Factory for an Alembic config aimed at a given database URL.

    Handed over as a fixture rather than imported from this module, so the test files need no
    knowledge of how conftest is packaged.
    """
    return _alembic_config


@pytest.fixture
def empty_database() -> Iterator[str]:
    """URL of a fresh, empty database. The test decides what to do with it."""
    with _scratch_database(_admin_url()) as url:
        yield url


@pytest.fixture(scope="session")
def migrated_engine() -> Iterator[Engine]:
    """Engine bound to a scratch database with the full migration applied.

    Session-scoped because applying the schema takes long enough that doing it per test would
    discourage writing tests. Isolation comes from `db`, which rolls every test back.
    """
    with _scratch_database(_admin_url()) as url:
        command.upgrade(_alembic_config(url), "head")
        engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[sa.Connection]:
    """Connection inside a transaction that is always rolled back.

    Tests may therefore insert freely, and a test that deliberately violates a constraint leaves
    nothing for the next one to trip over.
    """
    connection = migrated_engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def employee_id(db: sa.Connection) -> uuid.UUID:
    """One employee, with every mandatory column populated."""
    return db.execute(
        sa.text(
            """
            INSERT INTO employees (
                full_name, full_name_en, passport_number_encrypted, passport_number_hash,
                phone_encrypted, country, emergency_contact_name,
                emergency_contact_phone_encrypted, start_date
            ) VALUES (
                'אברהם כהן', 'Avraham Cohen', 'gcm:passport', 'hmac:passport',
                'gcm:phone', 'IL', 'Sarah Cohen', 'gcm:emergency', DATE '2025-01-01'
            ) RETURNING id
            """
        )
    ).scalar_one()


@pytest.fixture
def client_id(db: sa.Connection) -> uuid.UUID:
    return db.execute(
        sa.text("INSERT INTO clients (name) VALUES ('Kibbutz Rosh Tzurim') RETURNING id")
    ).scalar_one()


@pytest.fixture
def site_ids(db: sa.Connection, client_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Two sites for the same client — the multi-site day needs somewhere to move to."""
    created: list[uuid.UUID] = []
    for name, number in (("Site A", "S-001"), ("Site B", "S-002")):
        created.append(
            db.execute(
                sa.text(
                    """
                    INSERT INTO sites (name, site_number, client_id, qr_token)
                    VALUES (:name, :number, :client_id, :token)
                    RETURNING id
                    """
                ),
                {"name": name, "number": number, "client_id": client_id, "token": f"qr-{number}"},
            ).scalar_one()
        )
    return created[0], created[1]
