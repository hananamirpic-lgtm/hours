"""Auto-generated employee number that doubles as the employee's login username.

This migration reshapes how an employee is brought into the system. Three previously-mandatory
fields become optional, a new `employee_number` column is added, and every existing non-terminated
employee is backfilled with a number and a matching login so the accounts that predate this feature
behave exactly like the ones created afterwards.

What upgrade() does, in order:

1. Nullability. `country`, `emergency_contact_name`, and `emergency_contact_phone_encrypted` (the
   *_encrypted form is the real DB column name for the emergency phone) are altered to `nullable=True`
   so an employee can be created without them (Requirement 1.5). The application-layer schema is what
   now decides which fields are mandatory; the database no longer forces these three.

2. New column. `employee_number` is added as plain `sa.Text()`, `nullable=True`. It is not encrypted
   (unlike passport/phone), so it is indexable and can serve directly as a login username. It is
   nullable at the column level so this migration can add it before the backfill assigns values, and
   because a terminated employee legitimately holds no active number.

3. Partial unique index. `uq_employees_employee_number_not_terminated` is created mirroring the
   passport index in migration 0001 verbatim in style: uniqueness applies only to rows whose status
   is not `terminated`, so a terminated employee's number is released for reuse. This is a PostgreSQL
   partial index; the SQLite unit-test engine cannot express it and is backstopped in the service.

4. Backfill. Every existing NON-terminated employee that has `employee_number IS NULL` is assigned a
   distinct free 4-digit number in 2000-2999, deterministically ordered by `created_at, id`, using a
   window function: `2000 + (row_number() - 1)`. The `employee_number` column is brand new, so no
   existing row holds a number yet; assigning `2000 + (rn - 1)` to the non-terminated rows is
   therefore distinct and collision-free by construction. Terminated employees are left NULL — they
   hold no active number.

   For each backfilled employee that does not already have a linked login, an employee-role login is
   inserted: `username = employee_number`, `role = 'employee'`, `must_change_password = true`,
   `is_active = true`, `employee_id` set. The login is created only for a non-terminated employee that
   received a number and that has no existing `users` row for that `employee_id` at all — this
   respects both `uq_users_username` and the `uq_users_employee_id` constraint from migration 0001,
   leaving any pre-existing login (active or not) untouched. An `ON CONFLICT (username) DO NOTHING`
   guard is the final backstop against a username collision.

   Password hashing. bcrypt cannot be computed in raw SQL, so the initial password `"1234"` is hashed
   once at module import via `app.core.security.hash_password` into `_INITIAL_HASH`, and that single
   hash string is passed as a bound literal for every backfilled login. A bcrypt hash carries its own
   salt, so one hash of `"1234"` verifies `"1234"` for every row — sharing it across rows is correct.

No grant is added. `employees` and `users` were both covered by migration 0001's
`GRANT ... ON ALL TABLES` to the application role, and this migration only adds a column and rows to
tables the application role can already write; it creates no new table.

downgrade() drops the partial unique index, drops the `employee_number` column, and restores NOT NULL
on `country`, `emergency_contact_name`, and `emergency_contact_phone_encrypted`. Restoring NOT NULL
succeeds only on a database whose rows all carry non-null values in those three columns; a row left
null by a create made after this migration would make the downgrade fail, which is intended — the
prior non-null schema cannot be restored while a violating row exists. The employee logins created by
the backfill are intentionally NOT deleted on downgrade: they are valid logins a person may already be
using, and removing them would lock those people out.

Revision ID: 0006_employee_number
Revises: 0005_must_change_password
Create Date: 2025-01-06 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.core.security import hash_password

revision = "0006_employee_number"
down_revision = "0005_must_change_password"
branch_labels = None
depends_on = None

# One bcrypt hash of the initial password, computed once at import. bcrypt embeds its own salt, so a
# single hash of "1234" verifies "1234" for every backfilled login; sharing it across rows is correct
# and avoids hashing per row (which raw SQL cannot do anyway).
_INITIAL_HASH = hash_password("1234")

# The employee-role value of the user_role enum, and the number range. Repeated as literals rather
# than imported, like migrations 0001/0004/0005: a migration is a frozen record and must not change
# meaning when a constant is renamed elsewhere.
EMPLOYEE_ROLE = "employee"
EMPLOYEE_NUMBER_MIN = 2000


def upgrade() -> None:
    # 1. Relax the three columns the application layer now treats as optional (Requirement 1.5).
    op.alter_column("employees", "country", nullable=True)
    op.alter_column("employees", "emergency_contact_name", nullable=True)
    op.alter_column("employees", "emergency_contact_phone_encrypted", nullable=True)

    # 2. Add the new number column. Plain Text, not encrypted, indexable, usable as a username.
    op.add_column("employees", sa.Column("employee_number", sa.Text(), nullable=True))

    # 3. Partial unique index, mirroring the passport index in 0001 verbatim in style: uniqueness is
    #    over non-terminated employees only, so a terminated employee's number is freed for reuse.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_employees_employee_number_not_terminated
            ON employees (employee_number)
            WHERE employee_number IS NOT NULL AND status <> 'terminated'::employee_status
        """
    )

    # 4a. Backfill numbers. Every existing non-terminated employee lacking a number gets a distinct
    #     free 4-digit value, ordered deterministically by created_at, id. The column is brand new so
    #     no row holds a number yet: 2000 + (row_number() - 1) is distinct by construction. Terminated
    #     employees are left NULL — they hold no active number.
    op.execute(
        sa.text(
            """
            WITH numbered AS (
                SELECT
                    id,
                    :base + (
                        ROW_NUMBER() OVER (ORDER BY created_at, id) - 1
                    ) AS assigned_number
                FROM employees
                WHERE employee_number IS NULL
                  AND status <> 'terminated'::employee_status
            )
            UPDATE employees AS e
            SET employee_number = numbered.assigned_number::text
            FROM numbered
            WHERE e.id = numbered.id
            """
        ).bindparams(base=EMPLOYEE_NUMBER_MIN)
    )

    # 4b. Backfill logins. For each non-terminated employee that just received a number and has no
    #     existing users row at all (respecting both uq_users_username and uq_users_employee_id), add
    #     an employee-role login. The single precomputed hash of "1234" is passed as a bound literal.
    #     The ON CONFLICT (username) DO NOTHING guard is the final backstop; a pre-existing login is
    #     left untouched.
    op.execute(
        sa.text(
            """
            INSERT INTO users (
                id, username, password_hash, role, employee_id,
                is_active, must_change_password, is_2fa_enabled, failed_login_count,
                language, token_version, created_at, updated_at
            )
            SELECT
                gen_random_uuid(),
                e.employee_number,
                :password_hash,
                CAST(:role AS user_role),
                e.id,
                true,
                true,
                false,
                0,
                'he'::app_language,
                1,
                now(),
                now()
            FROM employees AS e
            WHERE e.employee_number IS NOT NULL
              AND e.status <> 'terminated'::employee_status
              AND NOT EXISTS (
                  SELECT 1 FROM users AS u WHERE u.employee_id = e.id
              )
            ON CONFLICT (username) DO NOTHING
            """
        ).bindparams(password_hash=_INITIAL_HASH, role=EMPLOYEE_ROLE)
    )


def downgrade() -> None:
    op.drop_index(
        "uq_employees_employee_number_not_terminated", table_name="employees"
    )
    op.drop_column("employees", "employee_number")
    # Restore NOT NULL. Succeeds only on a database whose rows all carry non-null values in these
    # three columns; a row left null after this migration makes the downgrade fail, as intended. The
    # backfilled employee logins are intentionally NOT deleted here — they are valid, in-use logins.
    op.alter_column("employees", "country", nullable=False)
    op.alter_column("employees", "emergency_contact_name", nullable=False)
    op.alter_column("employees", "emergency_contact_phone_encrypted", nullable=False)
