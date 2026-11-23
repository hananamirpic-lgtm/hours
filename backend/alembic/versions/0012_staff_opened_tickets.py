"""The username of the staff member who opened a ticket on a customer's behalf.

Adds one nullable column, `svc_tickets.opened_by_username text` (Requirement 26.4), and nothing else.
A ticket the customer opened keeps NULL there; a ticket opened by staff holds the staff username. No
existing row changes: every ticket already stored was opened by its customer. The column has no
default, no check and no index, and the application never serializes it to a customer.

This migration runs on the live database when the release is deployed, so it is purely additive and
idempotent, like `0011_service_tickets`:

* The only object it touches is `svc_tickets`, and only by adding a column. It touches no Hours table,
  no enum, no sequence and no grant: the table-level grants of `0011` already cover a new column.
* The column is added with `IF NOT EXISTS`. A database restored from a dump taken after this migration
  ran, with an older `alembic_version`, therefore upgrades cleanly.
* It reads no application setting, so it runs whatever the value of `SERVICE_TICKETS_ENABLED` is, and
  it makes no object-store call.

The back-out target that removes the whole feature stays `0010_operations_admin_role`: a downgrade to
it runs this migration's downgrade and then `0011`'s.

Downgrade drops the column and the staff usernames in it. It keeps every ticket, every customer and
every other column. The drop uses `IF EXISTS` on the table and on the column, so it never fails on a
restored database that lacks either.

Revision ID: 0012_staff_opened_tickets
Revises: 0011_service_tickets
Create Date: 2025-01-12 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "0012_staff_opened_tickets"
down_revision = "0011_service_tickets"
branch_labels = None
depends_on = None

# Repeated as literals rather than imported, like migrations 0001, 0004, 0007, 0010 and 0011.
_TABLE = "svc_tickets"
_COLUMN = "opened_by_username"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} text")


def downgrade() -> None:
    op.execute(f"ALTER TABLE IF EXISTS {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
