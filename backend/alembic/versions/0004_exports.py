"""The `exports` table — record every requested Excel/PDF export and its outcome (Requirement 19).

An export is a state machine, not a file (see `app.models.export`): a request creates a row, the
render fills in the stored file or marks the row failed, and a large report is rendered in the
background so the row starts `pending` and a notification follows when it is ready (Requirement 19.6).
This migration creates the table that durable record lives in, plus the three enums it uses.

The rendered bytes are not stored here — they go to the same private object storage the employee
documents use, under `file_key` — so this table stays small and holds only the stamp fields every
export carries (Requirement 19.4): the report type, the format, the period, the filters and who
requested it.

The application role gets the same SELECT/INSERT/UPDATE/DELETE the other business tables get. The
grant in migration 0001 was `ON ALL TABLES`, which is a one-time snapshot and does not reach a table
created later, so it is re-granted here for this one table. `change_logs` is the only table whose
privileges are deliberately narrower, and this migration does not touch it.

Revision ID: 0004_exports
Revises: 0003_seed_reference_data
Create Date: 2025-01-04 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_exports"
down_revision = "0003_seed_reference_data"
branch_labels = None
depends_on = None

# Repeated as a literal rather than imported, like migration 0001: a migration is a frozen record and
# must not change meaning when a constant is renamed.
APPLICATION_ROLE = "hours_app"

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.TIMESTAMP(timezone=True)

# `create_type=False`: the types are created explicitly at the top of upgrade(), matching the pattern
# migration 0001 uses so a shared type is never created twice.
export_format = postgresql.ENUM("xlsx", "pdf", name="export_format", create_type=False)
export_report_type = postgresql.ENUM(
    "by_employee",
    "by_site",
    "by_client",
    "profitability",
    "payment_request",
    name="export_report_type",
    create_type=False,
)
export_status = postgresql.ENUM("pending", "ready", "failed", name="export_status", create_type=False)

ENUM_TYPES = (export_format, export_report_type, export_status)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in ENUM_TYPES:
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "exports",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "requested_by_user_id",
            UUID,
            sa.ForeignKey("users.id", name="fk_exports_requested_by_user_id_users"),
            nullable=False,
        ),
        sa.Column("report_type", export_report_type, nullable=False),
        sa.Column("format", export_format, nullable=False),
        sa.Column("status", export_status, nullable=False, server_default="pending"),
        sa.Column("language", sa.Text(), nullable=False, server_default="he"),
        sa.Column("period_year", sa.Integer(), nullable=False),
        sa.Column("period_month", sa.Integer(), nullable=False),
        sa.Column("filters", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("file_key", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("completed_at", TIMESTAMPTZ, nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("period_month BETWEEN 1 AND 12", name="period_month_range"),
    )
    op.create_index(
        "ix_exports_requested_by_user_id_created_at",
        "exports",
        ["requested_by_user_id", "created_at"],
    )

    # Re-grant for this table: migration 0001's ALL TABLES grant was a snapshot and does not reach a
    # table created afterwards. The default privileges are the standard business-table set.
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON exports TO {APPLICATION_ROLE}"
    )


def downgrade() -> None:
    op.drop_index("ix_exports_requested_by_user_id_created_at", table_name="exports")
    op.drop_table("exports")
    bind = op.get_bind()
    for enum_type in ENUM_TYPES:
        enum_type.drop(bind, checkfirst=True)
