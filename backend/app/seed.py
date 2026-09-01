"""Seed and bootstrap.

A fresh database is migrated and then seeded. Three things are seeded, and they are separated here
because they have different lifetimes and different sources of truth:

* **Settings defaults** — the tuning knobs the calculation engine reads: the daily overtime
  threshold, the Shabbat window, the no-checkout cutoff, the implausible-shift limit and the
  document-warning window. Static, the same for every deployment, safe to bake into a migration.
* **The holiday calendar** — the Israeli national holidays for the current and next year, computed
  from the Hebrew calendar (see `app.core.hebrew_calendar`). Static per year, so the *rule* is safe
  to bake into a migration; the *years* are resolved at run time so a migration applied in 2027
  seeds 2027 and 2028, not the two years that happened to be current when the migration was written.
* **The bootstrap admin** — the first administrator, created from environment variables. This one
  is emphatically *not* baked into a migration: the password is a secret, and a migration is a
  committed, frozen artefact. It is created by the seed CLI reading `BOOTSTRAP_ADMIN_*` from the
  environment, which is where a secrets manager can put it.

Every function here is idempotent. Seeding is a release step that may run more than once — a
re-deploy, a re-run after a half-finished one — and running it twice must leave the same state as
running it once, never a duplicate holiday or a second admin. Idempotency is achieved with
`ON CONFLICT DO NOTHING` against the natural keys the schema already enforces (`settings.key`,
`holidays.date`, `users.username`), so the database, not this code, is the arbiter of "already
there".

The functions take a `Connection` and never commit. The caller owns the transaction: the migration
runs inside Alembic's, and the CLI opens one and commits at the end, so a failure part-way leaves
nothing behind.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.core.config import Settings, get_settings
from app.core.hebrew_calendar import israeli_holidays_for_gregorian_year
from app.core.security import hash_password
from app.models.user import UserRole

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- settings defaults


@dataclass(frozen=True, slots=True)
class SettingDefault:
    """One tuning knob, with the type the typed accessor will read it back through."""

    key: str
    value: str
    value_type: str
    description: str


# The values the calculation engine will read once it exists. Chosen to match the requirements and
# the scope assumptions exactly:
#   * overtime after 8 hours a day → 480 minutes (Requirement 10, design "default 480 minutes"),
#   * Shabbat Friday 16:00 → Saturday 20:00 local (assumption A7),
#   * document warnings 30 days ahead (Requirement 4.4).
# The no-checkout cutoff and the implausible-shift limit are operational thresholds the jobs and the
# check-out path read; their defaults are stated here so the engine has something to read on a fresh
# database and an administrator can tune them later through the settings screen.
SETTINGS_DEFAULTS: tuple[SettingDefault, ...] = (
    SettingDefault(
        key="overtime_daily_threshold_minutes",
        value="480",
        value_type="integer",
        description="Minutes worked in a day before further minutes are overtime (8h).",
    ),
    SettingDefault(
        key="shabbat_start_weekday",
        value="4",
        value_type="integer",
        description="Weekday the Shabbat window opens on, Monday=0; Friday=4 (assumption A7).",
    ),
    SettingDefault(
        key="shabbat_start_time",
        value="16:00",
        value_type="time",
        description="Local time the Shabbat premium window opens on Friday (assumption A7).",
    ),
    SettingDefault(
        key="shabbat_end_weekday",
        value="5",
        value_type="integer",
        description="Weekday the Shabbat window closes on, Monday=0; Saturday=5 (assumption A7).",
    ),
    SettingDefault(
        key="shabbat_end_time",
        value="20:00",
        value_type="time",
        description="Local time the Shabbat premium window closes on Saturday (assumption A7).",
    ),
    SettingDefault(
        key="no_checkout_cutoff_time",
        value="23:59",
        value_type="time",
        description="Local time the daily no-checkout reminder is sent to employees still open.",
    ),
    SettingDefault(
        key="implausible_shift_hours",
        value="16",
        value_type="integer",
        description="A shift at least this long is flagged for review rather than accepted silently.",
    ),
    SettingDefault(
        key="max_open_shift_hours",
        value="16",
        value_type="integer",
        description="An open shift older than this alerts the site manager and is flagged as an "
        "anomaly (Requirement 14.2, default 16h).",
    ),
    SettingDefault(
        key="duplicate_scan_window_seconds",
        value="60",
        value_type="integer",
        description="Seconds after a successful scan that an identical repeat is treated as a "
        "duplicate submission and ignored (Requirement 9.3).",
    ),
    SettingDefault(
        key="document_expiry_warning_days",
        value="30",
        value_type="integer",
        description="Days before a document's expiry that a warning is raised (Requirement 4.4).",
    ),
)


def seed_settings(connection: Connection) -> int:
    """Insert any missing settings default. Returns how many rows were newly inserted.

    Existing keys are left untouched: an administrator may have tuned a value, and a re-run of the
    seed must not stamp their change back to the default. That is exactly `ON CONFLICT (key) DO
    NOTHING`, which also makes the operation safe to run against a database that already has some
    but not all of the keys.
    """
    inserted = 0
    statement = sa.text(
        """
        INSERT INTO settings (key, value, value_type, description)
        VALUES (:key, :value, CAST(:value_type AS setting_value_type), :description)
        ON CONFLICT (key) DO NOTHING
        """
    )
    for default in SETTINGS_DEFAULTS:
        result = connection.execute(
            statement,
            {
                "key": default.key,
                "value": default.value,
                "value_type": default.value_type,
                "description": default.description,
            },
        )
        inserted += result.rowcount or 0
    return inserted


# --------------------------------------------------------------------------- holiday calendar


def holiday_years(today: date | None = None) -> tuple[int, int]:
    """The two Gregorian years the calendar is seeded for: the current one and the next.

    Resolved from the clock at run time, deliberately, so the same migration seeds the right two
    years whenever it is applied. `today` is injectable for tests.
    """
    year = (today or date.today()).year
    return year, year + 1


def seed_holidays(connection: Connection, *, today: date | None = None) -> int:
    """Insert the Israeli national holidays for the current and next year. Returns rows inserted.

    Idempotent on `holidays.date`, the column the schema already makes unique. A holiday whose date
    is already present — because last year's seed run reached into this year, or because the seed
    ran twice — is left alone.
    """
    inserted = 0
    statement = sa.text(
        """
        INSERT INTO holidays (date, name_he, name_en, is_full_day)
        VALUES (:date, :name_he, :name_en, :is_full_day)
        ON CONFLICT (date) DO NOTHING
        """
    )
    for year in holiday_years(today):
        for holiday in israeli_holidays_for_gregorian_year(year):
            result = connection.execute(
                statement,
                {
                    "date": holiday.date,
                    "name_he": holiday.name_he,
                    "name_en": holiday.name_en,
                    "is_full_day": holiday.is_full_day,
                },
            )
            inserted += result.rowcount or 0
    return inserted


# --------------------------------------------------------------------------- bootstrap admin


def seed_bootstrap_admin(connection: Connection, *, settings: Settings | None = None) -> bool:
    """Create the first administrator from the environment, if configured and not already present.

    Returns True when an admin row was created, False when creation was skipped. Skips silently
    when no password is configured — a deployment that already has its admin should not be forced to
    re-supply the secret on every seed run — and skips when the username already exists, so a re-run
    never mints a second account or overwrites the password of a working one.

    The password is hashed with the same bcrypt cost the login path uses; nothing but the hash ever
    reaches the database. 2FA is left un-enrolled: the admin role is *required* to enrol
    (`is_2fa_enrolment_required`), so the first sign-in is sent straight to enrolment, which is the
    correct place to bind a second factor rather than seeding one nobody has scanned.
    """
    settings = settings or get_settings()
    if settings.bootstrap_admin_password is None:
        logger.info("bootstrap admin skipped: BOOTSTRAP_ADMIN_PASSWORD is not set")
        return False

    username = settings.bootstrap_admin_username
    existing = connection.execute(
        sa.text("SELECT 1 FROM users WHERE username = :username"), {"username": username}
    ).first()
    if existing is not None:
        logger.info("bootstrap admin skipped: user %r already exists", username)
        return False

    connection.execute(
        sa.text(
            """
            INSERT INTO users (id, username, password_hash, role, is_active, language, token_version)
            VALUES (
                :id, :username, :password_hash, CAST(:role AS user_role), true,
                CAST(:language AS app_language), 1
            )
            """
        ),
        {
            "id": uuid.uuid4(),
            "username": username,
            "password_hash": hash_password(settings.bootstrap_admin_password.get_secret_value()),
            "role": UserRole.ADMIN.value,
            "language": settings.bootstrap_admin_language,
        },
    )
    logger.info("bootstrap admin created: user %r (enrol 2FA on first sign-in)", username)
    return True


# --------------------------------------------------------------------------- reference-data seed


def seed_reference_data(connection: Connection, *, today: date | None = None) -> tuple[int, int]:
    """Seed the static reference data a migration is responsible for: settings and holidays.

    Kept separate from the admin so the Alembic data migration can call exactly this — reference
    data belongs in a migration, a secret-bearing credential does not.
    """
    settings_inserted = seed_settings(connection)
    holidays_inserted = seed_holidays(connection, today=today)
    return settings_inserted, holidays_inserted


# --------------------------------------------------------------------------- command-line entry


def run_seed(*, today: date | None = None) -> None:
    """Seed reference data and the bootstrap admin against the configured database, in one transaction.

    This is the release-step entry point (`python -m app.seed`). It opens one transaction so a
    failure part-way leaves the database exactly as it was, and it logs a one-line summary of what it
    did so a deploy log records whether the admin was created or found already present.

    Reference data is also seeded by the Alembic data migration; running it again here is harmless
    because every write is idempotent, and doing so means `python -m app.seed` on its own is enough
    to bring a freshly migrated database to a usable state without the operator remembering a second
    command.
    """
    from app.db.session import get_engine

    with get_engine().begin() as connection:
        settings_inserted, holidays_inserted = seed_reference_data(connection, today=today)
        admin_created = seed_bootstrap_admin(connection)

    logger.info(
        "seed complete: %d settings inserted, %d holidays inserted, admin %s",
        settings_inserted,
        holidays_inserted,
        "created" if admin_created else "unchanged",
    )


def _main() -> None:
    logging.basicConfig(level=get_settings().log_level, format="%(levelname)s %(name)s: %(message)s")
    run_seed()


if __name__ == "__main__":
    _main()
