"""The service-ticket tables, the ticket-number sequence and the `technician` role value.

Adds the Service Tickets feature's data model: seven `svc_` tables, one sequence and one value of the
`user_role` enum. This migration runs on the live database when the release is deployed, so it is
purely additive and idempotent:

* The only existing object it touches is the `user_role` enum, and it only adds a value.
* Every table is created only when it does not already exist. A database that kept the `svc_` tables
  after a restore (a dump taken before this migration, with the old `alembic_version`) therefore
  upgrades cleanly.
* The seed rows (the six default categories, the seven field labels and the single settings row) are
  inserted only into a table that this same run created. A re-run against existing tables never adds
  back a deleted category, renames one, reactivates one, or changes a label or a setting
  (Requirement 19.17).
* It reads no application setting, so it runs whatever the value of `SERVICE_TICKETS_ENABLED` is
  (Requirement 21.11), and it makes no object-store call.

The enum value is added first, on a committed connection, exactly as `0010_operations_admin_role`
does: `ALTER TYPE ... ADD VALUE` cannot be rolled back and, before PostgreSQL 12, cannot run inside a
transaction block. A value added in a transaction cannot be used until that transaction commits; this
migration never inserts a row with the new role, so that rule never applies. If a later step fails,
PostgreSQL's transactional DDL rolls back the tables, the sequence and the `alembic_version` update
together, and the only residue is the enum value, which is harmless. A re-run finds the value present
and carries on.

Constraint and index names are written out in full and passed through `op.f()`, so they equal what
`app.db.base.NAMING_CONVENTION` produces for the models in `app.service_tickets.models`, and an
autogenerate run sees no drift.

The application role is granted the standard business-table privileges on every new table, as `0007`
does: `0001`'s `GRANT ... ON ALL TABLES` is a snapshot and does not reach a table created afterwards.
A sequence needs its own grant for `nextval`, so the ticket-number sequence and the two identity
sequences (`svc_categories.seq`, `svc_signin_failures.id`) are granted as well. The role `hours_app` is
created by `0001`.

Seed values, status values, bounds and the role name are repeated here as literals and nothing is
imported from the application: a migration is a frozen record and must not change meaning when an
application constant is renamed.

Downgrade drops the `svc_` tables in reverse dependency order and the sequence. It deletes every
customer and ticket. It leaves the enum value in place (PostgreSQL cannot remove one without
rebuilding the type) and leaves every photo object in the object store untouched. `0001`'s downgrade
drops the `user_role` type itself when the chain is taken back to base.

Revision ID: 0011_service_tickets
Revises: 0010_operations_admin_role
Create Date: 2025-01-11 00:00:00
"""

from __future__ import annotations

import unicodedata

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0011_service_tickets"
down_revision = "0010_operations_admin_role"
branch_labels = None
depends_on = None

# Repeated as literals rather than imported, like migrations 0001, 0004, 0007 and 0010.
APPLICATION_ROLE = "hours_app"
_ENUM = "user_role"
_ENUM_VALUE = "technician"
_TICKET_NUMBER_SEQUENCE = "svc_ticket_number_seq"
# The names PostgreSQL gives the sequences behind the two identity columns.
_IDENTITY_SEQUENCES = ("svc_categories_seq_seq", "svc_signin_failures_id_seq")

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.TIMESTAMP(timezone=True)

# Dependency order: a table follows the tables it references.
_TABLES = (
    "svc_customers",
    "svc_categories",
    "svc_tickets",
    "svc_history_entries",
    "svc_settings",
    "svc_field_labels",
    "svc_signin_failures",
)

# The six default categories, in the order the Hebrew and English display order is built from
# (Requirement 19.14): `seq` is an identity column, so inserting them one by one in this order gives
# them increasing values in this order.
_SEED_CATEGORIES = (
    ("דליפה / סתימה", "Leak or Blockage"),
    ("רטיבות / לחות", "Dampness or Moisture"),
    ("חשמל", "Electrical"),
    ("אינסטלציה", "Plumbing"),
    ("קירות וגמרים", "Walls and Finishing"),
    ("כללי", "General"),
)

