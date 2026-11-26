"""Migration `0011_service_tickets` leaves the Hours data alone, seeds only what it creates, and reverses.

Migration `0012_staff_opened_tickets` (Requirement 26.4) adds one nullable column,
`svc_tickets.opened_by_username`; a downgrade to `0011` removes only that column.

Migration `0013_service_lists_branding` (Requirements 28.6, 29.6, 29.7, 30.1, 30.4) adds four tables
(`svc_localities`, `svc_parts`, `svc_ticket_parts`, `svc_branding`) and a nullable `locality_id` column on
`svc_tickets` and `svc_customers`; a downgrade to `0012` removes exactly those and keeps every ticket and
customer.

This file is the permanent proof of the product owner's overriding constraint for the service-tickets
release: applying the migration to a live Hours database changes nothing the Hours system holds.

* Requirement 20.1 and 25.3: no existing table, column, index, constraint, grant or row changes, and
  the only change to an existing object is the `technician` value of the `user_role` enum.
* Requirement 20.2: no foreign key crosses the boundary between the `svc_` tables and the rest.
* Requirements 19.14 and 19.15: the default categories, field labels and settings.
* Requirement 19.17: a re-run against existing tables adds back, renames, reactivates and resets
  nothing.
* Requirement 25.9: the downgrade drops only the `svc_` objects.

Nothing here is mocked. Every test runs Alembic against a scratch PostgreSQL database that the
integration harness creates and drops (`hours_test_<id>`), and asserts what the catalog and the rows
say afterwards. The development database is never touched.

How "Hours data is unchanged" is measured. The list of Hours tables is read from the catalog at
revision `0010`, not written down. For every one of them a snapshot records the column list (name,
position, type, nullability, default, identity, generation, collation, column privileges), every
constraint with its definition, every index with its definition, the triggers (including the internal
ones that a foreign key pointing at the table would add), the constraints of other tables that
reference it, its owner, its privileges, its storage options, its row count and an `md5` checksum of
the ordered text of every row. The snapshot taken at `0010` must equal the one taken at `0011`.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

import app.models  # noqa: F401  (registers every table on Base.metadata, the svc_ ones included)
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from app.core.security import hash_password
from app.db.base import Base

HOURS_REVISION = "0010_operations_admin_role"
SERVICE_REVISION = "0011_service_tickets"
STAFF_OPENED_REVISION = "0012_staff_opened_tickets"
LISTS_REVISION = "0013_service_lists_branding"
OPENED_BY_COLUMN = "opened_by_username"
LOCALITY_COLUMN = "locality_id"
SVC_PREFIX = "svc_"

# What migration 0011 creates: the tables, the ticket-number sequence and the two sequences behind the
# identity columns.
SVC_TABLES_0011 = {
    "svc_customers",
    "svc_categories",
    "svc_tickets",
    "svc_history_entries",
    "svc_settings",
    "svc_field_labels",
    "svc_signin_failures",
}
SVC_SEQUENCES_0011 = {"svc_ticket_number_seq", "svc_categories_seq_seq", "svc_signin_failures_id_seq"}

# What migration 0013 adds: four tables and the two sequences behind their identity columns.
SVC_TABLES_0013 = {"svc_localities", "svc_parts", "svc_ticket_parts", "svc_branding"}
SVC_SEQUENCES_0013 = {"svc_localities_seq_seq", "svc_parts_seq_seq"}

# The closed sets at head.
SVC_TABLES = SVC_TABLES_0011 | SVC_TABLES_0013
SVC_SEQUENCES = SVC_SEQUENCES_0011 | SVC_SEQUENCES_0013

# Requirement 19.14, in the design's order. Written out here, not imported from the migration, so a
# change to the migration's seed cannot silently move the test with it.
SEED_CATEGORIES = [
    ("דליפה / סתימה", "Leak or Blockage"),
    ("רטיבות / לחות", "Dampness or Moisture"),
    ("חשמל", "Electrical"),
    ("אינסטלציה", "Plumbing"),
    ("קירות וגמרים", "Walls and Finishing"),
    ("כללי", "General"),
]

# Requirement 19.15: field_key -> (label_he, label_en).
SEED_FIELD_LABELS = {
    "city": ("ישוב", "Settlement"),
    "apartment": ("מספר דירה", "Apartment number"),
    "phone": ("מספר טלפון", "Phone number"),
    "category": ("קטגוריית תקלה", "Issue category"),
    "description": ("תיאור התקלה", "Issue description"),
    "photo": ("תמונה", "Photo"),
    "urgent": ("דחוף", "Urgent"),
}

# Existing-table queries that are used more than once.
_SERVICE_RELATIONS_SQL = (
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind = :kind AND left(c.relname, 4) = 'svc_'"
)


# --------------------------------------------------------------------------------- fixtures


@dataclass(frozen=True)
class Scratch:
    """A scratch database from the harness, with an Alembic config and an engine aimed at it."""

    url: str
    config: Config
    engine: Engine


@pytest.fixture
def scratch(empty_database: str, make_alembic_config: Callable[[str], Config]) -> Iterator[Scratch]:
    engine = sa.create_engine(empty_database, poolclass=sa.pool.NullPool)
    try:
        yield Scratch(empty_database, make_alembic_config(empty_database), engine)
    finally:
        engine.dispose()


@pytest.fixture
def hours_database(scratch: Scratch) -> Scratch:
    """A scratch database at `0010` holding realistic Hours rows in every existing table."""
    command.upgrade(scratch.config, HOURS_REVISION)
    with scratch.engine.begin() as connection:
        _populate_hours_data(connection)
    return scratch


# --------------------------------------------------------------------------------- Hours data


def _scalar(connection: sa.Connection, sql: str, **params: Any) -> Any:
    return connection.execute(sa.text(sql), params).scalar_one()


def _populate_hours_data(connection: sa.Connection) -> None:
    """Insert rows into every table that exists at `0010`, NOT NULL columns set, in dependency order."""
    password_hash = hash_password("Correct-Horse-Battery-1")

    client_id = _scalar(
        connection,
        "INSERT INTO clients (name, company, company_number, notes) "
        "VALUES (:name, :company, :number, :notes) RETURNING id",
        name="קיבוץ ראש צורים",
        company="Rosh Tzurim Agricultural Cooperative",
        number="570012345",
        notes="Monthly invoice, 30 days net",
    )
    connection.execute(sa.text("INSERT INTO clients (name) VALUES ('Moshav Nahalal')"))

    staffing_company_id = _scalar(
        connection,
        "INSERT INTO staffing_companies (name, contact_person, hourly_rate, telephone, comments) "
        "VALUES ('Galil Manpower', 'Ronit Peretz', 61.50, '04-6001234', 'Agricultural placements') "
        "RETURNING id",
    )

    employee_ids: list[uuid.UUID] = []
    for number, full_name, english, passport in (
        ("E-1001", "אברהם כהן", "Avraham Cohen", "P-100001"),
        ("E-1002", "שרה לוי", "Sarah Levi", "P-100002"),
        ("E-1003", "Somchai Prasert", "Somchai Prasert", "P-100003"),
    ):
        employee_ids.append(
            _scalar(
                connection,
                """
                INSERT INTO employees (
                    full_name, full_name_en, passport_number_encrypted, passport_number_hash,
                    phone_encrypted, country, emergency_contact_name,
                    emergency_contact_phone_encrypted, start_date, employee_number, position,
                    staffing_company_id, notes
                ) VALUES (
                    :full_name, :english, :passport_enc, :passport_hash, :phone, 'IL', 'Dana Cohen',
                    :emergency, DATE '2025-01-01', :number, 'Field worker', :staffing, 'Hired in season'
                ) RETURNING id
                """,
                full_name=full_name,
                english=english,
                passport_enc=f"gcm:{passport}",
                passport_hash=f"hmac:{passport}",
                phone=f"gcm:phone:{number}",
                emergency=f"gcm:emergency:{number}",
                number=number,
                staffing=staffing_company_id,
            )
        )

    user_ids: dict[str, uuid.UUID] = {}
    for username, role, employee_id in (
        ("admin.noa", "admin", None),
        ("ops.yossi", "operations_admin", None),
        ("accounting.rina", "accounting", None),
        ("manager.david", "site_manager", None),
        ("E-1001", "employee", employee_ids[0]),
        ("E-1002", "employee", employee_ids[1]),
    ):
        user_ids[username] = _scalar(
            connection,
            """
            INSERT INTO users (username, password_hash, role, employee_id, language, must_change_password)
            VALUES (:username, :password_hash, CAST(:role AS user_role), :employee_id, 'he', :must)
            RETURNING id
            """,
            username=username,
            password_hash=password_hash,
            role=role,
            employee_id=employee_id,
            must=role not in {"admin", "operations_admin"},
        )

    site_ids: list[uuid.UUID] = []
    for name, number, address in (
        ("Orchard North", "S-001", "Rosh Tzurim, plot 4"),
        ("Greenhouse Row", "S-002", "Rosh Tzurim, plot 9"),
    ):
        site_ids.append(
            _scalar(
                connection,
                """
                INSERT INTO sites (name, site_number, address, client_id, manager_user_id, qr_token)
                VALUES (:name, :number, :address, :client_id, :manager, :token) RETURNING id
                """,
                name=name,
                number=number,
                address=address,
                client_id=client_id,
                manager=user_ids["manager.david"],
                token=f"qr-{number}",
            )
        )

    connection.execute(
        sa.text("INSERT INTO user_sites (user_id, site_id) VALUES (:user_id, :site_id)"),
        {"user_id": user_ids["manager.david"], "site_id": site_ids[0]},
    )
    for employee_id in employee_ids:
        connection.execute(
            sa.text(
                "INSERT INTO employee_rates (employee_id, hourly_wage, overtime_rate, "
                "shabbat_holiday_rate, travel_allowance_daily, effective_from) "
                "VALUES (:employee_id, 38.50, 48.13, 57.75, 15.00, DATE '2025-01-01')"
            ),
            {"employee_id": employee_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO employee_sites (employee_id, site_id, assigned_from) "
                "VALUES (:employee_id, :site_id, DATE '2025-01-01')"
            ),
            {"employee_id": employee_id, "site_id": site_ids[0]},
        )
    for site_id in site_ids:
        connection.execute(
            sa.text(
                "INSERT INTO site_rates (site_id, billing_rate, overtime_billing_rate, effective_from) "
                "VALUES (:site_id, 62.00, 77.50, DATE '2025-01-01')"
            ),
            {"site_id": site_id},
        )

    # (employee, site, work date, check-in, check-out, minutes); the last entry is still open.
    entries = (
        (
            employee_ids[0],
            site_ids[0],
            date(2026, 1, 4),
            datetime(2026, 1, 4, 5, 58, tzinfo=UTC),
            datetime(2026, 1, 4, 14, 31, tzinfo=UTC),
            513,
        ),
        (
            employee_ids[1],
            site_ids[1],
            date(2026, 1, 4),
            datetime(2026, 1, 4, 6, 2, tzinfo=UTC),
            datetime(2026, 1, 4, 13, 47, tzinfo=UTC),
            465,
        ),
        (employee_ids[2], site_ids[0], date(2026, 1, 5), datetime(2026, 1, 5, 6, 0, tzinfo=UTC), None, None),
    )
    entry_ids: list[uuid.UUID] = []
    for employee_id, site_id, work_date, check_in, check_out, minutes in entries:
        entry_ids.append(
            _scalar(
                connection,
                """
                INSERT INTO time_entries (
                    employee_id, site_id, work_date, check_in_at, check_out_at, total_minutes, source,
                    status, created_by_user_id
                ) VALUES (
                    :employee_id, :site_id, :work_date, :check_in, :check_out, :minutes,
                    CAST('qr_scan' AS time_entry_source),
                    CAST(:status AS time_entry_status), :created_by
                ) RETURNING id
                """,
                employee_id=employee_id,
                site_id=site_id,
                work_date=work_date,
                check_in=check_in,
                check_out=check_out,
                minutes=minutes,
                status="approved" if check_out else "draft",
                created_by=user_ids["admin.noa"],
            )
        )

    for field, old, new in (
        ("check_out_at", "2026-01-04T14:30:00+00:00", "2026-01-04T14:31:00+00:00"),
        ("total_minutes", "512", "513"),
    ):
        connection.execute(
            sa.text(
                "INSERT INTO change_logs (entity_type, entity_id, changed_by_user_id, field, old_value, "
                "new_value, reason, request_id) VALUES ('time_entry', :entity_id, :user_id, :field, "
                ":old, :new, 'Corrected after the foreman confirmed the time', :request_id)"
            ),
            {
                "entity_id": entry_ids[0],
                "user_id": user_ids["admin.noa"],
                "field": field,
                "old": old,
                "new": new,
                "request_id": f"req-{uuid.uuid4().hex[:8]}",
            },
        )

    connection.execute(
        sa.text(
            "INSERT INTO period_locks (year, month, locked_by_user_id, locked_at) "
            "VALUES (2025, 12, :user_id, :locked_at)"
        ),
        {"user_id": user_ids["admin.noa"], "locked_at": datetime(2026, 1, 2, 9, 0, tzinfo=UTC)},
    )
    # The year is far outside the seeded calendar, so this never collides with a seeded holiday.
    connection.execute(
        sa.text(
            "INSERT INTO holidays (date, name_he, name_en) "
            "VALUES (DATE '2099-04-01', 'חג בדיקה', 'Test holiday')"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO settings (key, value, value_type, description, updated_by_user_id) "
            "VALUES ('integration_probe', '42', CAST('integer' AS setting_value_type), "
            "'Row added by the migration test', :user_id)"
        ),
        {"user_id": user_ids["admin.noa"]},
    )
    connection.execute(
        sa.text(
            "INSERT INTO documents (employee_id, type, file_key, file_name, mime_type, size_bytes, "
            "expiry_date, uploaded_by_user_id) VALUES (:employee_id, CAST('passport' AS document_type), "
            "'documents/passport-e1001.pdf', 'passport.pdf', 'application/pdf', 48211, "
            "DATE '2030-06-30', :user_id)"
        ),
        {"employee_id": employee_ids[0], "user_id": user_ids["admin.noa"]},
    )
    connection.execute(
        sa.text(
            "INSERT INTO notifications (recipient_user_id, type, severity, title_key, body_params, "
            "related_entity_type, related_entity_id, dedupe_key) VALUES (:user_id, 'document_expiry', "
            "CAST('warning' AS notification_severity), 'notifications.document_expiry', "
            "CAST(:params AS jsonb), 'employee', :employee_id, 'document_expiry:e1001:2030-06-30')"
        ),
        {
            "user_id": user_ids["admin.noa"],
            "employee_id": employee_ids[0],
            "params": json.dumps({"employee": "Avraham Cohen", "days": 30}),
        },
    )

    payroll_record_id = _scalar(
        connection,
        "INSERT INTO payroll_records (employee_id, year, month, regular_minutes, regular_pay, travel, "
        "total_pay, calculated_at) VALUES (:employee_id, 2025, 12, 9600, 6160.00, 330.00, 6490.00, :at) "
        "RETURNING id",
        employee_id=employee_ids[0],
        at=datetime(2026, 1, 3, 8, 0, tzinfo=UTC),
    )
    connection.execute(
        sa.text(
            "INSERT INTO payroll_site_allocations (payroll_record_id, site_id, regular_minutes, cost) "
            "VALUES (:record_id, :site_id, 9600, 6160.00)"
        ),
        {"record_id": payroll_record_id, "site_id": site_ids[0]},
    )
    connection.execute(
        sa.text(
            "INSERT INTO billing_records (client_id, site_id, year, month, regular_minutes, "
            "billing_rate_applied, total_amount, calculated_at) "
            "VALUES (:client_id, :site_id, 2025, 12, 9600, 62.00, 9920.00, :at)"
        ),
        {"client_id": client_id, "site_id": site_ids[0], "at": datetime(2026, 1, 3, 8, 5, tzinfo=UTC)},
    )
    connection.execute(
        sa.text(
            "INSERT INTO exports (requested_by_user_id, report_type, format, period_year, period_month, "
            "file_name) VALUES (:user_id, CAST('by_employee' AS export_report_type), "
            "CAST('xlsx' AS export_format), 2025, 12, 'hours-2025-12.xlsx')"
        ),
        {"user_id": user_ids["accounting.rina"]},
    )


# ---------------------------------------------------------------------------------- snapshots


def _rows(connection: sa.Connection, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in connection.execute(sa.text(sql), params).all()]


def _existing_tables(connection: sa.Connection) -> list[str]:
    """Every ordinary table in `public` except Alembic's own bookkeeping table."""
    return [
        row[0]
        for row in _rows(
            connection,
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND c.relname <> 'alembic_version' "
            "ORDER BY c.relname",
        )
    ]


