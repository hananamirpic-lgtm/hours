"""The seed against a real PostgreSQL.

Idempotency is the property under test, and it is a property of the database's unique constraints —
`settings.key`, `holidays.date`, `users.username` — not of the Python. So these run against a real
server: a second seed run must insert nothing and must not raise, which only a database that actually
enforces those keys can demonstrate.

Migration 0003 has already seeded the session-scoped `migrated_engine`, so the reference-data tests
here assert the *result* of that seed and then prove a re-run is a no-op. The migration's own
apply-and-reverse is covered on a throwaway database so a downgrade cannot destroy the shared schema.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from alembic import command
from alembic.config import Config
from app.core.config import Settings
from app.core.hebrew_calendar import israeli_holidays_for_gregorian_year
from app.seed import (
    SETTINGS_DEFAULTS,
    holiday_years,
    seed_bootstrap_admin,
    seed_holidays,
    seed_reference_data,
    seed_settings,
)


def _ci_settings(**overrides: object) -> Settings:
    """Settings pointed nowhere real, with fields a test wants to vary applied on top."""
    base: dict[str, object] = {
        "database_url": "postgresql+psycopg://x:x@127.0.0.1:1/x",
        "redis_url": "redis://127.0.0.1:1/0",
        "jwt_secret_key": "integration-jwt-secret-not-a-real-secret00",
        "encryption_key": "integration-encryption-secret-not-real-000",
        "s3_endpoint_url": "http://127.0.0.1:1",
        "s3_access_key_id": "k",
        "s3_secret_access_key": "s",
        "s3_bucket_documents": "b",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- reference data present


def test_migration_seeded_every_settings_default(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        rows = dict(connection.execute(sa.text("SELECT key, value FROM settings")).all())
    for default in SETTINGS_DEFAULTS:
        assert rows.get(default.key) == default.value


def test_migration_seeded_the_holiday_calendar_for_both_years(migrated_engine: Engine) -> None:
    expected = {
        holiday.date
        for year in holiday_years()
        for holiday in israeli_holidays_for_gregorian_year(year)
    }
    with migrated_engine.connect() as connection:
        present = set(
            connection.execute(
                sa.text("SELECT date FROM holidays WHERE date = ANY(:dates)"), {"dates": list(expected)}
            )
            .scalars()
            .all()
        )
    assert expected <= present, f"missing seeded holidays: {sorted(expected - present)}"


# --------------------------------------------------------------------------- idempotency


def test_reseeding_settings_inserts_nothing(db: sa.Connection) -> None:
    # The migration already seeded; a re-run over the same connection must insert zero rows and not
    # raise on the unique key.
    assert seed_settings(db) == 0


def test_reseeding_holidays_inserts_nothing(db: sa.Connection) -> None:
    assert seed_holidays(db) == 0


def test_reference_seed_is_a_no_op_on_an_already_seeded_database(db: sa.Connection) -> None:
    settings_inserted, holidays_inserted = seed_reference_data(db)
    assert (settings_inserted, holidays_inserted) == (0, 0)


def test_settings_seed_leaves_an_administrators_tuned_value_alone(db: sa.Connection) -> None:
    # An admin changed the overtime threshold; a later seed run must not stamp it back to 480.
    db.execute(
        sa.text("UPDATE settings SET value = '600' WHERE key = 'overtime_daily_threshold_minutes'")
    )
    seed_settings(db)
    kept = db.execute(
        sa.text("SELECT value FROM settings WHERE key = 'overtime_daily_threshold_minutes'")
    ).scalar_one()
    assert kept == "600"


# --------------------------------------------------------------------------- bootstrap admin


def test_bootstrap_admin_is_created_from_the_environment(db: sa.Connection) -> None:
    settings = _ci_settings(
        bootstrap_admin_username="root", bootstrap_admin_password="a-strong-bootstrap-password"
    )
    created = seed_bootstrap_admin(db, settings=settings)
    assert created is True

    row = db.execute(
        sa.text("SELECT role, is_active, is_2fa_enabled, password_hash FROM users WHERE username = 'root'")
    ).one()
    assert row.role == "admin"
    assert row.is_active is True
    # 2FA is left un-enrolled: the admin is required to enrol on first sign-in, not seeded with a
    # factor nobody has scanned.
    assert row.is_2fa_enabled is False
    # The password is stored only as a bcrypt hash, never in the clear.
    assert row.password_hash.startswith("$2b$")
    assert "a-strong-bootstrap-password" not in row.password_hash


def test_bootstrap_admin_is_not_created_twice(db: sa.Connection) -> None:
    settings = _ci_settings(
        bootstrap_admin_username="root", bootstrap_admin_password="a-strong-bootstrap-password"
    )
    assert seed_bootstrap_admin(db, settings=settings) is True
    # Second run finds the user and creates nothing.
    assert seed_bootstrap_admin(db, settings=settings) is False
    count = db.execute(
        sa.text("SELECT count(*) FROM users WHERE username = 'root'")
    ).scalar_one()
    assert count == 1


def test_bootstrap_admin_skipped_without_a_password(db: sa.Connection) -> None:
    settings = _ci_settings(bootstrap_admin_username="root", bootstrap_admin_password=None)
    assert seed_bootstrap_admin(db, settings=settings) is False
    count = db.execute(sa.text("SELECT count(*) FROM users WHERE username = 'root'")).scalar_one()
    assert count == 0


# --------------------------------------------------------------------------- migration apply/reverse


def test_migration_0003_seeds_and_reverses_on_a_scratch_database(
    empty_database: str, make_alembic_config: Callable[[str], Config]
) -> None:
    """Applying through 0003 seeds; downgrading to 0002 removes exactly the seeded rows.

    Run on a throwaway database so the downgrade cannot touch the shared schema.
    """
    config = make_alembic_config(empty_database)
    engine = sa.create_engine(empty_database, poolclass=sa.pool.NullPool)
    try:
        command.upgrade(config, "0003_seed_reference_data")
        with engine.connect() as connection:
            settings_count = connection.execute(sa.text("SELECT count(*) FROM settings")).scalar_one()
            holidays_count = connection.execute(sa.text("SELECT count(*) FROM holidays")).scalar_one()
        assert settings_count == len(SETTINGS_DEFAULTS)
        assert holidays_count > 0

        command.downgrade(config, "0002_totp_pending_secret")
        with engine.connect() as connection:
            settings_after = connection.execute(sa.text("SELECT count(*) FROM settings")).scalar_one()
            holidays_after = connection.execute(sa.text("SELECT count(*) FROM holidays")).scalar_one()
        # The seed's own rows are gone; the tables remain (they belong to 0001).
        assert settings_after == 0
        assert holidays_after == 0
    finally:
        engine.dispose()


def test_downgrade_keeps_a_settings_row_an_admin_changed(
    empty_database: str, make_alembic_config: Callable[[str], Config]
) -> None:
    """A downgrade must not delete operator data — only the untouched defaults it inserted."""
    config = make_alembic_config(empty_database)
    engine = sa.create_engine(empty_database, poolclass=sa.pool.NullPool)
    try:
        command.upgrade(config, "0003_seed_reference_data")
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "UPDATE settings SET value = '600' "
                    "WHERE key = 'overtime_daily_threshold_minutes'"
                )
            )

        command.downgrade(config, "0002_totp_pending_secret")
        with engine.connect() as connection:
            surviving = connection.execute(
                sa.text("SELECT value FROM settings WHERE key = 'overtime_daily_threshold_minutes'")
            ).scalar_one_or_none()
        assert surviving == "600", "a tuned setting must survive a schema downgrade"
    finally:
        engine.dispose()


def test_holiday_years_resolve_from_the_clock() -> None:
    # Unit-level, but kept beside the seed tests: the migration relies on this to seed the right two
    # years whenever it happens to run.
    assert holiday_years(date(2030, 3, 1)) == (2030, 2031)
