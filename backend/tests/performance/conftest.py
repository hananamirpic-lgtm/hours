"""Fixtures for the performance and load tests, gated on a live PostgreSQL 16.

Requirement 24.1 (a scan under 2 s at the 95th percentile with 200 concurrent employees) and the
scale claim of 24.3 (500 employees, 100 sites without schema change) can only be measured against the
real database. The scan state machine's one-open-entry guarantee is the partial unique index and the
overlap exclusion constraint — both PostgreSQL-only (`btree_gist`), and the whole point of a
concurrency test is that the *database* enforces the invariant when application locks race. SQLite
would prove nothing here.

So this mirrors `tests/integration/conftest.py` exactly: point `TEST_DATABASE_URL` at a PostgreSQL the
caller may create databases on (the local docker-compose instance is the obvious one), and each run
works on a freshly created, uniquely named database that is dropped afterwards. Without the variable,
or without a reachable server, these tests **skip** rather than fail — a developer without the
container running, or CI on a front-end change, is not blocked by a stopped database.

    $env:TEST_DATABASE_URL = "postgresql+psycopg://hours:...@127.0.0.1:5432/hours"
    pytest tests/performance

The gating and scratch-database plumbing is duplicated from the integration conftest rather than
imported: pytest does not put sibling test packages on a shared import path, and a performance run
should not depend on the integration package being importable. The two are deliberately the same
shape, so a reader who knows one knows the other.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError

from alembic import command
from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parents[2]
PERFORMANCE_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything collected here `performance` and `slow`, so `-m "not slow"` skips it.

    A performance run builds large datasets and, for the load test, spins up many threads; none of it
    belongs in the fast unit loop. The markers let a caller include or exclude the whole directory
    without naming files.
    """
    for item in items:
        if PERFORMANCE_DIR in Path(str(item.fspath)).parents:
            item.add_marker("performance")
            item.add_marker("slow")


def _admin_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; performance tests need a live PostgreSQL")
    return url


def _alembic_config(url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@contextmanager
def _scratch_database(admin_url: str) -> Iterator[str]:
    """Create an empty database, yield its URL, then drop it."""
    name = f"hours_perf_{uuid.uuid4().hex[:12]}"
    admin_engine = sa.create_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool
    )
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    except OperationalError as error:  # server down, wrong port, wrong credentials
        admin_engine.dispose()
        pytest.skip(f"PostgreSQL at TEST_DATABASE_URL is not reachable: {error.__class__.__name__}")
    try:
        yield make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %(name)s AND pid <> pg_backend_pid()",
                {"name": name},
            )
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')
        admin_engine.dispose()


@pytest.fixture(scope="session")
def perf_engine() -> Iterator[Engine]:
    """Engine bound to a scratch database with the full migration applied.

    Session-scoped: applying the schema is the slow part, and the load and scale tests each want a
    fresh, migrated database rather than a rolled-back transaction — a concurrency test needs its
    writes to actually commit so parallel connections see one another, which a rollback fixture would
    forbid. Each test cleans up the rows it wrote (or the whole database is dropped at session end).

    A QueuePool sized for the load test's concurrency, so 200 threads do not serialise on a
    single-connection pool and turn a concurrency test into a queue-depth test.
    """
    with _scratch_database(_admin_url()) as url:
        command.upgrade(_alembic_config(url), "head")
        engine = sa.create_engine(url, pool_size=32, max_overflow=64, pool_timeout=30)
        try:
            yield engine
        finally:
            engine.dispose()