def _row_digest(connection: sa.Connection, table: str) -> tuple[int, str]:
    """Row count and an `md5` of every row's text, in text order, so row order cannot matter."""
    quoted = connection.dialect.identifier_preparer.quote(table)
    count, digest = connection.execute(
        sa.text(
            f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM {quoted} t"
        )
    ).one()
    return count, digest


def _table_snapshot(connection: sa.Connection, table: str) -> dict[str, Any]:
    quoted = connection.dialect.identifier_preparer.quote(table)
    row_count, row_checksum = _row_digest(connection, table)
    return {
        "relation": _rows(
            connection,
            "SELECT c.relkind, pg_get_userbyid(c.relowner), c.relacl::text, c.reloptions::text, "
            "c.relrowsecurity, c.relpersistence FROM pg_class c WHERE c.oid = CAST(:t AS regclass)",
            t=quoted,
        ),
        "columns": _rows(
            connection,
            "SELECT a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated, "
            "(SELECT collname FROM pg_collation WHERE oid = a.attcollation), a.attacl::text "
            "FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:t AS regclass) AND a.attnum > 0 AND NOT a.attisdropped "
            "ORDER BY a.attnum",
            t=quoted,
        ),
        "constraints": _rows(
            connection,
            "SELECT conname, contype, pg_get_constraintdef(oid), condeferrable, condeferred, convalidated "
            "FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) ORDER BY conname",
            t=quoted,
        ),
        # A foreign key from another table to this one: a new `svc_` constraint pointing here would
        # show up as an extra entry, and as an extra internal trigger below.
        "referenced_by": _rows(
            connection,
            "SELECT conname, conrelid::regclass::text FROM pg_constraint "
            "WHERE confrelid = CAST(:t AS regclass) ORDER BY conname",
            t=quoted,
        ),
        "indexes": _rows(
            connection,
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = :name "
            "ORDER BY indexname",
            name=table,
        ),
        # Named by constraint and function, not by `tgname`: the names of the internal
        # referential-integrity triggers contain an OID that differs between databases.
        "triggers": _rows(
            connection,
            "SELECT c.conname, t.tgfoid::regproc::text, t.tgtype, t.tgisinternal FROM pg_trigger t "
            "LEFT JOIN pg_constraint c ON c.oid = t.tgconstraint "
            "WHERE t.tgrelid = CAST(:t AS regclass) ORDER BY 1, 2, 3, 4",
            t=quoted,
        ),
        "row_count": row_count,
        "row_checksum": row_checksum,
    }


