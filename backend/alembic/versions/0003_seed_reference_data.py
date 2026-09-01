"""Seed the reference data a fresh database needs before it is usable.

Two kinds of data are seeded here, both of which a migration is the right home for because both are
static facts rather than a deployment's own content:

* the **settings defaults** the calculation engine reads — the overtime threshold, the Shabbat
  window, the no-checkout cutoff, the implausible-shift limit and the document-warning window;
* the **Israeli national holiday calendar** for the current and next year, so a shift on a holiday
  is paid at the holiday rate from day one.

The bootstrap admin is *not* seeded here, on purpose. Its password is a secret and a migration is a
committed, frozen artefact; a credential must never live in one. It is created by `python -m
app.seed`, which reads `BOOTSTRAP_ADMIN_*` from the environment.

The upgrade delegates to `app.seed`, which the seed CLI also calls, so there is one definition of
what the defaults are and one definition of idempotency. Every write is `ON CONFLICT DO NOTHING`, so
this migration is safe on a database an operator has already partly seeded, and the holiday years are
resolved from the clock at apply time — a database migrated in 2027 is seeded with 2027 and 2028,
not with whatever two years were current when this file was written.

The downgrade removes only the rows this migration is responsible for, matched by their natural keys,
and it will not delete a setting an administrator has since changed away from its default: that would
be destroying operator data on a schema rollback, which a downgrade must not do.

Revision ID: 0003_seed_reference_data
Revises: 0002_totp_pending_secret
Create Date: 2025-01-03 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.seed import SETTINGS_DEFAULTS, seed_reference_data

revision = "0003_seed_reference_data"
down_revision = "0002_totp_pending_secret"
branch_labels = None
depends_on = None


def upgrade() -> None:
    seed_reference_data(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()

    # Remove only the holidays this seed inserts. The calendar generator is the authority on which
    # dates those are, so a downgrade that re-derives them cannot fall out of step with the upgrade.
    # Deferred import keeps the seed's dependencies out of the module's import-time surface.
    from app.core.hebrew_calendar import israeli_holidays_for_gregorian_year
    from app.seed import holiday_years

    holiday_dates = [
        holiday.date
        for year in holiday_years()
        for holiday in israeli_holidays_for_gregorian_year(year)
    ]
    if holiday_dates:
        connection.execute(
            sa.text("DELETE FROM holidays WHERE date = ANY(:dates)"), {"dates": holiday_dates}
        )

    # Remove a settings default only where it still holds its seeded value. A row an administrator
    # has tuned is their data now, not this migration's, and a downgrade must leave it be.
    statement = sa.text("DELETE FROM settings WHERE key = :key AND value = :value")
    for default in SETTINGS_DEFAULTS:
        connection.execute(statement, {"key": default.key, "value": default.value})
