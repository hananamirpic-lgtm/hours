"""The settlement and replaceable-part lists, the branding row, the part links and `locality_id`.

Adds the data model of the second round of change requests (Requirements 28 to 30): four `svc_`
tables and one nullable `locality_id` column on each of two existing `svc_` tables.

* `svc_localities`: the settlement list, shaped exactly like `svc_categories` (identity `seq`, Hebrew and
  English names with their casefolded `*_key` columns, `is_active`, timestamps), names of 1 to 100
  characters. No seed rows: the list starts empty and the portal keeps the free-text city field until
  Config_Staff add a settlement (Requirement 30.3).
* `svc_parts`: the replaceable-part list, shaped like `svc_categories`, names of 1 to 60 characters. No
  seed rows.
* `svc_ticket_parts`: the parts replaced on a ticket. `ticket_id` references `svc_tickets.id` with
  `ON DELETE CASCADE`, `part_id` references `svc_parts.id` with `ON DELETE RESTRICT`, the primary key is
  `(ticket_id, part_id)` and `part_id` is indexed for the report filter.
* `svc_branding`: a single row (`id smallint PRIMARY KEY CHECK (id = 1)`) holding the optional company
  name and the object-store key of the optional logo. This migration inserts the empty row.
* `svc_tickets.locality_id` and `svc_customers.locality_id`: nullable uuid columns, each a foreign key to
  `svc_localities.id` with `ON DELETE RESTRICT` and an index. Every row that exists keeps NULL there and
  keeps the city text it has (Requirement 30.4).

This migration runs on the live database when the release is deployed, so it is purely additive and
idempotent, like `0011_service_tickets` and `0012_staff_opened_tickets`:

* It touches no Hours table, no enum and no existing sequence. The only existing objects it changes are
  the two `svc_` tables that gain a column.
* Every table is created only when it does not already exist, and nothing is seeded except the empty
  branding row, which is inserted with `ON CONFLICT DO NOTHING`: a re-run never resets a company name or
  a logo that Config_Staff have set (the same rule as Requirement 19.17 for the other seeds).
* Each column is added with `ADD COLUMN IF NOT EXISTS`, the foreign key inside that same clause, and each
  index with `CREATE INDEX IF NOT EXISTS`. A database restored from a dump taken after this migration
  ran, with an older `alembic_version`, therefore upgrades cleanly.
* It reads no application setting, so it runs whatever the value of `SERVICE_TICKETS_ENABLED` is, and it
  makes no object-store call.

Constraint and index names are written out in full and passed through `op.f()`, so they equal what
`app.db.base.NAMING_CONVENTION` produces for the models in `app.service_tickets.models`, and an
autogenerate run sees no drift.

The application role gets the same table-level grants `0011` gives the other `svc_` tables
(`SELECT, INSERT, UPDATE, DELETE`), and `USAGE, SELECT` on the two identity sequences behind the new
`seq` columns. A column added to an existing table is covered by the table-level grants of `0011`. The
role `hours_app` is created by `0001`.

The back-out target that removes the whole feature stays `0010_operations_admin_role`: a downgrade to it
runs this migration's downgrade, then `0012`'s, then `0011`'s.

Downgrade drops the two `locality_id` columns first (and with them their foreign keys and indexes), then
the four tables in reverse dependency order, using `IF EXISTS` throughout. It keeps every ticket and
every customer, with the city text they hold, and deletes the settlement and part lists, the part links
and the branding row. It makes no object-store call, so a logo object stays in the bucket until the
orphan sweep of a later release reports it.

Revision ID: 0013_service_lists_branding
Revises: 0012_staff_opened_tickets
Create Date: 2025-01-13 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013_service_lists_branding"
down_revision = "0012_staff_opened_tickets"
branch_labels = None
depends_on = None

# Repeated as literals rather than imported, like migrations 0001, 0004, 0007, 0010, 0011 and 0012.
APPLICATION_ROLE = "hours_app"
# The names PostgreSQL gives the sequences behind the two identity columns.
_IDENTITY_SEQUENCES = ("svc_localities_seq_seq", "svc_parts_seq_seq")

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.TIMESTAMP(timezone=True)

# Dependency order: a table follows the tables it references.
_TABLES = ("svc_localities", "svc_parts", "svc_branding", "svc_ticket_parts")

# The two existing tables that gain `locality_id`.
_LOCALITY_COLUMN_TABLES = ("svc_tickets", "svc_customers")


def upgrade() -> None:
    connection = op.get_bind()

    # Decide once, before creating anything, which tables this run creates.
    inspector = sa.inspect(connection)
    missing = {name for name in _TABLES if not inspector.has_table(name)}

    if "svc_localities" in missing:
        _create_name_list("svc_localities", max_length=100)
    if "svc_parts" in missing:
        _create_name_list("svc_parts", max_length=60)
    if "svc_branding" in missing:
        _create_branding()
    if "svc_ticket_parts" in missing:
        _create_ticket_parts()

    # The column, its foreign key and its index. `ADD COLUMN IF NOT EXISTS` skips the whole clause,
    # constraint included, when the column is already there.
    for table in _LOCALITY_COLUMN_TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS locality_id uuid "
            f"CONSTRAINT fk_{table}_locality_id_svc_localities "
            "REFERENCES svc_localities (id) ON DELETE RESTRICT"
        )
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_locality_id ON {table} (locality_id)")

    # The one empty branding row. A re-run never overwrites a row that is already there.
    op.execute("INSERT INTO svc_branding (id) VALUES (1) ON CONFLICT DO NOTHING")

    # Grants are idempotent, so they cover tables created now and tables that already existed.
    for name in _TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {name} TO {APPLICATION_ROLE}")
    for sequence in _IDENTITY_SEQUENCES:
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {sequence} TO {APPLICATION_ROLE}")


def downgrade() -> None:
    # Columns first: they reference `svc_localities`. Dropping a column drops its foreign key and index.
    for table in _LOCALITY_COLUMN_TABLES:
        op.execute(f"ALTER TABLE IF EXISTS {table} DROP COLUMN IF EXISTS locality_id")
    # Reverse dependency order. The identity sequences go with their tables. No object-store call.
    for name in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {name}")


def _create_name_list(table: str, *, max_length: int) -> None:
    """A table shaped exactly like `svc_categories`, with the given name length bound."""
    op.create_table(
        table,
        sa.Column("id", UUID, nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name_he", sa.Text(), nullable=False),
        sa.Column("name_en", sa.Text(), nullable=False),
        sa.Column("name_he_key", sa.Text(), nullable=False),
        sa.Column("name_en_key", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name=op.f(f"{table}_pkey")),
        sa.UniqueConstraint("seq", name=op.f(f"uq_{table}_seq")),
        sa.UniqueConstraint("name_he_key", name=op.f(f"uq_{table}_name_he_key")),
        sa.UniqueConstraint("name_en_key", name=op.f(f"uq_{table}_name_en_key")),
        sa.CheckConstraint(
            f"length(name_he) BETWEEN 1 AND {max_length}", name=op.f(f"ck_{table}_name_he_length")
        ),
        sa.CheckConstraint(
            f"length(name_en) BETWEEN 1 AND {max_length}", name=op.f(f"ck_{table}_name_en_length")
        ),
    )


def _create_branding() -> None:
    op.create_table(
        "svc_branding",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("company_name", sa.Text(), nullable=True),
        sa.Column("logo_key", sa.Text(), nullable=True),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name=op.f("svc_branding_pkey")),
        sa.UniqueConstraint("logo_key", name=op.f("uq_svc_branding_logo_key")),
        sa.CheckConstraint("id = 1", name=op.f("ck_svc_branding_id_is_one")),
        sa.CheckConstraint(
            "company_name IS NULL OR length(company_name) BETWEEN 1 AND 100",
            name=op.f("ck_svc_branding_company_name_length"),
        ),
    )


def _create_ticket_parts() -> None:
    op.create_table(
        "svc_ticket_parts",
        sa.Column("ticket_id", UUID, nullable=False),
        sa.Column("part_id", UUID, nullable=False),
        sa.PrimaryKeyConstraint("ticket_id", "part_id", name=op.f("svc_ticket_parts_pkey")),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["svc_tickets.id"],
            name=op.f("fk_svc_ticket_parts_ticket_id_svc_tickets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["part_id"],
            ["svc_parts.id"],
            name=op.f("fk_svc_ticket_parts_part_id_svc_parts"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(op.f("ix_svc_ticket_parts_part_id"), "svc_ticket_parts", ["part_id"])
