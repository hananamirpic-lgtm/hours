"""Force a first-use password change for non-administrator logins.

A login created by an administrator carries a password the administrator chose, so the person who
owns the account has never picked their own. This migration adds the obligation that closes that
gap: `must_change_password`, enforced exactly like the mandatory-2FA gate — a non-admin who owes a
change is answered 403 on every endpoint outside `/auth` until they set a new password (see
`app.api.deps.get_enrolled_user` and `app.services.auth.change_password`).

The column defaults to false, so a login that never sets it — the bootstrap admin, and any future
writer that does not name it — is not forced. The backfill then flips it true for every *existing*
non-admin already in the table, so the obligation reaches the accounts that predate it and not only
the ones created afterwards; administrators are left alone, matching the role exemption the gate and
the create path both apply.

No grant is added: `users` was covered by migration 0001's `GRANT ... ON ALL TABLES`, and this only
adds a column to a table the application role can already write.

Revision ID: 0005_must_change_password
Revises: 0004_exports
Create Date: 2025-01-05 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_must_change_password"
down_revision = "0004_exports"
branch_labels = None
depends_on = None

# Repeated as a literal rather than imported, like migrations 0001 and 0004: a migration is a frozen
# record and must not change meaning when a constant is renamed.
ADMIN_ROLE = "admin"


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Backfill: force every existing non-administrator to change their password on their next use, so
    # the obligation reaches accounts that predate it. Administrators are exempt, matching the create
    # path and the gate. The column default already leaves new rows false unless a writer sets it.
    op.execute(f"UPDATE users SET must_change_password = true WHERE role <> '{ADMIN_ROLE}'")


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
