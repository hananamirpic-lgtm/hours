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
    # Added by migration 0013.
    "svc_localities",
    "svc_parts",
    "svc_ticket_parts",
    "svc_branding",
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


def test_exactly_the_eleven_svc_tables_are_registered():
    assert {table.name for table in _svc_tables()} == SVC_TABLES
    assert len(SVC_TABLES) == 11


def test_every_table_added_by_the_feature_starts_with_svc():
    """The only tables beyond the Hours set are the eleven `svc_` ones."""
    extra = set(Base.metadata.tables) - HOURS_TABLES
    assert extra == SVC_TABLES


def test_every_foreign_key_on_a_svc_table_points_at_a_svc_table():
    for table in _svc_tables():
        for constraint in table.constraints:
            if isinstance(constraint, ForeignKeyConstraint):
                assert constraint.referred_table.name in SVC_TABLES, (table.name, constraint.name)


def test_every_svc_foreign_key_restricts_deletion_except_a_ticket_part_link_to_its_ticket():
    """Seven foreign keys: the three of 0011, the two `locality_id` columns and the two links of 0013."""
    foreign_keys = {
        (fk.parent.table.name, fk.parent.name): fk.ondelete
        for table in _svc_tables()
        for fk in table.foreign_keys
    }
    assert len(foreign_keys) == 7
    assert {key for key, rule in foreign_keys.items() if rule == "CASCADE"} == {
        ("svc_ticket_parts", "ticket_id")
    }
    assert set(foreign_keys.values()) == {"RESTRICT", "CASCADE"}
    assert {key for key, rule in foreign_keys.items() if rule == "RESTRICT"} == {
        ("svc_tickets", "customer_id"),
        ("svc_tickets", "category_id"),
        ("svc_history_entries", "ticket_id"),
        ("svc_tickets", "locality_id"),
        ("svc_customers", "locality_id"),
        ("svc_ticket_parts", "part_id"),
    }


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
        "ix_svc_tickets_locality_id",
    }
    assert "entry_text" in metadata.tables["svc_history_entries"].columns
    assert "created_date" in tickets.columns and "city_key" in tickets.columns
    assert categories.columns["seq"].identity is not None
    assert metadata.tables["svc_signin_failures"].columns["id"].identity is not None
    assert {
        c.name for c in metadata.tables["svc_settings"].constraints if isinstance(c, CheckConstraint)
    } == {"ck_svc_settings_id_is_one"}


def test_the_0013_tables_carry_the_names_and_bounds_the_design_fixes():
    metadata = Base.metadata

    def check_texts(table: str) -> dict[str, str]:
        return {
            str(c.name): str(c.sqltext)
            for c in metadata.tables[table].constraints
            if isinstance(c, CheckConstraint)
        }

    for table, longest in (("svc_localities", 100), ("svc_parts", 60)):
        categories = metadata.tables["svc_categories"]
        assert [c.name for c in metadata.tables[table].columns] == [c.name for c in categories.columns]
        assert metadata.tables[table].columns["seq"].identity is not None
        assert {
            c.name for c in metadata.tables[table].constraints if c.__class__.__name__ == "UniqueConstraint"
        } == {f"uq_{table}_seq", f"uq_{table}_name_he_key", f"uq_{table}_name_en_key"}
        assert check_texts(table) == {
            f"ck_{table}_name_he_length": f"length(name_he) BETWEEN 1 AND {longest}",
            f"ck_{table}_name_en_length": f"length(name_en) BETWEEN 1 AND {longest}",
        }

    parts = metadata.tables["svc_ticket_parts"]
    assert [c.name for c in parts.primary_key.columns] == ["ticket_id", "part_id"]
    assert {i.name for i in parts.indexes} == {"ix_svc_ticket_parts_part_id"}

    branding = metadata.tables["svc_branding"]
    assert [c.name for c in branding.columns] == ["id", "company_name", "logo_key", "updated_at"]
    assert set(check_texts("svc_branding")) == {
        "ck_svc_branding_id_is_one",
        "ck_svc_branding_company_name_length",
    }
    assert {c.name for c in branding.constraints if c.__class__.__name__ == "UniqueConstraint"} == {
        "uq_svc_branding_logo_key"
    }

    for table in ("svc_tickets", "svc_customers"):
        column = metadata.tables[table].columns["locality_id"]
        assert column.nullable is True and column.server_default is None
        assert {i.name for i in metadata.tables[table].indexes} >= {f"ix_{table}_locality_id"}


def test_create_all_on_sqlite_creates_every_svc_table():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    assert set(inspect(engine).get_table_names()) >= SVC_TABLES
    engine.dispose()
