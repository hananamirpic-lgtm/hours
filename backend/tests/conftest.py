"""Test configuration.

Environment values are forced rather than defaulted so a developer's local `.env` cannot change a
test outcome. Every host points at a closed local port: nothing here reaches a real dependency
unless a test deliberately probes one.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Importing app modules here is safe: nothing under `app` reads the environment at import time —
# `Settings` is instantiated on first use, by which point the values below are in place.
from app.db.base import Base
from auth_support import DEFAULT_PASSWORD
from sample_models import SampleBase

# Ports chosen to be closed, so real probes fail fast with a connection error instead of hanging.
TEST_ENV: dict[str, str] = {
    "ENVIRONMENT": "test",
    "LOG_LEVEL": "WARNING",
    "APP_TIMEZONE": "Asia/Jerusalem",
    "CORS_ALLOWED_ORIGINS": "http://localhost:5173,http://localhost:4173",
    "DATABASE_URL": "postgresql+psycopg://test:test@127.0.0.1:59901/test",
    "REDIS_URL": "redis://127.0.0.1:59902/0",
    "JWT_SECRET_KEY": "test-jwt-secret-key-not-a-real-secret-value",
    "ENCRYPTION_KEY": "test-encryption-key-not-a-real-secret-key",
    "S3_ENDPOINT_URL": "http://127.0.0.1:59903",
    "S3_ACCESS_KEY_ID": "test-access-key",
    "S3_SECRET_ACCESS_KEY": "test-secret-key",
    "S3_BUCKET_DOCUMENTS": "hours-documents-test",
    "PROBE_TIMEOUT_SECONDS": "1.0",
    # Pinned true so the mandatory-2FA gate is enforced under test regardless of a local dev value
    # (docker-compose sets REQUIRE_2FA_ENROLMENT=false for the running app); a test that wants it off
    # sets it deliberately. Without this, the app factory would read the relaxed value and flip the
    # process-wide `_ENFORCE_2FA` off, silently disabling the 2FA-enrolment tests.
    "REQUIRE_2FA_ENROLMENT": "true",
}

for key, value in TEST_ENV.items():
    os.environ[key] = value


@pytest.fixture
def settings():
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        get_settings.cache_clear()

@pytest.fixture(autouse=True)
def _reset_2fa_enforcement():
    """Restore the process-wide 2FA enforcement flag after every test.

    `create_app` sets `app.models.user._ENFORCE_2FA` from `require_2fa_enrolment`, a module-level
    global. A test that builds an app with the flag relaxed — or one that toggles it directly — would
    otherwise leak `False` into later tests and silently disable the mandatory-2FA gate they assert
    on. Snapshotting and restoring it here keeps each test isolated from that global.
    """
    from app.models import user as user_model

    original = user_model._ENFORCE_2FA
    try:
        yield
    finally:
        user_model._ENFORCE_2FA = original


@pytest.fixture
def client(settings) -> Iterator:
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def api_client(settings, session: Session) -> Iterator:
    """Test client whose requests run against the in-memory database.

    The session is shared with the test rather than created per request, so a test can seed a user and
    then read back what the endpoint did to it. The auth service commits, which the SQLite engine
    handles the same way PostgreSQL does.
    """
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- in-memory database
# The encryption types and the audit writer need a real session: a fake session would prove that the
# code calls `add`, which is not the claim under test. What is under test is that a value goes into a
# column encrypted and comes back out plaintext, and that audit rows share the fate of the
# transaction that wrote them — both of which need a database round trip and neither of which needs
# PostgreSQL. SQLite in memory gives that in milliseconds. The things that genuinely require
# PostgreSQL (the exclusion constraint, the privilege revocation) are covered in tests/integration.


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """In-memory engine carrying every mapped table plus the sample table.

    `StaticPool` because every connection to `:memory:` gets its own private database; without it the
    session would find an empty schema.

    The whole of `Base.metadata` rather than a hand-listed subset: `change_logs` carries a foreign key
    to `users`, so the two cannot be created independently, and a list would need editing every time a
    model lands. The schema the application actually runs on is still the migration's — this is a
    convenience for tests that need a round trip and do not need PostgreSQL.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        # `check_same_thread` because `TestClient` runs the application on its own thread while the
        # test seeds and reads on the main one. Safe here: `StaticPool` hands out one connection and
        # the two threads take turns, since the test blocks on the request it made.
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    SampleBase.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(sqlite_engine: Engine) -> Iterator[Session]:
    """Session with the same settings the application uses, so behaviour matches production."""
    factory = sessionmaker(bind=sqlite_engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as opened:
        yield opened


# --------------------------------------------------------------------------- authentication support


@pytest.fixture(scope="session")
def default_password_hash() -> str:
    """`DEFAULT_PASSWORD` hashed once for the whole session.

    bcrypt at cost 12 is deliberately slow — around a quarter of a second — so hashing the same
    password once per test would add more time than the tests themselves take.
    """
    from app.core.security import hash_password

    return hash_password(DEFAULT_PASSWORD)


@pytest.fixture
def make_user(session: Session, default_password_hash: str):
    """Factory creating a committed user. Any column can be overridden by keyword."""
    from app.models.user import User, UserRole

    counter = itertools.count(1)

    def _make(**overrides) -> object:
        fields: dict[str, object] = {
            "username": f"user{next(counter)}",
            "password_hash": default_password_hash,
            "role": UserRole.SITE_MANAGER,
        }
        fields.update(overrides)
        created = User(**fields)
        session.add(created)
        session.commit()
        return created

    return _make