def _hours_snapshot(connection: sa.Connection, tables: list[str]) -> dict[str, dict[str, Any]]:
    return {table: _table_snapshot(connection, table) for table in tables}


@dataclass(frozen=True)
class Inventory:
    """Database-wide object lists, for noticing an object that no table snapshot would reveal."""

    relations: frozenset[tuple[str, str]]
    types: frozenset[tuple[str, str]]
    enum_labels: dict[str, list[str]]
    functions: frozenset[str]
    extensions: frozenset[str]


def _inventory(connection: sa.Connection) -> Inventory:
    enum_labels: dict[str, list[str]] = {}
    for type_name, label in _rows(
        connection,
        "SELECT t.typname, e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
        "JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = 'public' "
        "ORDER BY t.typname, e.enumsortorder",
    ):
        enum_labels.setdefault(type_name, []).append(label)

    return Inventory(
        relations=frozenset(
            _rows(
                connection,
                "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public'",
            )
        ),
        types=frozenset(
            _rows(
                connection,
                "SELECT t.typname, t.typtype FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = 'public'",
            )
        ),
        enum_labels=enum_labels,
        functions=frozenset(
            row[0]
            for row in _rows(
                connection,
                "SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'",
            )
        ),
        extensions=frozenset(row[0] for row in _rows(connection, "SELECT extname FROM pg_extension")),
    )


_SVC_OBJECT_NAME = re.compile(r"^_?(?:ix_|uq_)?svc_")


def _is_svc(name: str) -> bool:
    """Whether a relation or type name belongs to the feature.

    Tables, sequences and primary-key indexes start with `svc_`, other indexes with `ix_svc_` or
    `uq_svc_`, and the array type of a table with an underscore in front.
    """
    return _SVC_OBJECT_NAME.match(name) is not None


def _version(connection: sa.Connection) -> str:
    return str(_scalar(connection, "SELECT version_num FROM alembic_version"))


def _assert_hours_unchanged(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]], context: str
) -> None:
    assert before.keys() == after.keys(), f"{context}: the set of Hours tables changed"
    for table, parts in before.items():
        for part, expected in parts.items():
            assert after[table][part] == expected, f"{context}: {table}.{part} changed"


def _expected_enum_labels(before: Inventory) -> dict[str, list[str]]:
    """The enum labels after 0011: `technician` appended to `user_role`, every other type as it was."""
    return {
        name: [*labels, "technician"] if name == "user_role" else labels
        for name, labels in before.enum_labels.items()
    }


def _assert_only_service_objects_added(
    before: Inventory,
    after: Inventory,
    *,
    tables: set[str] = SVC_TABLES_0011,
    sequences: set[str] = SVC_SEQUENCES_0011,
    enum_labels: Callable[[Inventory], dict[str, list[str]]] = _expected_enum_labels,
) -> None:
    """The database differs by the given `svc_` objects (and the enum change, if any), and nothing else."""
    assert before.relations <= after.relations, "an existing relation disappeared"
    added = after.relations - before.relations
    assert added, "the migration added nothing"
    foreign = sorted(name for name, _ in added if not _is_svc(name))
    assert foreign == [], f"relations outside the svc_ namespace were added: {foreign}"
    assert {name for name, kind in added if kind == "r"} == tables
    assert {name for name, kind in added if kind == "S"} == sequences

    assert before.types <= after.types, "an existing type disappeared"
    assert all(_is_svc(name) for name, _ in after.types - before.types)

    assert after.enum_labels == enum_labels(before)
    assert after.functions == before.functions
    assert after.extensions == before.extensions


# ----------------------------------------------------------------------------- service rows


