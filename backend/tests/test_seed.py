"""Seed unit tests that need no database.

The parts of `app.seed` that are pure — which settings defaults exist, which two years the holiday
seed covers, and the decision to skip the bootstrap admin when no password is configured — are
tested here. The parts that need a real PostgreSQL (the inserts, their idempotency, the admin row)
are in tests/integration/test_seed_reference_data.py, because idempotency is a property of the
database's unique constraints and a fake would assert nothing.
"""

from __future__ import annotations

from datetime import date

from app.core.config import Settings
from app.seed import SETTINGS_DEFAULTS, holiday_years, seed_bootstrap_admin


def test_settings_defaults_cover_the_engine_knobs() -> None:
    keys = {default.key for default in SETTINGS_DEFAULTS}
    # The values the calculation engine and the jobs read on a fresh database. Named explicitly so a
    # dropped default fails here rather than surfacing as a mysterious missing setting later.
    assert {
        "overtime_daily_threshold_minutes",
        "shabbat_start_weekday",
        "shabbat_start_time",
        "shabbat_end_weekday",
        "shabbat_end_time",
        "no_checkout_cutoff_time",
        "implausible_shift_hours",
        "document_expiry_warning_days",
    } <= keys


def test_overtime_threshold_default_is_eight_hours() -> None:
    overtime = next(d for d in SETTINGS_DEFAULTS if d.key == "overtime_daily_threshold_minutes")
    assert overtime.value == "480"
    assert overtime.value_type == "integer"


def test_shabbat_window_default_is_friday_1600_to_saturday_2000() -> None:
    by_key = {d.key: d.value for d in SETTINGS_DEFAULTS}
    # Assumption A7: Friday (weekday 4) 16:00 to Saturday (weekday 5) 20:00.
    assert by_key["shabbat_start_weekday"] == "4"
    assert by_key["shabbat_start_time"] == "16:00"
    assert by_key["shabbat_end_weekday"] == "5"
    assert by_key["shabbat_end_time"] == "20:00"


def test_settings_default_value_types_are_valid_enum_members() -> None:
    valid = {"string", "integer", "decimal", "boolean", "time", "json"}
    assert all(default.value_type in valid for default in SETTINGS_DEFAULTS)


def test_holiday_years_are_the_current_and_next() -> None:
    assert holiday_years(date(2025, 6, 1)) == (2025, 2026)
    # A December date still means "this year and next", not a rollover surprise.
    assert holiday_years(date(2025, 12, 31)) == (2025, 2026)


def test_bootstrap_admin_skipped_when_no_password_configured() -> None:
    # No password → the function returns False before touching the connection, so passing None for
    # the connection proves it never uses one on this path.
    settings = _settings_without_password()
    assert seed_bootstrap_admin(connection=None, settings=settings) is False  # type: ignore[arg-type]


def _settings_without_password() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://x:x@127.0.0.1:1/x",
        redis_url="redis://127.0.0.1:1/0",
        jwt_secret_key="unit-test-jwt-secret-not-a-real-secret-000",
        encryption_key="unit-test-encryption-secret-not-real-0000",
        s3_endpoint_url="http://127.0.0.1:1",
        s3_access_key_id="k",
        s3_secret_access_key="s",
        s3_bucket_documents="b",
        bootstrap_admin_password=None,
    )
