"""Add the `operations_admin` value to the `user_role` enum.

`operations_admin` is a full operational administrator with no financial visibility (see
`app.core.authz`). It needs to be a legal value of the existing `user_role` PostgreSQL enum so a
`users.role` column can hold it.

Adding an enum value is the whole migration — no table, column, constraint or grant changes. Two
PostgreSQL facts shape how it is done:

* `ALTER TYPE ... ADD VALUE` historically could not run inside a transaction block. Modern PostgreSQL
  (12+) allows it, and `IF NOT EXISTS` makes the statement idempotent, but to be safe across versions
  the statement is issued on a connection whose transaction is committed first (autocommit), so a
  re-run or a mid-migration failure cannot leave a half-open transaction.
* Removing an enum value is not supported without rebuilding the type and rewriting every dependent
  column, which is unsafe once any row uses the value. So `downgrade` is a deliberate no-op: it leaves
  the value in place. This mirrors how enum additions are handled elsewhere in this project.

Revision ID: 0010_operations_admin_role
Revises: 0009_drop_site_end_date
Create Date: 2025-01-10 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "0010_operations_admin_role"
down_revision = "0009_drop_site_end_date"
branch_labels = None
depends_on = None

_ENUM = "user_role"
_VALUE = "operations_admin"


def upgrade() -> None:
    # Commit any open transaction, then add the value on an autocommit connection: ADD VALUE cannot be
    # rolled back and, on older PostgreSQL, cannot run inside a transaction block. IF NOT EXISTS makes
    # the statement safe to re-run.
    connection = op.get_bind()
    connection.execute(sa_text_commit())
    connection.exec_driver_sql(f"ALTER TYPE {_ENUM} ADD VALUE IF NOT EXISTS '{_VALUE}'")


def downgrade() -> None:
    # Intentional no-op: PostgreSQL cannot drop an enum value without rebuilding the type and every
    # dependent column, which is unsafe once a row may hold the value. The value is left in place.
    pass


def sa_text_commit():  # noqa: ANN201 - tiny local helper, keeps the import surface minimal
    import sqlalchemy as sa

    return sa.text("COMMIT")