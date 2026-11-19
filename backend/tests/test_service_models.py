"""The `svc_` models stay inside their own table set (Requirements 20.1, 20.2, 3.11, 10.1, 14.9, 19.7).

Registering the models changes `Base.metadata`, which `tests/conftest.py` feeds to `create_all`, so these
tests also pin the property that matters most to the Hours tables: nothing outside `svc_` points at, or
gained anything from, the new tables.
"""

from __future__ import annotations

import re

from sqlalchemy import CheckConstraint, Table, create_engine, inspect
from sqlalchemy.schema import ForeignKeyConstraint

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base

SVC_TABLES = {
    "svc_customers",
    "svc_categories",
    "svc_tickets",
    "svc_history_entries",
    "svc_settings",
    "svc_field_labels",
    "svc_signin_failures",
}

#: The tables of the Hours application before this feature; the set `main` registers.
HOURS_TABLES = {
    "billing_records",
    "change_logs",
    "clients",
    "documents",
    "employee_rates",
    "employee_sites",
    "employees",
    "exports",
    "notifications",
    "payroll_records",
    "payroll_site_allocations",
    "period_locks",
    "settings",
    "site_rates",
    "sites",
    "staffing_companies",
    "time_entries",
    "user_sites",
    "users",
}


def _svc_tables() -> list[Table]:
    return [table for name, table in Base.metadata.tables.items() if name.startswith("svc_")]


def _non_svc_tables() -> list[Table]:
    return [table for name, table in Base.metadata.tables.items() if not name.startswith("svc_")]


def test_exactly_the_seven_svc_tables_are_registered():
    assert {table.name for table in _svc_tables()} == SVC_TABLES


def test_every_table_added_by_the_feature_starts_with_svc():
    """The only tables beyond the Hours set are the seven `svc_` ones."""
    extra = set(Base.metadata.tables) - HOURS_TABLES
    assert extra == SVC_TABLES


def test_every_foreign_key_on_a_svc_table_points_at_a_svc_table():
    for table in _svc_tables():
        for constraint in table.constraints:
            if isinstance(constraint, ForeignKeyConstraint):
                assert constraint.referred_table.name in SVC_TABLES, (table.name, constraint.name)


def test_every_svc_foreign_key_restricts_deletion():
    foreign_keys = [fk for table in _svc_tables() for fk in table.foreign_keys]
    assert len(foreign_keys) == 3
    assert {fk.ondelete for fk in foreign_keys} == {"RESTRICT"}


def test_no_hours_table_references_a_svc_table():
    for table in _non_svc_tables():
        for foreign_key in table.foreign_keys:
            assert not foreign_key.column.table.name.startswith("svc_"), (table.name, foreign_key)


def test_no_hours_table_gained_a_column_or_constraint_named_for_the_feature():
    """The Hours tables are untouched: no `svc`-flavoured column, index or constraint on them."""
    for table in _non_svc_tables():
        names = [column.name for column in table.columns]
        names += [str(constraint.name) for constraint in table.constraints if constraint.name]
        names += [str(index.name) for index in table.indexes]
        assert not [name for name in names if "svc" in name or "ticket" in name], table.name


def test_the_ticket_number_check_is_between_10000_and_99999():
    ticket = Base.metadata.tables["svc_tickets"]
    checks = {c.name: str(c.sqltext) for c in ticket.constraints if isinstance(c, CheckConstraint)}
    bounds = re.search(r"BETWEEN\s+(\d+)\s+AND\s+(\d+)", checks["ck_svc_tickets_ticket_number_range"])
    assert bounds is not None
    assert (int(bounds.group(1)), int(bounds.group(2))) == (10000, 99999)


def test_the_ticket_status_check_lists_the_four_states():
    ticket = Base.metadata.tables["svc_tickets"]
    sqltext = next(
        str(c.sqltext)
        for c in ticket.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_svc_tickets_status_valid"
    )
    assert set(re.findall(r"'([a-z_]+)'", sqltext)) == {"received", "in_progress", "closed", "cancelled"}


def test_the_names_the_design_fixes_are_present():
    metadata = Base.metadata
    customers = metadata.tables["svc_customers"]
    categories = metadata.tables["svc_categories"]
    tickets = metadata.tables["svc_tickets"]
    unique = {
        t.name: {c.name for c in t.constraints if c.__class__.__name__ == "UniqueConstraint"}
        for t in (customers, categories, tickets)
    }
    assert "uq_svc_customers_phone" in unique["svc_customers"]
    assert {
        "uq_svc_categories_seq",
        "uq_svc_categories_name_he_key",
        "uq_svc_categories_name_en_key",
    } <= unique["svc_categories"]
    assert {"uq_svc_tickets_ticket_number", "uq_svc_tickets_photo_key"} <= unique["svc_tickets"]
    assert {i.name for i in tickets.indexes} == {
        "ix_svc_tickets_customer_id_created_at",
        "ix_svc_tickets_created_at_ticket_number",
        "ix_svc_tickets_status_urgent",
        "ix_svc_tickets_category_id",
        "ix_svc_tickets_city_key",
        "ix_svc_tickets_created_date",
    }
    assert "entry_text" in metadata.tables["svc_history_entries"].columns
    assert "created_date" in tickets.columns and "city_key" in tickets.columns
    assert categories.columns["seq"].identity is not None
    assert metadata.tables["svc_signin_failures"].columns["id"].identity is not None
    assert {
        c.name for c in metadata.tables["svc_settings"].constraints if isinstance(c, CheckConstraint)
    } == {"ck_svc_settings_id_is_one"}


def test_create_all_on_sqlite_creates_every_svc_table():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    assert set(inspect(engine).get_table_names()) >= SVC_TABLES
    engine.dispose()
