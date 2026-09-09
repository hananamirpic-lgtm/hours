"""The `staffing_companies` table and the employee link.

Adds the Staffing Company feature's data model: a `staffing_companies` table (an external labour
provider — name and contact person mandatory, an optional flat `hourly_rate`, telephone and comments),
and a nullable `staffing_company_id` foreign key on `employees` pointing at it.

The column is nullable on purpose. Every *new* employee must select a staffing company, but that rule
is enforced on the create path (in `EmployeeCreate`), not by a NOT NULL constraint — an employee row
created before this feature has no company, and a NOT NULL column would make this migration fail on
any database that already holds employees. So the migration adds the column nullable and leaves every
existing employee's `staffing_company_id` null (Requirement 2.9-2.11, 6.3).

The application role is granted the standard business-table privileges on the new table. Migration
0001's grant was `ON ALL TABLES`, a one-time snapshot that does not reach a table created afterwards —
exactly the situation migration 0004 documents for the `exports` table — so `staffing_companies` is
re-granted here. `employees` already carries its grant from 0001, and adding a column to it needs no
new grant, so only the new table is granted.

Revision ID: 0007_staffing_company
Revises: 0006_employee_number
Create Date: 2025-01-07 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007_staffing_company"
down_revision = "0006_employee_number"
branch_labels = None
depends_on = None

# Repeated as a literal rather than imported, like migrations 0001 and 0004: a migration is a frozen
# record and must not change meaning when a constant is renamed.
APPLICATION_ROLE = "hours_app"

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "staffing_companies",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("contact_person", sa.Text(), nullable=False),
        sa.Column("hourly_rate", sa.Numeric(12, 2), nullable=True),
        sa.Column("telephone", sa.Text(), nullable=True),
        sa.Column("comments", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
    )

    # The employee link. Nullable so employees created before this feature stay valid and are left
    # null; the "new employee must select a company" rule lives on the create path, not here.
    op.add_column("employees", sa.Column("staffing_company_id", UUID, nullable=True))
    op.create_foreign_key(
        "fk_employees_staffing_company_id",
        "employees",
        "staffing_companies",
        ["staffing_company_id"],
        ["id"],
    )
    op.create_index("ix_employees_staffing_company_id", "employees", ["staffing_company_id"])

    # Re-grant for this table: migration 0001's ALL TABLES grant was a snapshot and does not reach a
    # table created afterwards. The default privileges are the standard business-table set.
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON staffing_companies TO {APPLICATION_ROLE}"
    )


def downgrade() -> None:
    # Reverse in dependency order: the index and foreign key and column on `employees` first, then the
    # table they referenced.
    op.drop_index("ix_employees_staffing_company_id", table_name="employees")
    op.drop_constraint("fk_employees_staffing_company_id", "employees", type_="foreignkey")
    op.drop_column("employees", "staffing_company_id")
    op.drop_table("staffing_companies")