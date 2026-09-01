"""`change_logs` is append-only, enforced by PostgreSQL privileges.

Requirement 13.3 says the system exposes no way to update or delete an audit record. Reviewing the
API for the absence of such an endpoint proves it today; revoking the privilege proves it for every
future code path, including one written in a hurry to fix something else.

The tests `SET ROLE` to the application role, so they exercise the privilege the running service
will actually have, not the migration runner's.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import ProgrammingError

from app.db.roles import APPLICATION_ROLE


def _insert_audit_row(db: sa.Connection) -> uuid.UUID:
    return db.execute(
        sa.text(
            """
            INSERT INTO change_logs (entity_type, entity_id, field, old_value, new_value, reason)
            VALUES ('time_entry', gen_random_uuid(), 'check_out_at', '15:30', '16:00', 'forgot to scan')
            RETURNING id
            """
        )
    ).scalar_one()


def test_the_application_role_exists(db: sa.Connection) -> None:
    """The migration creates it, so a fresh environment is correct without an operator step."""
    exists = db.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
        {"role": APPLICATION_ROLE},
    ).scalar_one()
    assert exists, f"{APPLICATION_ROLE} is missing; app.db.roles and the migration have diverged"


def test_the_application_role_may_insert_audit_rows(db: sa.Connection) -> None:
    """Append-only means append is allowed — the audit writer runs as this role."""
    db.execute(sa.text(f"SET LOCAL ROLE {APPLICATION_ROLE}"))
    row_id = _insert_audit_row(db)
    assert row_id is not None


@pytest.mark.parametrize(
    ("operation", "statement"),
    [
        ("update", "UPDATE change_logs SET new_value = 'tampered' WHERE id = :id"),
        ("delete", "DELETE FROM change_logs WHERE id = :id"),
        ("truncate", "TRUNCATE change_logs"),
    ],
)
def test_the_application_role_cannot_rewrite_audit_rows(
    db: sa.Connection, operation: str, statement: str
) -> None:
    row_id = _insert_audit_row(db)
    db.execute(sa.text(f"SET LOCAL ROLE {APPLICATION_ROLE}"))

    with pytest.raises(ProgrammingError) as error, db.begin_nested():
        db.execute(sa.text(statement), {"id": row_id} if ":id" in statement else {})

    # PostgreSQL reports a missing table privilege as insufficient_privilege (42501).
    assert (
        getattr(error.value.orig, "sqlstate", None) == "42501"
    ), f"{operation} was not denied for {APPLICATION_ROLE}: {error.value.orig}"


def test_privileges_granted_on_change_logs_are_insert_and_select_only(db: sa.Connection) -> None:
    """States the whole grant, so a later blanket GRANT ALL shows up here rather than in an audit."""
    granted = set(
        db.execute(
            sa.text(
                "SELECT privilege_type FROM information_schema.table_privileges "
                "WHERE table_schema = 'public' AND table_name = 'change_logs' AND grantee = :role"
            ),
            {"role": APPLICATION_ROLE},
        )
        .scalars()
        .all()
    )
    assert granted == {"SELECT", "INSERT"}


def test_the_application_role_has_no_say_over_the_migration_version(db: sa.Connection) -> None:
    """The application must not be able to rewrite which migration the database believes it is on."""
    granted = db.execute(
        sa.text(
            "SELECT count(*) FROM information_schema.table_privileges "
            "WHERE table_schema = 'public' AND table_name = 'alembic_version' AND grantee = :role"
        ),
        {"role": APPLICATION_ROLE},
    ).scalar_one()
    assert granted == 0


def test_other_tables_remain_writable_by_the_application_role(db: sa.Connection) -> None:
    """The revocation is targeted. Revoking too widely would break every ordinary update."""
    granted = set(
        db.execute(
            sa.text(
                "SELECT privilege_type FROM information_schema.table_privileges "
                "WHERE table_schema = 'public' AND table_name = 'time_entries' AND grantee = :role"
            ),
            {"role": APPLICATION_ROLE},
        )
        .scalars()
        .all()
    )
    assert {"SELECT", "INSERT", "UPDATE", "DELETE"} <= granted