# The seven configurable field labels (Requirement 19.15): (field_key, label_he, label_en).
_SEED_FIELD_LABELS = (
    ("city", "ישוב", "Settlement"),
    ("apartment", "מספר דירה", "Apartment number"),
    ("phone", "מספר טלפון", "Phone number"),
    ("category", "קטגוריית תקלה", "Issue category"),
    ("description", "תיאור התקלה", "Issue description"),
    ("photo", "תמונה", "Photo"),
    ("urgent", "דחוף", "Urgent"),
)


def _key(value: str) -> str:
    """NFC, stripped and casefolded: the same definition the application uses for `*_key` columns."""
    return unicodedata.normalize("NFC", value).strip().casefold()


def upgrade() -> None:
    connection = op.get_bind()

    # Step 1: the enum value, on a committed connection (see the module docstring). Everything below
    # runs in the new transaction, so a failure rolls back every table and the version update.
    connection.execute(sa.text("COMMIT"))
    connection.exec_driver_sql(f"ALTER TYPE {_ENUM} ADD VALUE IF NOT EXISTS '{_ENUM_VALUE}'")

    # Decide once, before creating anything, which tables this run creates.
    inspector = sa.inspect(connection)
    missing = {name for name in _TABLES if not inspector.has_table(name)}

    if "svc_customers" in missing:
        _create_customers()
    if "svc_categories" in missing:
        _create_categories()
    if "svc_tickets" in missing:
        _create_tickets()
    if "svc_history_entries" in missing:
        _create_history_entries()
    if "svc_settings" in missing:
        _create_settings()
    if "svc_field_labels" in missing:
        _create_field_labels()
    if "svc_signin_failures" in missing:
        _create_signin_failures()

    # Never RESTARTed and never setval'd: a re-run keeps the value an existing sequence has reached.
    op.execute(
        f"CREATE SEQUENCE IF NOT EXISTS {_TICKET_NUMBER_SEQUENCE} "
        "START 10000 MINVALUE 10000 MAXVALUE 99999 NO CYCLE CACHE 1"
    )

    # Seed only the tables this run created (Requirement 19.17).
    if "svc_categories" in missing:
        _seed_categories(connection)
    if "svc_field_labels" in missing:
        _seed_field_labels(connection)
    if "svc_settings" in missing:
        _seed_settings(connection)

    # Grants are idempotent, so they cover tables created now and tables that already existed.
    for name in _TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {name} TO {APPLICATION_ROLE}")
    for sequence in (_TICKET_NUMBER_SEQUENCE, *_IDENTITY_SEQUENCES):
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {sequence} TO {APPLICATION_ROLE}")


def downgrade() -> None:
    # Reverse dependency order. The enum value stays, no existing object is touched and no object-store
    # call is made: photo objects stay in the bucket.
    for name in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {name}")
    op.execute(f"DROP SEQUENCE IF EXISTS {_TICKET_NUMBER_SEQUENCE}")


