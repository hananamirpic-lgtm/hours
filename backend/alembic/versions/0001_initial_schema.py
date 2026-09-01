"""Initial schema

Every table in the design, created by hand rather than by autogenerate, because the three
load-bearing pieces of this schema cannot be expressed by SQLAlchemy models alone:

* the partial unique index that allows an employee at most one open time entry,
* the GiST exclusion constraint that makes two overlapping completed entries impossible, and
* the privilege revocation that makes `change_logs` append-only.

Conventions, applied without exception:

* timestamps are `timestamptz` and are stored in UTC; local-time classification happens in the
  service layer, and `time_entries.work_date` records the local date of check-in so no reader has
  to recompute it,
* money is `NUMERIC(12,2)`; no float appears anywhere,
* durations are integer minutes, never fractional hours,
* current values that change over time (wages, billing rates) live in effective-dated history
  tables, so recalculating a past period cannot silently produce a different answer.

No column here holds a latitude, longitude, radius or any other location value. That is a
requirement (6.8, 9.6, 20.10), not an omission, and `tests/integration/test_no_location_columns.py`
asserts it against the live catalogue rather than trusting this note.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2025-01-01 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


# Repeated as a literal rather than imported from app code: a migration is a frozen record of what
# was applied, so it must not change meaning when a constant is renamed. `app.db.roles` holds the
# same name for the application to use, and a test asserts the two agree.
APPLICATION_ROLE = "hours_app"

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.TIMESTAMP(timezone=True)
MONEY = sa.Numeric(12, 2)

# `create_type=False` everywhere: the types are created once, explicitly, at the top of upgrade().
# Left to SQLAlchemy, a type shared by two tables is created twice and the second attempt fails.
user_role = postgresql.ENUM(
    "admin", "site_manager", "accounting", "employee", name="user_role", create_type=False
)
app_language = postgresql.ENUM("he", "en", name="app_language", create_type=False)
employee_status = postgresql.ENUM(
    "active", "on_leave", "inactive", "terminated", name="employee_status", create_type=False
)
site_status = postgresql.ENUM("active", "completed", "on_hold", name="site_status", create_type=False)
qr_mode = postgresql.ENUM("unified", "separate", name="qr_mode", create_type=False)
assignment_mode = postgresql.ENUM("open", "strict", name="assignment_mode", create_type=False)
time_entry_source = postgresql.ENUM(
    "qr_scan", "manual", "system_transition", name="time_entry_source", create_type=False
)
time_entry_status = postgresql.ENUM(
    "draft", "review", "approved", "locked", name="time_entry_status", create_type=False
)
document_type = postgresql.ENUM("passport", "work_permit", "other", name="document_type", create_type=False)
notification_severity = postgresql.ENUM(
    "info", "warning", "critical", name="notification_severity", create_type=False
)
calculation_status = postgresql.ENUM("draft", "final", name="calculation_status", create_type=False)
setting_value_type = postgresql.ENUM(
    "string", "integer", "decimal", "boolean", "time", "json", name="setting_value_type", create_type=False
)

ENUM_TYPES = (
    user_role,
    app_language,
    employee_status,
    site_status,
    qr_mode,
    assignment_mode,
    time_entry_source,
    time_entry_status,
    document_type,
    notification_severity,
    calculation_status,
    setting_value_type,
)

# Reverse creation order, so a table is always dropped before whatever it points at.
TABLES_IN_CREATION_ORDER = (
    "employees",
    "employee_rates",
    "clients",
    "users",
    "sites",
    "user_sites",
    "site_rates",
    "employee_sites",
    "time_entries",
    "change_logs",
    "period_locks",
    "holidays",
    "settings",
    "documents",
    "notifications",
    "payroll_records",
    "payroll_site_allocations",
    "billing_records",
)


def _id() -> sa.Column:
    return sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
    )


def upgrade() -> None:
    bind = op.get_bind()

    # pgcrypto for gen_random_uuid(); btree_gist so an exclusion constraint can combine an equality
    # test on a uuid with an overlap test on a range, which is what the no-overlap guarantee needs.
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "btree_gist"')

    for enum_type in ENUM_TYPES:
        enum_type.create(bind, checkfirst=True)

    _create_people_and_places()
    _create_time_entries()
    _create_audit_and_periods()
    _create_reference_data()
    _create_documents_and_notifications()
    _create_payroll_and_billing()
    _apply_application_role_privileges()


def downgrade() -> None:
    bind = op.get_bind()

    for table in reversed(TABLES_IN_CREATION_ORDER):
        op.drop_table(table)

    for enum_type in ENUM_TYPES:
        enum_type.drop(bind, checkfirst=True)

    _drop_application_role()

    # Safe here because this is the first migration: at this point the database holds nothing else
    # that could depend on either extension. Neither drop uses CASCADE, so if something unexpected
    # does depend on one, the downgrade fails loudly instead of removing it.
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
    op.execute('DROP EXTENSION IF EXISTS "pgcrypto"')


# --------------------------------------------------------------------------- people and places


def _create_people_and_places() -> None:
    op.create_table(
        "employees",
        _id(),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("full_name_en", sa.Text(), nullable=False),
        # Object-storage key, not the image. Optional at the database level because an employee is
        # created before a photo is uploaded; the API enforces that one arrives (Requirement 3.1).
        sa.Column("photo_key", sa.Text(), nullable=True),
        sa.Column("passport_number_encrypted", sa.Text(), nullable=False),
        # Deterministic HMAC of the passport number. Uniqueness and lookup need a stable value, and
        # AES-GCM ciphertext is different on every write, so it cannot serve either purpose.
        sa.Column("passport_number_hash", sa.Text(), nullable=False),
        sa.Column("phone_encrypted", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("date_of_birth_encrypted", sa.Text(), nullable=True),
        sa.Column("address_encrypted", sa.Text(), nullable=True),
        sa.Column("emergency_contact_name", sa.Text(), nullable=False),
        sa.Column("emergency_contact_phone_encrypted", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("position", sa.Text(), nullable=True),
        sa.Column("status", employee_status, nullable=False, server_default="active"),
        *_timestamps(),
        # No deleted_at: employees are never hard-deleted and never soft-deleted either. Status
        # carries the whole lifecycle, so historical time entries keep a valid parent row
        # (Requirement 3.8).
    )
    # Uniqueness applies to people still on the books. A terminated employee's passport must not
    # block a rehire or a genuinely different person reusing a recycled number (Requirement 3.6).
    op.execute(
        """
        CREATE UNIQUE INDEX uq_employees_passport_number_hash_not_terminated
            ON employees (passport_number_hash)
            WHERE status <> 'terminated'::employee_status
        """
    )
    op.create_index("ix_employees_status", "employees", ["status"])
    op.create_index("ix_employees_full_name", "employees", ["full_name"])

    op.create_table(
        "employee_rates",
        _id(),
        sa.Column(
            "employee_id",
            UUID,
            sa.ForeignKey("employees.id", name="fk_employee_rates_employee_id_employees"),
            nullable=False,
        ),
        sa.Column("hourly_wage", MONEY, nullable=False),
        sa.Column("overtime_rate", MONEY, nullable=False),
        sa.Column("shabbat_holiday_rate", MONEY, nullable=False),
        sa.Column("travel_allowance_daily", MONEY, nullable=False, server_default="0"),
        sa.Column("effective_from", sa.Date(), nullable=False),
        # NULL means "still in force". Payroll resolves the row covering each work date, which is
        # what makes a mid-month rate change produce the right answer (Requirement 16.9).
        sa.Column("effective_to", sa.Date(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="period_ordered",
        ),
        sa.CheckConstraint(
            "hourly_wage >= 0 AND overtime_rate >= 0 AND shabbat_holiday_rate >= 0 "
            "AND travel_allowance_daily >= 0",
            name="non_negative",
        ),
    )
    # Two rates in force on one date would make pay ambiguous, so the database refuses it rather
    # than leaving the guarantee to whichever code path writes next.
    op.execute(
        """
        ALTER TABLE employee_rates ADD CONSTRAINT ex_employee_rates_no_overlapping_periods
            EXCLUDE USING gist (
                employee_id WITH =,
                daterange(effective_from, effective_to, '[]') WITH &&
            )
        """
    )
    op.create_index(
        "ix_employee_rates_employee_id_effective_from",
        "employee_rates",
        ["employee_id", "effective_from"],
    )

    op.create_table(
        "clients",
        _id(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("company_number", sa.Text(), nullable=True),
        sa.Column("contact_person", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        # Days plus free text, per Requirement 5.5, so "30 days" stays computable.
        sa.Column("payment_terms_days", sa.Integer(), nullable=True),
        sa.Column("payment_terms_notes", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
        # Unique when present; several clients may legitimately have no company number, and NULLs
        # do not collide in a unique constraint (Requirement 5.2).
        sa.UniqueConstraint("company_number", name="uq_clients_company_number"),
        sa.CheckConstraint(
            "payment_terms_days IS NULL OR payment_terms_days >= 0",
            name="payment_terms_days_non_negative",
        ),
    )
    op.create_index("ix_clients_name", "clients", ["name"])

    op.create_table(
        "users",
        _id(),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", user_role, nullable=False),
        # Links a login to a person, set for the employee role. Unique so two logins cannot both
        # act as the same employee.
        sa.Column(
            "employee_id",
            UUID,
            sa.ForeignKey("employees.id", name="fk_users_employee_id_employees"),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("totp_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("is_2fa_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", TIMESTAMPTZ, nullable=True),
        sa.Column("language", app_language, nullable=False, server_default="he"),
        # Bumped on deactivation and on password change, which invalidates tokens already issued
        # without keeping server-side session state (Requirement 20.8).
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_login_at", TIMESTAMPTZ, nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("employee_id", name="uq_users_employee_id"),
        sa.CheckConstraint("failed_login_count >= 0", name="failed_login_count_non_negative"),
    )
    op.create_index("ix_users_role", "users", ["role"])

    op.create_table(
        "sites",
        _id(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("site_number", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column(
            "client_id",
            UUID,
            sa.ForeignKey("clients.id", name="fk_sites_client_id_clients"),
            nullable=False,
        ),
        # At most one manager per site, many sites per manager (Requirement 6.7). The column carries
        # the "at most one" half; `user_sites` carries the scope a manager can actually read.
        sa.Column(
            "manager_user_id",
            UUID,
            sa.ForeignKey("users.id", name="fk_sites_manager_user_id_users"),
            nullable=True,
        ),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("status", site_status, nullable=False, server_default="active"),
        sa.Column("notes", sa.Text(), nullable=True),
        # Unified: one code for both directions, the server decides which. Separate: distinct
        # check-in and check-out codes (Requirement 8).
        sa.Column("qr_mode", qr_mode, nullable=False, server_default="unified"),
        sa.Column("qr_token", sa.Text(), nullable=False),
        # Bumped by regeneration, which is what invalidates a printed code that has leaked.
        sa.Column("qr_token_version", sa.Integer(), nullable=False, server_default="1"),
        # Open: anyone may record time here. Strict: only assigned employees. Assignment is an
        # expectation, not a location control — there is no location control.
        sa.Column("assignment_mode", assignment_mode, nullable=False, server_default="open"),
        *_timestamps(),
        sa.UniqueConstraint("site_number", name="uq_sites_site_number"),
        # The token is the whole of the QR payload's authority, so two sites must never share one.
        sa.UniqueConstraint("qr_token", name="uq_sites_qr_token"),
        sa.CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date",
            name="dates_ordered",
        ),
        sa.CheckConstraint("qr_token_version >= 1", name="qr_token_version_positive"),
        # No latitude, longitude, radius or location-mode column exists here, by requirement
        # (6.8, 20.10). Nothing in the scan path has anywhere to put a coordinate.
    )
    op.create_index("ix_sites_client_id", "sites", ["client_id"])
    op.create_index("ix_sites_status", "sites", ["status"])

    op.create_table(
        "user_sites",
        sa.Column(
            "user_id",
            UUID,
            sa.ForeignKey("users.id", name="fk_user_sites_user_id_users", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "site_id",
            UUID,
            sa.ForeignKey("sites.id", name="fk_user_sites_site_id_sites", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        # Composite primary key, no surrogate id: the pair *is* the fact. Site-manager
        # authorization reads exclusively from this table.
        sa.PrimaryKeyConstraint("user_id", "site_id", name="user_sites_pkey"),
    )
    op.create_index("ix_user_sites_site_id", "user_sites", ["site_id"])

    op.create_table(
        "site_rates",
        _id(),
        sa.Column(
            "site_id",
            UUID,
            sa.ForeignKey("sites.id", name="fk_site_rates_site_id_sites"),
            nullable=False,
        ),
        sa.Column("billing_rate", MONEY, nullable=False),
        # Optional: where no overtime billing rate is configured, overtime bills at the standard
        # rate (Requirement 17.2).
        sa.Column("overtime_billing_rate", MONEY, nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="period_ordered",
        ),
        sa.CheckConstraint(
            "billing_rate >= 0 AND (overtime_billing_rate IS NULL OR overtime_billing_rate >= 0)",
            name="non_negative",
        ),
    )
    # History, not a mutable current value: this is what makes Requirement 6.6 true — updating a
    # rate cannot retroactively change what an already-billed period was worth.
    op.execute(
        """
        ALTER TABLE site_rates ADD CONSTRAINT ex_site_rates_no_overlapping_periods
            EXCLUDE USING gist (
                site_id WITH =,
                daterange(effective_from, effective_to, '[]') WITH &&
            )
        """
    )
    op.create_index("ix_site_rates_site_id_effective_from", "site_rates", ["site_id", "effective_from"])

    op.create_table(
        "employee_sites",
        sa.Column(
            "employee_id",
            UUID,
            sa.ForeignKey("employees.id", name="fk_employee_sites_employee_id_employees", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "site_id",
            UUID,
            sa.ForeignKey("sites.id", name="fk_employee_sites_site_id_sites", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("assigned_from", sa.Date(), nullable=False),
        sa.Column("assigned_to", sa.Date(), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("employee_id", "site_id", name="employee_sites_pkey"),
        sa.CheckConstraint(
            "assigned_to IS NULL OR assigned_to >= assigned_from",
            name="dates_ordered",
        ),
        # Expected placement only. This table never restricts where time can be recorded — that is
        # what `sites.assignment_mode` decides, and even then it is an expectation, not a location
        # check (Requirement 7.3).
    )
    op.create_index("ix_employee_sites_site_id", "employee_sites", ["site_id"])


def _create_time_entries() -> None:
    """The core table, plus the three constraints the whole design rests on."""
    op.create_table(
        "time_entries",
        _id(),
        sa.Column(
            "employee_id",
            UUID,
            sa.ForeignKey("employees.id", name="fk_time_entries_employee_id_employees"),
            nullable=False,
        ),
        sa.Column(
            "site_id",
            UUID,
            sa.ForeignKey("sites.id", name="fk_time_entries_site_id_sites"),
            nullable=False,
        ),
        # Local date of check-in, written by the service. A shift crossing midnight belongs to the
        # day it started, and no report should have to derive that from a UTC timestamp.
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("check_in_at", TIMESTAMPTZ, nullable=False),
        sa.Column("check_out_at", TIMESTAMPTZ, nullable=True),
        sa.Column("total_minutes", sa.Integer(), nullable=True),
        sa.Column("source", time_entry_source, nullable=False),
        sa.Column("is_manual", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("manual_reason", sa.Text(), nullable=True),
        sa.Column("status", time_entry_status, nullable=False, server_default="draft"),
        # `unassigned_site`, `implausible_duration`. An array rather than a column per flag, so a
        # new anomaly needs no migration.
        sa.Column(
            "flags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "created_by_user_id",
            UUID,
            sa.ForeignKey("users.id", name="fk_time_entries_created_by_user_id_users"),
            nullable=True,
        ),
        sa.Column(
            "closed_by_user_id",
            UUID,
            sa.ForeignKey("users.id", name="fk_time_entries_closed_by_user_id_users"),
            nullable=True,
        ),
        sa.Column("deleted_at", TIMESTAMPTZ, nullable=True),
        sa.Column("delete_reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "check_out_at IS NULL OR check_out_at > check_in_at",
            name="check_out_after_check_in",
        ),
        sa.CheckConstraint(
            "total_minutes IS NULL OR total_minutes >= 0",
            name="total_minutes_non_negative",
        ),
        # Requirement 12.3: a manual entry or correction without a stated reason is not acceptable,
        # and a blank string is not a reason.
        sa.CheckConstraint(
            "NOT is_manual OR btrim(coalesce(manual_reason, '')) <> ''",
            name="manual_requires_reason",
        ),
        sa.CheckConstraint(
            "deleted_at IS NULL OR btrim(coalesce(delete_reason, '')) <> ''",
            name="delete_requires_reason",
        ),
    )

    # Requirement 11.3, enforced by the database rather than only in application code: two
    # concurrent check-ins race here, and exactly one of them wins.
    op.execute(
        """
        CREATE UNIQUE INDEX one_open_entry_per_employee
            ON time_entries (employee_id)
            WHERE check_out_at IS NULL AND deleted_at IS NULL
        """
    )

    # Requirement 11.7. Applies to completed entries: an open entry has no end, so it has no range
    # to overlap, and the index above is what constrains it.
    op.execute(
        """
        ALTER TABLE time_entries ADD CONSTRAINT no_overlapping_entries
            EXCLUDE USING gist (
                employee_id WITH =,
                tstzrange(check_in_at, check_out_at) WITH &&
            ) WHERE (check_out_at IS NOT NULL AND deleted_at IS NULL)
        """
    )

    op.create_index("ix_time_entries_employee_id_work_date", "time_entries", ["employee_id", "work_date"])
    op.create_index("ix_time_entries_site_id_work_date", "time_entries", ["site_id", "work_date"])
    op.create_index("ix_time_entries_status_work_date", "time_entries", ["status", "work_date"])
    op.create_index("ix_time_entries_work_date", "time_entries", ["work_date"])


def _create_audit_and_periods() -> None:
    op.create_table(
        "change_logs",
        _id(),
        # Polymorphic by design: one audit table for every entity, so a new entity type needs no
        # new table and the audit view has one shape to render.
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column(
            "changed_by_user_id",
            UUID,
            sa.ForeignKey("users.id", name="fk_change_logs_changed_by_user_id_users"),
            nullable=True,
        ),
        sa.Column("changed_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        # One row per changed field, which is what lets the audit view render a sentence naming the
        # field, its old value and its new one.
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        # No updated_at and no deleted_at: an append-only table has nothing to update.
    )
    op.create_index(
        "ix_change_logs_entity_type_entity_id_changed_at",
        "change_logs",
        ["entity_type", "entity_id", "changed_at"],
    )
    op.create_index(
        "ix_change_logs_changed_by_user_id_changed_at",
        "change_logs",
        ["changed_by_user_id", "changed_at"],
    )

    op.create_table(
        "period_locks",
        _id(),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column(
            "locked_by_user_id",
            UUID,
            sa.ForeignKey("users.id", name="fk_period_locks_locked_by_user_id_users"),
            nullable=True,
        ),
        sa.Column("locked_at", TIMESTAMPTZ, nullable=True),
        sa.Column(
            "unlocked_by_user_id",
            UUID,
            sa.ForeignKey("users.id", name="fk_period_locks_unlocked_by_user_id_users"),
            nullable=True,
        ),
        sa.Column("unlocked_at", TIMESTAMPTZ, nullable=True),
        sa.Column("unlock_reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("year", "month", name="uq_period_locks_year_month"),
        sa.CheckConstraint("month BETWEEN 1 AND 12", name="month_range"),
        sa.CheckConstraint("year BETWEEN 2000 AND 2200", name="year_range"),
        # An unlock without a stated reason leaves no answer to "why did approved hours move?"
        sa.CheckConstraint(
            "unlocked_at IS NULL OR btrim(coalesce(unlock_reason, '')) <> ''",
            name="unlock_requires_reason",
        ),
    )


def _create_reference_data() -> None:
    op.create_table(
        "holidays",
        _id(),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("name_he", sa.Text(), nullable=False),
        sa.Column("name_en", sa.Text(), nullable=False),
        sa.Column("is_full_day", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint("date", name="uq_holidays_date"),
    )

    op.create_table(
        "settings",
        _id(),
        sa.Column("key", sa.Text(), nullable=False),
        # Stored as text with a declared type, read through typed accessors. One row shape serves
        # every setting, and the accessor is the single place a conversion can go wrong.
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("value_type", setting_value_type, nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "updated_by_user_id",
            UUID,
            sa.ForeignKey("users.id", name="fk_settings_updated_by_user_id_users"),
            nullable=True,
        ),
        *_timestamps(),
        sa.UniqueConstraint("key", name="uq_settings_key"),
    )


def _create_documents_and_notifications() -> None:
    op.create_table(
        "documents",
        _id(),
        sa.Column(
            "employee_id",
            UUID,
            sa.ForeignKey("employees.id", name="fk_documents_employee_id_employees"),
            nullable=False,
        ),
        sa.Column("type", document_type, nullable=False),
        # Key in private object storage. The file itself never enters the database, and is only
        # ever served through a short-lived signed URL.
        sa.Column("file_key", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column(
            "uploaded_by_user_id",
            UUID,
            sa.ForeignKey("users.id", name="fk_documents_uploaded_by_user_id_users"),
            nullable=True,
        ),
        sa.Column("deleted_at", TIMESTAMPTZ, nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("file_key", name="uq_documents_file_key"),
        sa.CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
    )
    op.create_index("ix_documents_employee_id", "documents", ["employee_id"])
    # Drives the daily expiry sweep, which scans by date across all employees.
    op.create_index("ix_documents_expiry_date", "documents", ["expiry_date"])

    op.create_table(
        "notifications",
        _id(),
        sa.Column(
            "recipient_user_id",
            UUID,
            sa.ForeignKey("users.id", name="fk_notifications_recipient_user_id_users"),
            nullable=False,
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("severity", notification_severity, nullable=False, server_default="info"),
        # Translation key plus parameters, never a rendered sentence: the recipient's language is
        # applied at render time (Requirement 21.6).
        sa.Column("title_key", sa.Text(), nullable=False),
        sa.Column(
            "body_params",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("related_entity_type", sa.Text(), nullable=True),
        sa.Column("related_entity_id", UUID, nullable=True),
        # Unique, and this is what makes a daily job idempotent: a re-run collides instead of
        # sending a second copy (Requirements 4.6, 14.x).
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("read_at", TIMESTAMPTZ, nullable=True),
        sa.Column(
            "delivered_channels",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        *_timestamps(),
        sa.UniqueConstraint("dedupe_key", name="uq_notifications_dedupe_key"),
    )
    op.create_index(
        "ix_notifications_recipient_user_id_is_read_created_at",
        "notifications",
        ["recipient_user_id", "is_read", "created_at"],
    )


def _create_payroll_and_billing() -> None:
    op.create_table(
        "payroll_records",
        _id(),
        sa.Column(
            "employee_id",
            UUID,
            sa.ForeignKey("employees.id", name="fk_payroll_records_employee_id_employees"),
            nullable=False,
        ),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        # Minutes, not hours: 4 h 29 min is 4.4833… hours and rounding that repeatedly drifts.
        sa.Column("regular_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overtime_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shabbat_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("holiday_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("regular_pay", MONEY, nullable=False, server_default="0"),
        sa.Column("overtime_pay", MONEY, nullable=False, server_default="0"),
        sa.Column("shabbat_pay", MONEY, nullable=False, server_default="0"),
        sa.Column("holiday_pay", MONEY, nullable=False, server_default="0"),
        sa.Column("travel", MONEY, nullable=False, server_default="0"),
        sa.Column("bonuses", MONEY, nullable=False, server_default="0"),
        sa.Column("deductions", MONEY, nullable=False, server_default="0"),
        sa.Column("total_pay", MONEY, nullable=False, server_default="0"),
        sa.Column("status", calculation_status, nullable=False, server_default="draft"),
        sa.Column("calculated_at", TIMESTAMPTZ, nullable=True),
        *_timestamps(),
        # Recalculation must replace the draft, not add a second one (Requirement 16.10). The
        # constraint is what makes an upsert possible.
        sa.UniqueConstraint("employee_id", "year", "month", name="uq_payroll_records_employee_id_year_month"),
        sa.CheckConstraint("month BETWEEN 1 AND 12", name="month_range"),
        sa.CheckConstraint("year BETWEEN 2000 AND 2200", name="year_range"),
        sa.CheckConstraint(
            "regular_minutes >= 0 AND overtime_minutes >= 0 AND shabbat_minutes >= 0 "
            "AND holiday_minutes >= 0",
            name="minutes_non_negative",
        ),
    )
    op.create_index("ix_payroll_records_year_month", "payroll_records", ["year", "month"])

    op.create_table(
        "payroll_site_allocations",
        _id(),
        sa.Column(
            "payroll_record_id",
            UUID,
            sa.ForeignKey(
                "payroll_records.id",
                name="fk_payroll_site_allocations_payroll_record_id_payroll_records",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "site_id",
            UUID,
            sa.ForeignKey("sites.id", name="fk_payroll_site_allocations_site_id_sites"),
            nullable=False,
        ),
        sa.Column("regular_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overtime_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shabbat_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("holiday_minutes", sa.Integer(), nullable=False, server_default="0"),
        # Must sum, across sites, to the parent record's pay excluding allowances. A stray agora
        # here would undermine every profit figure downstream (Requirement 16.8).
        sa.Column("cost", MONEY, nullable=False, server_default="0"),
        *_timestamps(),
        sa.UniqueConstraint(
            "payroll_record_id", "site_id", name="uq_payroll_site_allocations_payroll_record_id_site_id"
        ),
        sa.CheckConstraint(
            "regular_minutes >= 0 AND overtime_minutes >= 0 AND shabbat_minutes >= 0 "
            "AND holiday_minutes >= 0",
            name="minutes_non_negative",
        ),
    )
    op.create_index("ix_payroll_site_allocations_site_id", "payroll_site_allocations", ["site_id"])

    op.create_table(
        "billing_records",
        _id(),
        sa.Column(
            "client_id",
            UUID,
            sa.ForeignKey("clients.id", name="fk_billing_records_client_id_clients"),
            nullable=False,
        ),
        sa.Column(
            "site_id",
            UUID,
            sa.ForeignKey("sites.id", name="fk_billing_records_site_id_sites"),
            nullable=False,
        ),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("regular_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overtime_minutes", sa.Integer(), nullable=False, server_default="0"),
        # The rate actually used, copied at calculation time. Reading it back from site_rates later
        # would re-derive history and could disagree with the invoice already sent.
        sa.Column("billing_rate_applied", MONEY, nullable=False),
        sa.Column("overtime_rate_applied", MONEY, nullable=True),
        sa.Column("total_amount", MONEY, nullable=False, server_default="0"),
        sa.Column("status", calculation_status, nullable=False, server_default="draft"),
        sa.Column("calculated_at", TIMESTAMPTZ, nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("site_id", "year", "month", name="uq_billing_records_site_id_year_month"),
        sa.CheckConstraint("month BETWEEN 1 AND 12", name="month_range"),
        sa.CheckConstraint("year BETWEEN 2000 AND 2200", name="year_range"),
        sa.CheckConstraint(
            "regular_minutes >= 0 AND overtime_minutes >= 0",
            name="minutes_non_negative",
        ),
    )
    op.create_index(
        "ix_billing_records_client_id_year_month", "billing_records", ["client_id", "year", "month"]
    )


# --------------------------------------------------------------------------- privileges


def _apply_application_role_privileges() -> None:
    """Create the application role and make `change_logs` append-only for it.

    Requirement 13.3 says audit records are append-only. A code review can guarantee that no
    endpoint updates them; only the database can guarantee that no future code path does. The role
    is created here rather than by an operator so that a fresh environment is correct by default.
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APPLICATION_ROLE}') THEN
                CREATE ROLE {APPLICATION_ROLE} NOLOGIN;
            END IF;
        END $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APPLICATION_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APPLICATION_ROLE}")
    # ALL TABLES caught Alembic's own bookkeeping table as well. The application has no business
    # rewriting which migration is applied, and leaving the grant in place would also make the role
    # undroppable on downgrade, since privileges count as dependent objects.
    op.execute(f"REVOKE ALL ON alembic_version FROM {APPLICATION_ROLE}")
    # The point of the whole function. TRUNCATE is named as well: it is not granted above, but
    # revoking it explicitly means a later blanket GRANT ALL cannot quietly hand it over.
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON change_logs FROM {APPLICATION_ROLE}")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON change_logs FROM PUBLIC")

    # So a non-superuser migration runner can still `SET ROLE` to it, and so an operator can grant
    # it to the login role the API uses. A superuser is already a member of everything, so
    # pg_has_role short-circuits and nothing happens.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT pg_has_role(current_user, '{APPLICATION_ROLE}', 'MEMBER') THEN
                EXECUTE format('GRANT %I TO %I', '{APPLICATION_ROLE}', current_user);
            END IF;
        END $$;
        """
    )


def _drop_application_role() -> None:
    """Remove the role, unless another database in the cluster still depends on it.

    A role is cluster-wide while a migration is database-wide, so the drop can legitimately fail
    when the same role serves a second database. That case is tolerated rather than forced: the
    alternative is a downgrade that either errors for a reason unrelated to this database, or takes
    privileges away from an unrelated one.
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APPLICATION_ROLE}') THEN
                EXECUTE 'REVOKE ALL ON SCHEMA public FROM {APPLICATION_ROLE}';
                BEGIN
                    EXECUTE 'DROP ROLE {APPLICATION_ROLE}';
                EXCEPTION WHEN dependent_objects_still_exist OR insufficient_privilege THEN
                    NULL;
                END;
            END IF;
        END $$;
        """
    )
