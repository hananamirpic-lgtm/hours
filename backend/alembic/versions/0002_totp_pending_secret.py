"""Hold an unproven TOTP secret separately from the proven one.

Enrolment (Requirement 1.5) has two steps: a secret is issued so the user can scan it, and then a
code is submitted to prove the authenticator holds it. Between those two steps the secret is a
guess — nobody has yet shown that anything can generate codes from it.

Migration 0001 gave `users` a single `totp_secret_encrypted`, which is enough for a first enrolment
but not for a second one. Overwriting that column for a user who already has 2FA enabled destroys
the working secret at the moment the new one is issued, so an enrolment abandoned halfway — the tab
closed before the QR code was scanned — locks the user out of their own account. The account has no
credential that works and no way to reach the endpoint that would fix it.

This column is the fix: `totp_secret_encrypted` only ever holds a secret that has been proven, and
`totp_pending_secret_encrypted` holds the one waiting to be. Login reads the former, so the old
authenticator keeps working right up until the new one is demonstrated, and an abandoned enrolment
changes nothing.

Encrypted at rest like its neighbour (Requirement 20.2): a pending secret is a credential in waiting
and gets the same treatment as one in use. Nullable and with no default, so it is empty except during
the seconds an enrolment is in flight.

The table's privileges are unchanged — the grants migration 0001 applied are table-level, so they
already cover a column added later.

Revision ID: 0002_totp_pending_secret
Revises: 0001_initial_schema
Create Date: 2025-01-02 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_totp_pending_secret"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_pending_secret_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    # Dropping this discards any enrolment in flight, which is the correct loss: an unproven secret
    # is not a credential, and the user simply starts enrolment again.
    op.drop_column("users", "totp_pending_secret_encrypted")
