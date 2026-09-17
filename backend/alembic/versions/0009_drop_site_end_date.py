"""Drop the `sites.end_date` column and its `dates_ordered` check.

A site's end date was captured on the site card but never read by any calculation, report or scan
path — the effective-dated `site_rates` history and the `status` enum carry a site's lifecycle, not a
single end date. It is removed at the administrator's request to simplify the site form. The
`dates_ordered` check constraint (`end_date >= start_date`) only existed to relate the two dates, so
it goes with the column; `start_date` stays.

This drops a column, so it is not perfectly reversible: `downgrade` re-creates the column and the
check but cannot restore any end dates that were stored, which is acceptable because nothing consumed
them. On PostgreSQL the check is dropped explicitly first; on the SQLite unit-test engine the schema
is built from the models, so this migration is exercised only against PostgreSQL.

Revision ID: 0009_drop_site_end_date
Revises: 0008_public_app_url
Create Date: 2025-01-09 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009_drop_site_end_date"
down_revision = "0008_public_app_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the check first: it references the column, so the column cannot go while it stands.
    op.drop_constraint("dates_ordered", "sites", type_="check")
    op.drop_column("sites", "end_date")


def downgrade() -> None:
    # Re-create the column and the check exactly as migration 0001 wrote them. Any end dates that were
    # stored before the upgrade are gone; nothing read them, so no data is meaningfully lost.
    op.add_column("sites", sa.Column("end_date", sa.Date(), nullable=True))
    op.create_check_constraint(
        "dates_ordered",
        "sites",
        "end_date IS NULL OR start_date IS NULL OR end_date >= start_date",
    )