def _create_customers() -> None:
    op.create_table(
        "svc_customers",
        sa.Column("id", UUID, nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("full_name_key", sa.Text(), nullable=False),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("apartment", sa.Text(), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name=op.f("svc_customers_pkey")),
        sa.UniqueConstraint("phone", name=op.f("uq_svc_customers_phone")),
        sa.CheckConstraint("length(phone) = 10", name=op.f("ck_svc_customers_phone_length")),
        sa.CheckConstraint(
            "length(full_name) BETWEEN 1 AND 100", name=op.f("ck_svc_customers_full_name_length")
        ),
        sa.CheckConstraint(
            "city IS NULL OR length(city) BETWEEN 1 AND 100", name=op.f("ck_svc_customers_city_length")
        ),
        sa.CheckConstraint(
            "apartment IS NULL OR length(apartment) BETWEEN 1 AND 20",
            name=op.f("ck_svc_customers_apartment_length"),
        ),
    )
    op.create_index(op.f("ix_svc_customers_full_name_key"), "svc_customers", ["full_name_key"])


def _create_categories() -> None:
    op.create_table(
        "svc_categories",
        sa.Column("id", UUID, nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name_he", sa.Text(), nullable=False),
        sa.Column("name_en", sa.Text(), nullable=False),
        sa.Column("name_he_key", sa.Text(), nullable=False),
        sa.Column("name_en_key", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name=op.f("svc_categories_pkey")),
        sa.UniqueConstraint("seq", name=op.f("uq_svc_categories_seq")),
        sa.UniqueConstraint("name_he_key", name=op.f("uq_svc_categories_name_he_key")),
        sa.UniqueConstraint("name_en_key", name=op.f("uq_svc_categories_name_en_key")),
        sa.CheckConstraint("length(name_he) BETWEEN 1 AND 60", name=op.f("ck_svc_categories_name_he_length")),
        sa.CheckConstraint("length(name_en) BETWEEN 1 AND 60", name=op.f("ck_svc_categories_name_en_length")),
    )


def _create_tickets() -> None:
    op.create_table(
        "svc_tickets",
        sa.Column("id", UUID, nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("ticket_number", sa.Integer(), nullable=False),
        sa.Column("customer_id", UUID, nullable=False),
        sa.Column("category_id", UUID, nullable=False),
        sa.Column("city", sa.Text(), nullable=False),
        sa.Column("city_key", sa.Text(), nullable=False),
        sa.Column("apartment", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("photo_key", sa.Text(), nullable=True),
        sa.Column("urgent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'received'")),
        sa.Column("assigned_technician_name", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("created_date", sa.Date(), nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("svc_tickets_pkey")),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["svc_customers.id"],
            name=op.f("fk_svc_tickets_customer_id_svc_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["svc_categories.id"],
            name=op.f("fk_svc_tickets_category_id_svc_categories"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("ticket_number", name=op.f("uq_svc_tickets_ticket_number")),
        sa.UniqueConstraint("photo_key", name=op.f("uq_svc_tickets_photo_key")),
        sa.CheckConstraint(
            "ticket_number BETWEEN 10000 AND 99999", name=op.f("ck_svc_tickets_ticket_number_range")
        ),
        sa.CheckConstraint(
            "status IN ('received', 'in_progress', 'closed', 'cancelled')",
            name=op.f("ck_svc_tickets_status_valid"),
        ),
        sa.CheckConstraint("length(city) BETWEEN 1 AND 100", name=op.f("ck_svc_tickets_city_length")),
        sa.CheckConstraint(
            "length(apartment) BETWEEN 1 AND 20", name=op.f("ck_svc_tickets_apartment_length")
        ),
        sa.CheckConstraint("length(phone) = 10", name=op.f("ck_svc_tickets_phone_length")),
        sa.CheckConstraint("length(description) <= 2000", name=op.f("ck_svc_tickets_description_length")),
        sa.CheckConstraint(
            "length(assigned_technician_name) <= 100",
            name=op.f("ck_svc_tickets_assigned_technician_name_length"),
        ),
    )
    op.create_index(
        op.f("ix_svc_tickets_customer_id_created_at"), "svc_tickets", ["customer_id", "created_at"]
    )
    op.create_index(
        op.f("ix_svc_tickets_created_at_ticket_number"), "svc_tickets", ["created_at", "ticket_number"]
    )
    op.create_index(op.f("ix_svc_tickets_status_urgent"), "svc_tickets", ["status", "urgent"])
    op.create_index(op.f("ix_svc_tickets_category_id"), "svc_tickets", ["category_id"])
    op.create_index(op.f("ix_svc_tickets_city_key"), "svc_tickets", ["city_key"])
    op.create_index(op.f("ix_svc_tickets_created_date"), "svc_tickets", ["created_date"])


def _create_history_entries() -> None:
    op.create_table(
        "svc_history_entries",
        sa.Column("id", UUID, nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("ticket_id", UUID, nullable=False),
        sa.Column("entry_text", sa.Text(), nullable=False),
        sa.Column("technician_name", sa.Text(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("edited_at", TIMESTAMPTZ, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("svc_history_entries_pkey")),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["svc_tickets.id"],
            name=op.f("fk_svc_history_entries_ticket_id_svc_tickets"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(entry_text) BETWEEN 1 AND 2000", name=op.f("ck_svc_history_entries_entry_text_length")
        ),
        sa.CheckConstraint(
            "length(technician_name) BETWEEN 1 AND 100",
            name=op.f("ck_svc_history_entries_technician_name_length"),
        ),
    )
    op.create_index(
        op.f("ix_svc_history_entries_ticket_id_created_at"),
        "svc_history_entries",
        ["ticket_id", "created_at"],
    )


def _create_settings() -> None:
    op.create_table(
        "svc_settings",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("description_required", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("photo_upload_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name=op.f("svc_settings_pkey")),
        sa.CheckConstraint("id = 1", name=op.f("ck_svc_settings_id_is_one")),
    )


def _create_field_labels() -> None:
    op.create_table(
        "svc_field_labels",
        sa.Column("field_key", sa.Text(), nullable=False),
        sa.Column("label_he", sa.Text(), nullable=False),
        sa.Column("label_en", sa.Text(), nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("field_key", name=op.f("svc_field_labels_pkey")),
        sa.CheckConstraint(
            "field_key IN ('city', 'apartment', 'phone', 'category', 'description', 'photo', 'urgent')",
            name=op.f("ck_svc_field_labels_field_key_valid"),
        ),
        sa.CheckConstraint(
            "length(label_he) BETWEEN 1 AND 60", name=op.f("ck_svc_field_labels_label_he_length")
        ),
        sa.CheckConstraint(
            "length(label_en) BETWEEN 1 AND 60", name=op.f("ck_svc_field_labels_label_en_length")
        ),
    )


def _create_signin_failures() -> None:
    op.create_table(
        "svc_signin_failures",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("failed_at", TIMESTAMPTZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("svc_signin_failures_pkey")),
    )
    op.create_index(
        op.f("ix_svc_signin_failures_phone_failed_at"), "svc_signin_failures", ["phone", "failed_at"]
    )
    op.create_index(op.f("ix_svc_signin_failures_failed_at"), "svc_signin_failures", ["failed_at"])


def _seed_categories(connection: sa.engine.Connection) -> None:
    # One statement per row, in order, so the identity column numbers them in the design's order.
    for name_he, name_en in _SEED_CATEGORIES:
        connection.execute(
            sa.text(
                "INSERT INTO svc_categories (name_he, name_en, name_he_key, name_en_key) "
                "VALUES (:name_he, :name_en, :name_he_key, :name_en_key)"
            ),
            {
                "name_he": name_he,
                "name_en": name_en,
                "name_he_key": _key(name_he),
                "name_en_key": _key(name_en),
            },
        )


def _seed_field_labels(connection: sa.engine.Connection) -> None:
    for field_key, label_he, label_en in _SEED_FIELD_LABELS:
        connection.execute(
            sa.text(
                "INSERT INTO svc_field_labels (field_key, label_he, label_en) "
                "VALUES (:field_key, :label_he, :label_en)"
            ),
            {"field_key": field_key, "label_he": label_he, "label_en": label_en},
        )


def _seed_settings(connection: sa.engine.Connection) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO svc_settings (id, description_required, photo_upload_enabled) "
            "VALUES (1, true, true)"
        )
    )
