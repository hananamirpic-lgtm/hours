"""The migration applies to an empty database, and reverses without residue.

Requirement 24.6: schema changes are applied by versioned migrations. A migration that only goes
forwards is half a migration — a failed release has to be able to go back.
"""

from __future__ import annotations

from collections.abc import Callable

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from alembic import command
from alembic.config import Config

# The design's table list, written out rather than derived, so a table added by accident or dropped
# by accident both show up as a failure.
EXPECTED_TABLES = {
    "users",
    "user_sites",
    "employees",
    "employee_rates",
    "clients",
    "sites",
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
    "exports",
}


def _table_names(connection: sa.Connection) -> set[str]:
    return set(
        connection.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
        .scalars()
        .all()
    )


def test_migration_creates_exactly_the_designed_tables(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        tables = _table_names(connection)

    # alembic_version is bookkeeping, not part of the data model.
    assert tables - {"alembic_version"} == EXPECTED_TABLES


def test_required_extensions_are_enabled(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        extensions = set(connection.execute(sa.text("SELECT extname FROM pg_extension")).scalars().all())

    # pgcrypto supplies gen_random_uuid(); btree_gist is what lets the exclusion constraint mix an
    # equality test on a uuid with an overlap test on a range.
    assert {"pgcrypto", "btree_gist"} <= extensions


def test_time_entries_invariants_exist_in_the_database(migrated_engine: Engine) -> None:
    """The three constraints the whole design rests on, present under the names the design gives."""
    with migrated_engine.connect() as connection:
        indexes = set(
            connection.execute(sa.text("SELECT indexname FROM pg_indexes WHERE tablename = 'time_entries'"))
            .scalars()
            .all()
        )
        constraints = dict(
            connection.execute(
                sa.text(
                    "SELECT conname, contype FROM pg_constraint " "WHERE conrelid = 'time_entries'::regclass"
                )
            ).all()
        )

    assert "one_open_entry_per_employee" in indexes
    # 'x' is an exclusion constraint; a check constraint under the same name would not guarantee
    # anything about overlaps.
    assert constraints.get("no_overlapping_entries") == "x"
    assert constraints.get("ck_time_entries_check_out_after_check_in") == "c"


def test_designed_indexes_exist(migrated_engine: Engine) -> None:
    expected = {
        "time_entries": {
            "ix_time_entries_employee_id_work_date",
            "ix_time_entries_site_id_work_date",
            "ix_time_entries_status_work_date",
            "ix_time_entries_work_date",
        },
        "employees": {"uq_employees_passport_number_hash_not_terminated"},
        "change_logs": {
            "ix_change_logs_entity_type_entity_id_changed_at",
            "ix_change_logs_changed_by_user_id_changed_at",
        },
        "documents": {"ix_documents_employee_id", "ix_documents_expiry_date"},
    }

    with migrated_engine.connect() as connection:
        for table, names in expected.items():
            present = set(
                connection.execute(
                    sa.text("SELECT indexname FROM pg_indexes WHERE tablename = :table"),
                    {"table": table},
                )
                .scalars()
                .all()
            )
            assert names <= present, f"{table} is missing {sorted(names - present)}"


def test_rate_history_cannot_overlap(migrated_engine: Engine) -> None:
    """Two rates in force on one date would make pay and billing ambiguous."""
    with migrated_engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT conrelid::regclass::text AS table_name, conname FROM pg_constraint "
                "WHERE contype = 'x' AND conrelid::regclass::text IN ('employee_rates', 'site_rates')"
            )
        ).all()

    assert {row.table_name for row in rows} == {"employee_rates", "site_rates"}


def test_every_timestamp_column_carries_a_time_zone(migrated_engine: Engine) -> None:
    """A naive timestamp column would silently lose the offset the whole design depends on."""
    with migrated_engine.connect() as connection:
        naive = connection.execute(
            sa.text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type = 'timestamp without time zone'"
            )
        ).all()

    assert naive == []


def test_money_columns_are_fixed_point(migrated_engine: Engine) -> None:
    """No float or double anywhere: pay and invoices are not allowed to be approximate."""
    with migrated_engine.connect() as connection:
        approximate = connection.execute(
            sa.text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type IN ('real', 'double precision')"
            )
        ).all()
        numerics = connection.execute(
            sa.text(
                "SELECT table_name, column_name, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type = 'numeric'"
            )
        ).all()

    assert approximate == []
    assert numerics, "the schema should hold monetary columns"
    for row in numerics:
        assert (row.numeric_precision, row.numeric_scale) == (12, 2), (
            f"{row.table_name}.{row.column_name} is NUMERIC"
            f"({row.numeric_precision},{row.numeric_scale}), not NUMERIC(12,2)"
        )


def test_migration_applies_and_reverses_on_an_empty_database(
    empty_database: str, make_alembic_config: Callable[[str], Config]
) -> None:
    """Upgrade then downgrade, leaving the database as empty as it was found.

    Run on its own scratch database rather than the session-scoped one, because a downgrade would
    otherwise destroy the schema every other test in the session depends on.
    """
    config = make_alembic_config(empty_database)
    engine = sa.create_engine(empty_database, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as connection:
            assert _table_names(connection) == set(), "the scratch database should start empty"

        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert _table_names(connection) - {"alembic_version"} == EXPECTED_TABLES

        command.downgrade(config, "base")
        with engine.connect() as connection:
            remaining = _table_names(connection) - {"alembic_version"}
            enum_types = (
                connection.execute(
                    sa.text(
                        "SELECT typname FROM pg_type t "
                        "JOIN pg_namespace n ON n.oid = t.typnamespace "
                        "WHERE t.typtype = 'e' AND n.nspname = 'public'"
                    )
                )
                .scalars()
                .all()
            )

        assert remaining == set(), f"downgrade left tables behind: {sorted(remaining)}"
        # Enum types are easy to forget in a downgrade, and a leftover type makes a re-upgrade fail.
        assert enum_types == [], f"downgrade left enum types behind: {sorted(enum_types)}"
    finally:
        engine.dispose()