def _key(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip().casefold()


def _insert_customer_and_ticket(connection: sa.Connection) -> None:
    """One customer and one ticket, numbered with `nextval`, so the sequence has been used."""
    now = datetime(2026, 2, 1, 8, 30, tzinfo=UTC)
    customer_id = _scalar(
        connection,
        "INSERT INTO svc_customers (phone, full_name, full_name_key, city, apartment, password_hash) "
        "VALUES ('0505556677', 'דנה לוי', :key, 'Rosh Tzurim', '12', :hash) RETURNING id",
        key=_key("דנה לוי"),
        hash=hash_password("Initial-Pass-1"),
    )
    category_id = _scalar(connection, "SELECT id FROM svc_categories WHERE name_en = 'Leak or Blockage'")
    connection.execute(
        sa.text(
            "INSERT INTO svc_tickets (ticket_number, customer_id, category_id, city, city_key, apartment, "
            "phone, description, urgent, created_at, created_date, updated_at) "
            "VALUES (nextval('svc_ticket_number_seq'), :customer_id, :category_id, 'Rosh Tzurim', "
            "'rosh tzurim', '12', '0505556677', 'Water under the sink', true, :now, :today, :now)"
        ),
        {"customer_id": customer_id, "category_id": category_id, "now": now, "today": now.date()},
    )


def _use_the_new_lists(connection: sa.Connection) -> None:
    """At 0013 with a ticket and a customer in place: fill every table and column 0013 added.

    One settlement chosen by the customer and the ticket, one part linked to the ticket, a company name
    and a logo key on the branding row. `city` keeps the text it had, as the application will keep it.
    """
    locality_id = _scalar(
        connection,
        "INSERT INTO svc_localities (name_he, name_en, name_he_key, name_en_key) "
        "VALUES ('ראש צורים', 'Rosh Tzurim', :hek, :enk) RETURNING id",
        hek=_key("ראש צורים"),
        enk=_key("Rosh Tzurim"),
    )
    part_id = _scalar(
        connection,
        "INSERT INTO svc_parts (name_he, name_en, name_he_key, name_en_key) "
        "VALUES ('אטם', 'Gasket', :hek, :enk) RETURNING id",
        hek=_key("אטם"),
        enk=_key("Gasket"),
    )
    connection.execute(sa.text("UPDATE svc_tickets SET locality_id = :id"), {"id": locality_id})
    connection.execute(sa.text("UPDATE svc_customers SET locality_id = :id"), {"id": locality_id})
    connection.execute(
        sa.text("INSERT INTO svc_ticket_parts (ticket_id, part_id) SELECT id, :part FROM svc_tickets"),
        {"part": part_id},
    )
    connection.execute(
        sa.text(
            "UPDATE svc_branding SET company_name = 'Acme Service', "
            f"logo_key = 'service-tickets/branding/{uuid.uuid4()}.png'"
        )
    )


def _sequence_state(connection: sa.Connection, name: str) -> tuple[int, bool]:
    # Reads the sequence's own row. `nextval` would consume a number and change what is measured.
    row = connection.execute(sa.text(f"SELECT last_value, is_called FROM {name}")).one()
    return row.last_value, row.is_called


def _service_state(connection: sa.Connection) -> dict[str, Any]:
    """Row counts and checksums of every existing `svc_` table, and the state of every `svc_` sequence.

    Read from the catalog, so the same helper serves every revision from 0011 on (0013 adds tables).
    """
    tables = sorted(row[0] for row in _rows(connection, _SERVICE_RELATIONS_SQL, kind="r"))
    sequences = sorted(row[0] for row in _rows(connection, _SERVICE_RELATIONS_SQL, kind="S"))
    return {
        "tables": {table: _row_digest(connection, table) for table in tables},
        "sequences": {name: _sequence_state(connection, name) for name in sequences},
    }


# ----------------------------------------------------------------------------------- the tests


def test_upgrade_leaves_every_existing_hours_table_unchanged(hours_database: Scratch) -> None:
    """R20.1, R25.3: catalog and row checksums of every Hours table are identical at 0010 and 0011."""
    with hours_database.engine.connect() as connection:
        tables = _existing_tables(connection)
        before = _hours_snapshot(connection, tables)
        inventory_before = _inventory(connection)
        assert _version(connection) == HOURS_REVISION

    # The comparison must not be vacuous: the list comes from the catalog, covers the Hours tables
    # the design names, and every one of them holds rows.
    assert len(tables) >= 20
    assert {"users", "employees", "clients", "sites", "time_entries", "settings", "change_logs"} <= set(
        tables
    )
    assert [table for table in tables if before[table]["row_count"] == 0] == []

    command.upgrade(hours_database.config, SERVICE_REVISION)

    with hours_database.engine.connect() as connection:
        assert _version(connection) == SERVICE_REVISION
        after_tables = _existing_tables(connection)
        assert [t for t in after_tables if not t.startswith(SVC_PREFIX)] == tables
        assert {t for t in after_tables if t.startswith(SVC_PREFIX)} == SVC_TABLES_0011
        after = _hours_snapshot(connection, tables)
        inventory_after = _inventory(connection)

    _assert_hours_unchanged(before, after, "upgrade 0010 to 0011")
    _assert_only_service_objects_added(inventory_before, inventory_after)


def test_the_upgrade_appends_the_technician_value_to_user_role(hours_database: Scratch) -> None:
    """The one permitted change to an existing object: the value is appended, nothing is reordered."""
    with hours_database.engine.connect() as connection:
        before = _inventory(connection).enum_labels["user_role"]
    assert "technician" not in before

    command.upgrade(hours_database.config, SERVICE_REVISION)

    with hours_database.engine.connect() as connection:
        assert _inventory(connection).enum_labels["user_role"] == [*before, "technician"]


def test_no_foreign_key_crosses_the_service_ticket_boundary(migrated_engine: Engine) -> None:
    """R20.2, both directions: a link either way would couple the feature to the Hours schema."""
    with migrated_engine.connect() as connection:
        pairs = _rows(
            connection,
            "SELECT child.relname, parent.relname FROM pg_constraint con "
            "JOIN pg_class child ON child.oid = con.conrelid "
            "JOIN pg_class parent ON parent.oid = con.confrelid "
            "WHERE con.contype = 'f'",
        )

    crossing = [
        (child, parent)
        for child, parent in pairs
        if child.startswith(SVC_PREFIX) != parent.startswith(SVC_PREFIX)
    ]
    assert crossing == []
    # Both sides must be populated, or the assertion above says nothing.
    assert any(child.startswith(SVC_PREFIX) and parent.startswith(SVC_PREFIX) for child, parent in pairs)
    assert any(
        not child.startswith(SVC_PREFIX) and not parent.startswith(SVC_PREFIX) for child, parent in pairs
    )


def test_seed_content_matches_the_design(scratch: Scratch) -> None:
    """R19.14, R19.15: six categories in order, seven labels, settings (1, true, true)."""
    command.upgrade(scratch.config, SERVICE_REVISION)

    with scratch.engine.connect() as connection:
        categories = _rows(
            connection,
            "SELECT name_he, name_en, name_he_key, name_en_key, is_active, seq "
            "FROM svc_categories ORDER BY seq",
        )
        labels = _rows(connection, "SELECT field_key, label_he, label_en FROM svc_field_labels")
        settings = _rows(
            connection, "SELECT id, description_required, photo_upload_enabled FROM svc_settings"
        )

    assert [(row[0], row[1]) for row in categories] == SEED_CATEGORIES
    assert all(row[4] is True for row in categories)
    sequence_numbers = [row[5] for row in categories]
    assert sequence_numbers == sorted(set(sequence_numbers)), "seq must be strictly increasing"
    assert [(row[2], row[3]) for row in categories] == [(_key(he), _key(en)) for he, en in SEED_CATEGORIES]
    assert len(labels) == len(SEED_FIELD_LABELS) == 7
    assert {key: (he, en) for key, he, en in labels} == SEED_FIELD_LABELS
    assert settings == [(1, True, True)]


def test_a_rerun_against_existing_tables_changes_nothing(scratch: Scratch) -> None:
    """R19.17: nothing added back, renamed, reactivated or reset, and no ticket number consumed."""
    command.upgrade(scratch.config, SERVICE_REVISION)

    renamed = ("חשמלאי", "Electrician")
    with scratch.engine.begin() as connection:
        _insert_customer_and_ticket(connection)
        connection.execute(
            sa.text(
                "UPDATE svc_categories SET name_he = :he, name_en = :en, name_he_key = :hek, "
                "name_en_key = :enk WHERE name_en = 'Electrical'"
            ),
            {"he": renamed[0], "en": renamed[1], "hek": _key(renamed[0]), "enk": _key(renamed[1])},
        )
        connection.execute(sa.text("DELETE FROM svc_categories WHERE name_en = 'General'"))
        connection.execute(sa.text("UPDATE svc_categories SET is_active = false WHERE name_en = 'Plumbing'"))
        connection.execute(
            sa.text(
                "UPDATE svc_field_labels SET label_he = 'יישוב', label_en = 'Town' WHERE field_key = 'city'"
            )
        )
        connection.execute(
            sa.text("UPDATE svc_settings SET description_required = false, photo_upload_enabled = false")
        )

    with scratch.engine.connect() as connection:
        before = _service_state(connection)
    assert before["sequences"]["svc_ticket_number_seq"] == (10000, True), "the ticket used number 10000"

    command.stamp(scratch.config, HOURS_REVISION)
    with scratch.engine.connect() as connection:
        assert _version(connection) == HOURS_REVISION
    command.upgrade(scratch.config, SERVICE_REVISION)

    with scratch.engine.connect() as connection:
        assert _version(connection) == SERVICE_REVISION
        after = _service_state(connection)
        categories = _rows(connection, "SELECT name_en, is_active FROM svc_categories ORDER BY seq")
        label = _rows(connection, "SELECT label_he, label_en FROM svc_field_labels WHERE field_key = 'city'")
        settings = _rows(
            connection, "SELECT id, description_required, photo_upload_enabled FROM svc_settings"
        )

    # Every row of every svc_ table, and every sequence, is exactly as the edits left it.
    assert after == before
    # The same facts, one by one, so a failure names what broke. "General" stays deleted and no
    # category is duplicated.
    assert categories == [
        ("Leak or Blockage", True),
        ("Dampness or Moisture", True),
        ("Electrician", True),
        ("Plumbing", False),
        ("Walls and Finishing", True),
    ]
    assert label == [("יישוב", "Town")]
    assert settings == [(1, False, False)]


@pytest.mark.parametrize("start", [SERVICE_REVISION, STAFF_OPENED_REVISION, LISTS_REVISION])
def test_downgrade_removes_only_the_service_objects(hours_database: Scratch, start: str) -> None:
    """R25.9: tables and sequence gone, `technician` kept, every existing table as it was at 0010.

    Taken from 0011, 0012 and 0013: the back-out target that removes the whole feature stays 0010.
    """
    with hours_database.engine.connect() as connection:
        tables = _existing_tables(connection)
        hours_before = _hours_snapshot(connection, tables)
        inventory_before = _inventory(connection)

    command.upgrade(hours_database.config, start)
    with hours_database.engine.begin() as connection:
        _insert_customer_and_ticket(connection)  # the downgrade must also work with data in place
        if start in (STAFF_OPENED_REVISION, LISTS_REVISION):
            connection.execute(sa.text(f"UPDATE svc_tickets SET {OPENED_BY_COLUMN} = 'tech.one'"))
        if start == LISTS_REVISION:
            _use_the_new_lists(connection)  # lists, links and branding in place too
    command.downgrade(hours_database.config, HOURS_REVISION)

    with hours_database.engine.connect() as connection:
        assert _version(connection) == HOURS_REVISION
        assert _existing_tables(connection) == tables
        hours_after = _hours_snapshot(connection, tables)
        inventory_after = _inventory(connection)

    _assert_hours_unchanged(hours_before, hours_after, "downgrade 0011 to 0010")
    assert [name for name, _ in inventory_after.relations if _is_svc(name)] == [], "svc_ relations remain"
    assert [name for name, _ in inventory_after.types if _is_svc(name)] == [], "svc_ types remain"
    assert inventory_after.relations == inventory_before.relations
    assert inventory_after.types == inventory_before.types
    assert inventory_after.functions == inventory_before.functions
    assert inventory_after.extensions == inventory_before.extensions
    # `technician` stays: PostgreSQL cannot remove an enum value without rebuilding the type.
    assert inventory_after.enum_labels == _expected_enum_labels(inventory_before)


def test_downgrade_to_base_leaves_no_tables_and_no_enum_types(scratch: Scratch) -> None:
    command.upgrade(scratch.config, "head")
    command.downgrade(scratch.config, "base")

    with scratch.engine.connect() as connection:
        remaining = _existing_tables(connection)
        enum_types = _rows(
            connection,
            "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE t.typtype = 'e' AND n.nspname = 'public'",
        )
        sequences = _rows(
            connection,
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'S'",
        )

    assert remaining == [], f"downgrade left tables behind: {remaining}"
    assert enum_types == [], f"downgrade left enum types behind: {enum_types}"
    assert sequences == [], f"downgrade left sequences behind: {sequences}"


def _diff_table(diff: Any) -> str:
    """The table an autogenerate diff entry is about. A `modify_*` group arrives as a list."""
    if isinstance(diff, list):
        return _diff_table(diff[0])
    match diff[0]:
        case "add_table" | "remove_table":
            return str(diff[1].name)
        case "add_column" | "remove_column":
            return str(diff[2])
        case "add_index" | "remove_index" | "add_constraint" | "remove_constraint" | "add_fk" | "remove_fk":
            return str(diff[1].table.name)
        case _:
            return str(diff[2]) if len(diff) > 2 else ""


def _is_documented_uuid_default_omission(diff: Any) -> bool:
    """`id` columns: the database default is `gen_random_uuid()`, the model has only a Python default.

    `app.service_tickets.models` leaves the server default out on purpose, as `app.models.user` does:
    it is not valid on the SQLite engine the unit tests build from `Base.metadata`, and the Python
    default covers the ORM. The migration carries it for every other writer.
    """
    entry = diff[0] if isinstance(diff, list) else diff
    if entry[0] != "modify_default" or entry[3] != "id":
        return False
    existing, declared = entry[5], entry[6]
    return declared is None and "gen_random_uuid()" in str(getattr(existing, "arg", existing))


def test_models_and_schema_agree_for_the_service_tables(migrated_engine: Engine) -> None:
    """Autogenerate proposes nothing for any `svc_` table: models and migration say the same thing."""
    assert {name for name in Base.metadata.tables if name.startswith(SVC_PREFIX)} == SVC_TABLES

    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        diffs = compare_metadata(context, Base.metadata)

    service_diffs = [diff for diff in diffs if _diff_table(diff).startswith(SVC_PREFIX)]
    assert [diff for diff in service_diffs if not _is_documented_uuid_default_omission(diff)] == []
    # The exception is the one the models document, not a blanket pass: exactly the uuid keys that have
    # a server default (the two list tables of 0013 included; `svc_ticket_parts` and `svc_branding` have
    # no uuid key).
    assert sorted(_diff_table(diff) for diff in service_diffs) == [
        "svc_categories",
        "svc_customers",
        "svc_history_entries",
        "svc_localities",
        "svc_parts",
        "svc_tickets",
    ]


def test_the_application_role_holds_the_designed_grants(migrated_engine: Engine) -> None:
    """`hours_app` has the business-table privileges on each svc_ table and nextval on each sequence."""
    with migrated_engine.connect() as connection:
        tables = {row[0] for row in _rows(connection, _SERVICE_RELATIONS_SQL, kind="r")}
        sequences = {row[0] for row in _rows(connection, _SERVICE_RELATIONS_SQL, kind="S")}
        assert tables == SVC_TABLES
        assert sequences == SVC_SEQUENCES

        missing: list[str] = []
        for table in sorted(tables):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                granted = _scalar(
                    connection, "SELECT has_table_privilege('hours_app', :t, :p)", t=table, p=privilege
                )
                if not granted:
                    missing.append(f"{privilege} on {table}")
        for sequence in sorted(sequences):
            for privilege in ("USAGE", "SELECT"):
                granted = _scalar(
                    connection, "SELECT has_sequence_privilege('hours_app', :s, :p)", s=sequence, p=privilege
                )
                if not granted:
                    missing.append(f"{privilege} on {sequence}")

    assert missing == []


def test_upgrade_succeeds_when_the_service_tables_already_exist(hours_database: Scratch) -> None:
    """Deploy-order safety: a database restored to 0010 that still holds the svc_ tables upgrades cleanly."""
    with hours_database.engine.connect() as connection:
        tables = _existing_tables(connection)
        hours_before = _hours_snapshot(connection, tables)

    command.upgrade(hours_database.config, SERVICE_REVISION)
    with hours_database.engine.begin() as connection:
        _insert_customer_and_ticket(connection)
    with hours_database.engine.connect() as connection:
        service_before = _service_state(connection)

    # The restored database: svc_ objects present, `alembic_version` back at 0010.
    command.stamp(hours_database.config, HOURS_REVISION)
    command.upgrade(hours_database.config, SERVICE_REVISION)

    with hours_database.engine.connect() as connection:
        assert _version(connection) == SERVICE_REVISION
        service_after = _service_state(connection)
        assert _scalar(connection, "SELECT count(*) FROM svc_categories") == len(SEED_CATEGORIES)
        assert _scalar(connection, "SELECT count(*) FROM svc_field_labels") == len(SEED_FIELD_LABELS)
        assert _scalar(connection, "SELECT count(*) FROM svc_settings") == 1
        hours_after = _hours_snapshot(connection, tables)

    assert service_after == service_before
    _assert_hours_unchanged(hours_before, hours_after, "re-upgrade over existing svc_ tables")


# --------------------------------------------------------------- 0012: staff-opened tickets


def _ticket_columns(connection: sa.Connection) -> list[tuple[Any, ...]]:
    """Every `svc_tickets` column as (name, type, nullable, default), in table order."""
    return _rows(
        connection,
        "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'svc_tickets' ORDER BY ordinal_position",
    )


def test_the_migration_chain_runs_from_0010_through_0011_and_0012_to_0013(scratch: Scratch) -> None:
    """The head is 0013 on 0012 on 0011 on 0010, and a fresh upgrade to head lands on it."""
    script = ScriptDirectory.from_config(scratch.config)
    assert script.get_heads() == [LISTS_REVISION]
    for revision_id, parent in (
        (LISTS_REVISION, STAFF_OPENED_REVISION),
        (STAFF_OPENED_REVISION, SERVICE_REVISION),
        (SERVICE_REVISION, HOURS_REVISION),
    ):
        revision = script.get_revision(revision_id)
        assert revision is not None
        assert revision.down_revision == parent

    command.upgrade(scratch.config, "head")
    with scratch.engine.connect() as connection:
        assert _version(connection) == LISTS_REVISION


def test_upgrade_adds_only_a_nullable_text_column_and_keeps_the_tickets(hours_database: Scratch) -> None:
    """R26.4: `opened_by_username` is nullable text without a default, and every stored ticket reads NULL."""
    command.upgrade(hours_database.config, SERVICE_REVISION)
    with hours_database.engine.begin() as connection:
        _insert_customer_and_ticket(connection)
    with hours_database.engine.connect() as connection:
        tables = _existing_tables(connection)
        columns_before = _ticket_columns(connection)
        inventory_before = _inventory(connection)
        ticket_before = _rows(connection, "SELECT id, ticket_number, description FROM svc_tickets")
    assert OPENED_BY_COLUMN not in [row[0] for row in columns_before]

    command.upgrade(hours_database.config, STAFF_OPENED_REVISION)

    with hours_database.engine.connect() as connection:
        assert _version(connection) == STAFF_OPENED_REVISION
        columns_after = _ticket_columns(connection)
        inventory_after = _inventory(connection)
        ticket_after = _rows(connection, "SELECT id, ticket_number, description FROM svc_tickets")
        opened_by = _rows(connection, f"SELECT {OPENED_BY_COLUMN} FROM svc_tickets")
        assert _existing_tables(connection) == tables

    assert columns_after == [*columns_before, (OPENED_BY_COLUMN, "text", "YES", None)]
    assert ticket_after == ticket_before
    assert opened_by == [(None,)]
    # No new relation, type, function or extension: the migration adds a column and nothing else.
    assert inventory_after == inventory_before


def test_upgrade_to_0012_leaves_every_existing_hours_table_unchanged(hours_database: Scratch) -> None:
    """No Hours table changes between 0011 and 0012."""
    command.upgrade(hours_database.config, SERVICE_REVISION)
    with hours_database.engine.connect() as connection:
        tables = [name for name in _existing_tables(connection) if not name.startswith(SVC_PREFIX)]
        before = _hours_snapshot(connection, tables)

    command.upgrade(hours_database.config, STAFF_OPENED_REVISION)

    with hours_database.engine.connect() as connection:
        after = _hours_snapshot(connection, tables)
    _assert_hours_unchanged(before, after, "upgrade 0011 to 0012")


def test_downgrade_to_0011_removes_only_the_column(hours_database: Scratch) -> None:
    """R26.4: the column and its values go; every ticket, customer, column and Hours table stays."""
    command.upgrade(hours_database.config, SERVICE_REVISION)
    with hours_database.engine.begin() as connection:
        _insert_customer_and_ticket(connection)
    with hours_database.engine.connect() as connection:
        tables = _existing_tables(connection)
        hours_tables = [name for name in tables if not name.startswith(SVC_PREFIX)]
        columns_at_0011 = _ticket_columns(connection)
        service_at_0011 = _service_state(connection)
        hours_before = _hours_snapshot(connection, hours_tables)
        inventory_before = _inventory(connection)

    command.upgrade(hours_database.config, STAFF_OPENED_REVISION)
    with hours_database.engine.begin() as connection:
        connection.execute(sa.text(f"UPDATE svc_tickets SET {OPENED_BY_COLUMN} = 'tech.one'"))

    command.downgrade(hours_database.config, SERVICE_REVISION)

    with hours_database.engine.connect() as connection:
        assert _version(connection) == SERVICE_REVISION
        assert _existing_tables(connection) == tables
        assert _ticket_columns(connection) == columns_at_0011
        # Every svc_ row checksum and every sequence equals what it was before 0012 ran, so the ticket
        # and the customer are exactly as they were and no ticket number was consumed.
        assert _service_state(connection) == service_at_0011
        assert _scalar(connection, "SELECT count(*) FROM svc_tickets") == 1
        hours_after = _hours_snapshot(connection, hours_tables)
        inventory_after = _inventory(connection)

    _assert_hours_unchanged(hours_before, hours_after, "downgrade 0012 to 0011")
    assert inventory_after == inventory_before


def test_upgrade_to_0012_succeeds_when_the_column_already_exists(hours_database: Scratch) -> None:
    """Deploy-order safety: a database restored from a 0012-era dump with an older version upgrades."""
    command.upgrade(hours_database.config, STAFF_OPENED_REVISION)
    with hours_database.engine.begin() as connection:
        _insert_customer_and_ticket(connection)
        connection.execute(sa.text(f"UPDATE svc_tickets SET {OPENED_BY_COLUMN} = 'tech.one'"))
    with hours_database.engine.connect() as connection:
        service_before = _service_state(connection)

    command.stamp(hours_database.config, SERVICE_REVISION)
    command.upgrade(hours_database.config, STAFF_OPENED_REVISION)

    with hours_database.engine.connect() as connection:
        assert _version(connection) == STAFF_OPENED_REVISION
        assert _rows(connection, f"SELECT {OPENED_BY_COLUMN} FROM svc_tickets") == [("tech.one",)]
        assert _service_state(connection) == service_before


# --------------------------------------------------- 0013: lists, branding and part links


def _table_columns(connection: sa.Connection, table: str) -> list[tuple[Any, ...]]:
    """Every column of `table` as (name, type, nullable, default), in table order."""
    return _rows(
        connection,
        "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t ORDER BY ordinal_position",
        t=table,
    )


def _service_snapshots(connection: sa.Connection, tables: set[str]) -> dict[str, dict[str, Any]]:
    return {table: _table_snapshot(connection, table) for table in sorted(tables)}


def test_upgrade_to_0013_adds_only_the_designed_objects(hours_database: Scratch) -> None:
    """R28.6, R29.6, R29.7, R30.1, R30.4: four tables, two columns, an empty branding row, nothing else."""
    command.upgrade(hours_database.config, STAFF_OPENED_REVISION)
    with hours_database.engine.begin() as connection:
        _insert_customer_and_ticket(connection)
    with hours_database.engine.connect() as connection:
        tables = _existing_tables(connection)
        hours_tables = [name for name in tables if not name.startswith(SVC_PREFIX)]
        hours_before = _hours_snapshot(connection, hours_tables)
        inventory_before = _inventory(connection)
        tickets_before = _table_columns(connection, "svc_tickets")
        customers_before = _table_columns(connection, "svc_customers")
        categories_columns = _table_columns(connection, "svc_categories")
        rows_before = (
            _rows(connection, "SELECT id, ticket_number, city, city_key, description FROM svc_tickets"),
            _rows(connection, "SELECT id, phone, full_name, city FROM svc_customers"),
        )
        untouched = sorted(SVC_TABLES_0011 - {"svc_tickets", "svc_customers"})
        other_before = {table: _row_digest(connection, table) for table in untouched}
    assert LOCALITY_COLUMN not in [row[0] for row in tickets_before + customers_before]

    command.upgrade(hours_database.config, LISTS_REVISION)

    with hours_database.engine.connect() as connection:
        assert _version(connection) == LISTS_REVISION
        after_tables = _existing_tables(connection)
        assert [name for name in after_tables if not name.startswith(SVC_PREFIX)] == hours_tables
        assert {name for name in after_tables if name.startswith(SVC_PREFIX)} == SVC_TABLES
        inventory_after = _inventory(connection)
        hours_after = _hours_snapshot(connection, hours_tables)

        # Each new column: nullable uuid, no default, appended after the existing columns.
        locality = (LOCALITY_COLUMN, "uuid", "YES", None)
        assert _table_columns(connection, "svc_tickets") == [*tickets_before, locality]
        assert _table_columns(connection, "svc_customers") == [*customers_before, locality]

        # The two list tables have exactly the shape of `svc_categories`.
        assert _table_columns(connection, "svc_localities") == categories_columns
        assert _table_columns(connection, "svc_parts") == categories_columns
        for table in ("svc_categories", "svc_localities", "svc_parts"):
            assert _rows(
                connection,
                "SELECT a.attname, a.attidentity::text FROM pg_attribute a "
                "WHERE a.attrelid = CAST(:t AS regclass) AND a.attname = 'seq'",
                t=table,
            ) == [("seq", "a")]
        assert _table_columns(connection, "svc_ticket_parts") == [
            ("ticket_id", "uuid", "NO", None),
            ("part_id", "uuid", "NO", None),
        ]
        assert _table_columns(connection, "svc_branding") == [
            ("id", "smallint", "NO", None),
            ("company_name", "text", "YES", None),
            ("logo_key", "text", "YES", None),
            ("updated_at", "timestamp with time zone", "NO", "now()"),
        ]

        # No seed rows in the lists, the empty branding row, and no link.
        for table in ("svc_localities", "svc_parts", "svc_ticket_parts"):
            assert _scalar(connection, f"SELECT count(*) FROM {table}") == 0
        assert _rows(connection, "SELECT id, company_name, logo_key FROM svc_branding") == [(1, None, None)]

        # Every stored ticket and customer is as it was, with a NULL settlement.
        assert (
            _rows(connection, "SELECT id, ticket_number, city, city_key, description FROM svc_tickets")
            == rows_before[0]
        )
        assert _rows(connection, "SELECT id, phone, full_name, city FROM svc_customers") == rows_before[1]
        assert _rows(connection, "SELECT locality_id FROM svc_tickets") == [(None,)]
        assert _rows(connection, "SELECT locality_id FROM svc_customers") == [(None,)]
        assert {table: _row_digest(connection, table) for table in untouched} == other_before

    _assert_hours_unchanged(hours_before, hours_after, "upgrade 0012 to 0013")
    _assert_only_service_objects_added(
        inventory_before,
        inventory_after,
        tables=SVC_TABLES_0013,
        sequences=SVC_SEQUENCES_0013,
        enum_labels=lambda before: before.enum_labels,
    )


def test_the_new_objects_carry_the_designed_names_and_rules(migrated_engine: Engine) -> None:
    """Constraint and index names equal what the models declare; every foreign key has its delete rule."""
    with migrated_engine.connect() as connection:
        constraints = set(
            _rows(
                connection,
                "SELECT conrelid::regclass::text, conname, contype::text, "
                "CASE WHEN contype = 'f' THEN confdeltype::text END FROM pg_constraint "
                "WHERE contype <> 'n' AND (conrelid::regclass::text IN "
                "('svc_localities', 'svc_parts', 'svc_ticket_parts', 'svc_branding') "
                "OR conname IN ('fk_svc_tickets_locality_id_svc_localities', "
                "'fk_svc_customers_locality_id_svc_localities'))",
            )
        )
        indexes = set(
            _rows(
                connection,
                "SELECT tablename, indexname FROM pg_indexes WHERE schemaname = 'public' AND "
                "(tablename IN ('svc_localities', 'svc_parts', 'svc_ticket_parts', 'svc_branding') "
                "OR indexname IN ('ix_svc_tickets_locality_id', 'ix_svc_customers_locality_id'))",
            )
        )
        locality_index_definitions = _rows(
            connection,
            "SELECT indexdef FROM pg_indexes WHERE indexname IN "
            "('ix_svc_tickets_locality_id', 'ix_svc_customers_locality_id') ORDER BY indexname",
        )

    expected: set[tuple[str, str, str, str | None]] = {
        ("svc_ticket_parts", "svc_ticket_parts_pkey", "p", None),
        ("svc_ticket_parts", "fk_svc_ticket_parts_ticket_id_svc_tickets", "f", "c"),  # CASCADE
        ("svc_ticket_parts", "fk_svc_ticket_parts_part_id_svc_parts", "f", "r"),  # RESTRICT
        ("svc_branding", "svc_branding_pkey", "p", None),
        ("svc_branding", "uq_svc_branding_logo_key", "u", None),
        ("svc_branding", "ck_svc_branding_id_is_one", "c", None),
        ("svc_branding", "ck_svc_branding_company_name_length", "c", None),
        ("svc_tickets", "fk_svc_tickets_locality_id_svc_localities", "f", "r"),
        ("svc_customers", "fk_svc_customers_locality_id_svc_localities", "f", "r"),
    }
    for table in ("svc_localities", "svc_parts"):
        expected |= {
            (table, f"{table}_pkey", "p", None),
            (table, f"uq_{table}_seq", "u", None),
            (table, f"uq_{table}_name_he_key", "u", None),
            (table, f"uq_{table}_name_en_key", "u", None),
            (table, f"ck_{table}_name_he_length", "c", None),
            (table, f"ck_{table}_name_en_length", "c", None),
        }
    assert constraints == expected

    assert indexes == {
        ("svc_localities", "svc_localities_pkey"),
        ("svc_localities", "uq_svc_localities_seq"),
        ("svc_localities", "uq_svc_localities_name_he_key"),
        ("svc_localities", "uq_svc_localities_name_en_key"),
        ("svc_parts", "svc_parts_pkey"),
        ("svc_parts", "uq_svc_parts_seq"),
        ("svc_parts", "uq_svc_parts_name_he_key"),
        ("svc_parts", "uq_svc_parts_name_en_key"),
        ("svc_ticket_parts", "svc_ticket_parts_pkey"),
        ("svc_ticket_parts", "ix_svc_ticket_parts_part_id"),
        ("svc_branding", "svc_branding_pkey"),
        ("svc_branding", "uq_svc_branding_logo_key"),
        ("svc_customers", "ix_svc_customers_locality_id"),
        ("svc_tickets", "ix_svc_tickets_locality_id"),
    }
    assert all(row[0].endswith("(locality_id)") for row in locality_index_definitions)


def _integrity_failure(engine: Engine, sql: str, **params: Any) -> None:
    """The statement, run alone in its own transaction, is refused by a constraint."""
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(sa.text(sql), params)


def test_the_new_tables_enforce_their_rules(scratch: Scratch) -> None:
    """Name lengths, one branding row, the composite key, CASCADE for a ticket and RESTRICT elsewhere."""
    command.upgrade(scratch.config, "head")
    insert_name = (
        "INSERT INTO {table} (name_he, name_en, name_he_key, name_en_key) VALUES (:he, :en, :hek, :enk)"
    )

    def name_params(he: str, en: str) -> dict[str, str]:
        return {"he": he, "en": en, "hek": _key(he), "enk": _key(en)}

    # Settlement names run 1 to 100 characters, part names 1 to 60, in each language.
    for table, longest in (("svc_localities", 100), ("svc_parts", 60)):
        sql = insert_name.format(table=table)
        for he, en in (("א" * (longest + 1), "ok"), ("ok", "b" * (longest + 1)), ("", "ok"), ("ok", "")):
            _integrity_failure(scratch.engine, sql, **name_params(he, en))
        with scratch.engine.begin() as connection:
            connection.execute(sa.text(sql), name_params("א" * longest, "b" * longest))
            connection.execute(sa.text(sql), name_params("x", "y"))
        # The casefolded key is unique per language.
        _integrity_failure(scratch.engine, sql, **name_params("x", "other"))
        _integrity_failure(scratch.engine, sql, **name_params("other", "y"))

    # One branding row, with a name of 1 to 100 characters or none.
    _integrity_failure(scratch.engine, "INSERT INTO svc_branding (id) VALUES (2)")
    _integrity_failure(scratch.engine, "INSERT INTO svc_branding (id) VALUES (1)")
    _integrity_failure(scratch.engine, "UPDATE svc_branding SET company_name = ''")
    _integrity_failure(scratch.engine, "UPDATE svc_branding SET company_name = :name", name="n" * 101)
    with scratch.engine.begin() as connection:
        connection.execute(sa.text("UPDATE svc_branding SET company_name = :name"), {"name": "n" * 100})
        connection.execute(sa.text("UPDATE svc_branding SET company_name = NULL, logo_key = 'k'"))
    assert _scalar_engine(scratch.engine, "SELECT count(*) FROM svc_branding") == 1

    # Links: one row per (ticket, part); a part or a settlement in use cannot be deleted; a ticket takes
    # its links with it.
    with scratch.engine.begin() as connection:
        _insert_customer_and_ticket(connection)
        _use_the_new_lists(connection)
    ticket_id = _scalar_engine(scratch.engine, "SELECT id FROM svc_tickets")
    part_id = _scalar_engine(scratch.engine, "SELECT part_id FROM svc_ticket_parts")
    locality_id = _scalar_engine(scratch.engine, "SELECT locality_id FROM svc_tickets")
    _integrity_failure(
        scratch.engine,
        "INSERT INTO svc_ticket_parts (ticket_id, part_id) VALUES (:t, :p)",
        t=ticket_id,
        p=part_id,
    )
    _integrity_failure(scratch.engine, "DELETE FROM svc_parts WHERE id = :p", p=part_id)
    _integrity_failure(scratch.engine, "DELETE FROM svc_localities WHERE id = :loc", loc=locality_id)
    # An unknown settlement is refused too.
    _integrity_failure(scratch.engine, "UPDATE svc_customers SET locality_id = gen_random_uuid()")

    # With the ticket gone the settlement is still held by the customer, and the part is free again.
    with scratch.engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM svc_tickets WHERE id = :t"), {"t": ticket_id})
    assert _scalar_engine(scratch.engine, "SELECT count(*) FROM svc_ticket_parts") == 0
    _integrity_failure(scratch.engine, "DELETE FROM svc_localities WHERE id = :loc", loc=locality_id)
    with scratch.engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM svc_parts WHERE id = :p"), {"p": part_id})


def _scalar_engine(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as connection:
        return _scalar(connection, sql, **params)


def test_downgrade_to_0012_restores_the_exact_0012_state(hours_database: Scratch) -> None:
    """R28.6, R30.4: only what 0013 added goes; every old svc_ object, row and Hours table is unchanged."""
    command.upgrade(hours_database.config, STAFF_OPENED_REVISION)
    with hours_database.engine.begin() as connection:
        _insert_customer_and_ticket(connection)
        connection.execute(sa.text(f"UPDATE svc_tickets SET {OPENED_BY_COLUMN} = 'tech.one'"))
    with hours_database.engine.connect() as connection:
        tables = _existing_tables(connection)
        hours_tables = [name for name in tables if not name.startswith(SVC_PREFIX)]
        hours_before = _hours_snapshot(connection, hours_tables)
        snapshots_before = _service_snapshots(connection, SVC_TABLES_0011)
        service_before = _service_state(connection)
        inventory_before = _inventory(connection)

    command.upgrade(hours_database.config, LISTS_REVISION)
    with hours_database.engine.begin() as connection:
        _use_the_new_lists(connection)  # the downgrade must work with every new object in use
    command.downgrade(hours_database.config, STAFF_OPENED_REVISION)

    with hours_database.engine.connect() as connection:
        assert _version(connection) == STAFF_OPENED_REVISION
        assert _existing_tables(connection) == tables
        # Columns, constraints, indexes, triggers, grants, row counts and row checksums of every old
        # svc_ table, and every sequence: exactly what they were before 0013 ran.
        assert _service_snapshots(connection, SVC_TABLES_0011) == snapshots_before
        assert _service_state(connection) == service_before
        assert _scalar(connection, "SELECT count(*) FROM svc_tickets") == 1
        assert _scalar(connection, "SELECT count(*) FROM svc_customers") == 1
        assert _rows(connection, f"SELECT {OPENED_BY_COLUMN} FROM svc_tickets") == [("tech.one",)]
        hours_after = _hours_snapshot(connection, hours_tables)
        inventory_after = _inventory(connection)

    _assert_hours_unchanged(hours_before, hours_after, "downgrade 0013 to 0012")
    assert inventory_after == inventory_before


def test_a_rerun_of_0013_over_existing_objects_changes_nothing(hours_database: Scratch) -> None:
    """Deploy-order safety: nothing reset, duplicated or re-seeded, and the branding row is kept."""
    command.upgrade(hours_database.config, LISTS_REVISION)
    with hours_database.engine.begin() as connection:
        _insert_customer_and_ticket(connection)
        _use_the_new_lists(connection)
    with hours_database.engine.connect() as connection:
        tables = _existing_tables(connection)
        hours_tables = [name for name in tables if not name.startswith(SVC_PREFIX)]
        hours_before = _hours_snapshot(connection, hours_tables)
        snapshots_before = _service_snapshots(connection, SVC_TABLES)
        service_before = _service_state(connection)
        inventory_before = _inventory(connection)
        branding_before = _rows(connection, "SELECT id, company_name, logo_key, updated_at FROM svc_branding")
    assert branding_before[0][1] == "Acme Service"

    # The restored database: every 0013 object present, `alembic_version` back at 0012.
    command.stamp(hours_database.config, STAFF_OPENED_REVISION)
    with hours_database.engine.connect() as connection:
        assert _version(connection) == STAFF_OPENED_REVISION
    command.upgrade(hours_database.config, LISTS_REVISION)

    with hours_database.engine.connect() as connection:
        assert _version(connection) == LISTS_REVISION
        assert _existing_tables(connection) == tables
        assert _service_snapshots(connection, SVC_TABLES) == snapshots_before
        assert _service_state(connection) == service_before
        assert _rows(connection, "SELECT id, company_name, logo_key, updated_at FROM svc_branding") == (
            branding_before
        )
        assert _scalar(connection, "SELECT count(*) FROM svc_localities") == 1
        assert _scalar(connection, "SELECT count(*) FROM svc_parts") == 1
        assert _scalar(connection, "SELECT count(*) FROM svc_ticket_parts") == 1
        hours_after = _hours_snapshot(connection, hours_tables)
        inventory_after = _inventory(connection)

    _assert_hours_unchanged(hours_before, hours_after, "re-run of 0013")
    assert inventory_after == inventory_before


def test_the_0013_grants_match_the_old_tables(scratch: Scratch) -> None:
    """Dropped and re-created tables are granted again: a re-run after a restore leaves the grants whole."""
    command.upgrade(scratch.config, "head")
    with scratch.engine.begin() as connection:
        # Take the grants away, as a restore into a fresh role setup could, then re-run 0013.
        for table in sorted(SVC_TABLES_0013):
            connection.execute(sa.text(f"REVOKE ALL ON {table} FROM hours_app"))
        for sequence in sorted(SVC_SEQUENCES_0013):
            connection.execute(sa.text(f"REVOKE ALL ON SEQUENCE {sequence} FROM hours_app"))
    command.stamp(scratch.config, STAFF_OPENED_REVISION)
    command.upgrade(scratch.config, LISTS_REVISION)

    with scratch.engine.connect() as connection:
        for table in sorted(SVC_TABLES_0013 | {"svc_tickets", "svc_customers"}):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert _scalar(
                    connection, "SELECT has_table_privilege('hours_app', :t, :p)", t=table, p=privilege
                ), f"{privilege} on {table}"
        for sequence in sorted(SVC_SEQUENCES_0013):
            for privilege in ("USAGE", "SELECT"):
                assert _scalar(
                    connection, "SELECT has_sequence_privilege('hours_app', :s, :p)", s=sequence, p=privilege
                ), f"{privilege} on {sequence}"
