"""Seed the `public_app_url` setting.

The base URL a site's QR code points at. A generated QR now encodes `<public_app_url>/m/scan?token=...`
rather than the bare token, so scanning it with any phone camera opens the employee portal. The value
is a tuning knob like the others in the `settings` table, but it starts empty: "not configured" is the
correct initial state, so QR generation refuses with a clear error until an administrator sets a real
URL from the settings screen. The application-layer validation (in `app.services.settings`) enforces
that a stored value is an absolute http/https URL.

This is a data migration only: it inserts one row idempotently. The row is deliberately NOT added to
`app.seed.SETTINGS_DEFAULTS`, because that tuple is consumed by migration 0003's seed and downgrade;
seeding here keeps this setting's lifecycle self-contained. The `settings` table was already granted to
the application role by migration 0001, so no new grant is required, and no table is created.

Revision ID: 0008_public_app_url
Revises: 0007_staffing_company
Create Date: 2025-01-08 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008_public_app_url"
down_revision = "0007_staffing_company"
branch_labels = None
depends_on = None

# Repeated as literals rather than imported, like the other migrations: a migration is a frozen record
# and must not change meaning when a constant is renamed elsewhere.
_KEY = "public_app_url"
_DEFAULT = ""
_DESCRIPTION = "Base URL that site QR codes point to, e.g. https://hours.example.com."


def upgrade() -> None:
    # Idempotent insert, matching the seed pattern in migration 0003: safe on a database an operator
    # has already partly seeded. The empty default makes "not configured" the initial state.
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO settings (key, value, value_type, description)
            VALUES (:key, :value, CAST(:value_type AS setting_value_type), :description)
            ON CONFLICT (key) DO NOTHING
            """
        ),
        {"key": _KEY, "value": _DEFAULT, "value_type": "string", "description": _DESCRIPTION},
    )


def downgrade() -> None:
    # Remove the row only where it still holds its seeded (empty) value. A URL an administrator has set
    # is their data now, not this migration's, so a downgrade must leave a configured value be —
    # mirroring how migration 0003 downgrades its settings defaults.
    op.get_bind().execute(
        sa.text("DELETE FROM settings WHERE key = :key AND value = :value"),
        {"key": _KEY, "value": _DEFAULT},
    )