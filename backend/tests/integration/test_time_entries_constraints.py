"""The attendance invariant, tested where it is actually enforced.

> An employee has at most one open time entry at any moment, and completed entries for one employee
> never overlap.

Requirement 11.3 asks for this at the database level, not only in application code, because the
failure mode it guards against is a race between two concurrent scans — and application code cannot
win that race by checking first. These tests therefore write SQL directly, bypassing any service
layer, which is the only way to prove the database itself refuses.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

WORK_DATE = date(2026, 8, 30)


def _at(hour: int, minute: int = 0) -> datetime:
    """A UTC instant on the work date. Storage is UTC; local classification happens elsewhere."""
    return datetime(WORK_DATE.year, WORK_DATE.month, WORK_DATE.day, hour, minute, tzinfo=UTC)


def _insert_entry(
    db: sa.Connection,
    employee_id: uuid.UUID,
    site_id: uuid.UUID,
    check_in: datetime,
    check_out: datetime | None = None,
) -> uuid.UUID:
    return db.execute(
        sa.text(
            """
            INSERT INTO time_entries (
                employee_id, site_id, work_date, check_in_at, check_out_at, total_minutes, source
            ) VALUES (
                :employee_id, :site_id, :work_date, :check_in, :check_out, :total_minutes, 'qr_scan'
            ) RETURNING id
            """
        ),
        {
            "employee_id": employee_id,
            "site_id": site_id,
            "work_date": WORK_DATE,
            "check_in": check_in,
            "check_out": check_out,
            "total_minutes": (
                None if check_out is None else int((check_out - check_in).total_seconds() // 60)
            ),
        },
    ).scalar_one()


def test_a_second_open_entry_for_the_same_employee_is_rejected(
    db: sa.Connection, employee_id: uuid.UUID, site_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Requirement 11.3. This is the write that a concurrent second scan attempts."""
    site_a, site_b = site_ids
    _insert_entry(db, employee_id, site_a, _at(7))

    with pytest.raises(IntegrityError) as error, db.begin_nested():
        _insert_entry(db, employee_id, site_b, _at(9))

    assert "one_open_entry_per_employee" in str(error.value.orig)


def test_two_employees_may_each_have_an_open_entry(
    db: sa.Connection, employee_id: uuid.UUID, site_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The index is per employee. A shared limit would stop the business working at all."""
    site_a, _ = site_ids
    colleague_id = db.execute(
        sa.text(
            """
            INSERT INTO employees (
                full_name, full_name_en, passport_number_encrypted, passport_number_hash,
                phone_encrypted, country, emergency_contact_name,
                emergency_contact_phone_encrypted, start_date
            ) VALUES (
                'שרה לוי', 'Sarah Levi', 'gcm:passport-2', 'hmac:passport-2',
                'gcm:phone-2', 'IL', 'Dan Levi', 'gcm:emergency-2', DATE '2025-01-01'
            ) RETURNING id
            """
        )
    ).scalar_one()

    _insert_entry(db, employee_id, site_a, _at(7))
    _insert_entry(db, colleague_id, site_a, _at(7))

    open_entries = db.execute(
        sa.text("SELECT count(*) FROM time_entries WHERE check_out_at IS NULL")
    ).scalar_one()
    assert open_entries == 2


def test_overlapping_completed_entries_are_rejected(
    db: sa.Connection, employee_id: uuid.UUID, site_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Requirement 11.7. Being paid at two sites for the same minute is not possible."""
    site_a, site_b = site_ids
    _insert_entry(db, employee_id, site_a, _at(7), _at(11, 30))

    with pytest.raises(IntegrityError) as error, db.begin_nested():
        _insert_entry(db, employee_id, site_b, _at(10), _at(15))

    assert "no_overlapping_entries" in str(error.value.orig)


def test_a_multi_site_day_of_adjacent_entries_is_accepted(
    db: sa.Connection, employee_id: uuid.UUID, site_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The brief's own day: 07:00–11:30 at one site, 12:00–17:00 at another, 570 minutes total.

    The exclusion constraint has to reject overlaps without rejecting this, or the feature the
    system exists for would be impossible.
    """
    site_a, site_b = site_ids
    _insert_entry(db, employee_id, site_a, _at(7), _at(11, 30))
    _insert_entry(db, employee_id, site_b, _at(12), _at(17))

    total = db.execute(
        sa.text("SELECT sum(total_minutes) FROM time_entries WHERE employee_id = :id"),
        {"id": employee_id},
    ).scalar_one()
    assert total == 570


def test_entries_touching_at_a_single_instant_are_accepted(
    db: sa.Connection, employee_id: uuid.UUID, site_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A transition closes one entry and opens the next at the same server time.

    `tstzrange` is half-open by default, so [07:00, 11:30) and [11:30, 15:00) do not overlap. If the
    constraint used an inclusive range, every transition would be rejected — which is why this is
    worth asserting rather than assuming.
    """
    site_a, site_b = site_ids
    _insert_entry(db, employee_id, site_a, _at(7), _at(11, 30))
    _insert_entry(db, employee_id, site_b, _at(11, 30), _at(15))

    entries = db.execute(
        sa.text("SELECT count(*) FROM time_entries WHERE employee_id = :id"), {"id": employee_id}
    ).scalar_one()
    assert entries == 2


def test_check_out_before_check_in_is_rejected(
    db: sa.Connection, employee_id: uuid.UUID, site_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    site_a, _ = site_ids

    with pytest.raises(IntegrityError) as error, db.begin_nested():
        _insert_entry(db, employee_id, site_a, _at(17), _at(9))

    assert "ck_time_entries_check_out_after_check_in" in str(error.value.orig)


def test_check_out_equal_to_check_in_is_rejected(
    db: sa.Connection, employee_id: uuid.UUID, site_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A zero-length shift is a double scan, not work. The constraint is strictly greater than."""
    site_a, _ = site_ids

    with pytest.raises(IntegrityError) as error, db.begin_nested():
        _insert_entry(db, employee_id, site_a, _at(9), _at(9))

    assert "ck_time_entries_check_out_after_check_in" in str(error.value.orig)


def test_a_soft_deleted_open_entry_does_not_block_a_new_one(
    db: sa.Connection, employee_id: uuid.UUID, site_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Soft delete has to release the invariant, or a mistaken entry would lock the employee out."""
    site_a, site_b = site_ids
    stale = _insert_entry(db, employee_id, site_a, _at(7))
    db.execute(
        sa.text(
            "UPDATE time_entries SET deleted_at = now(), delete_reason = 'opened in error' " "WHERE id = :id"
        ),
        {"id": stale},
    )

    _insert_entry(db, employee_id, site_b, _at(9))

    live_open = db.execute(
        sa.text(
            "SELECT count(*) FROM time_entries WHERE employee_id = :id "
            "AND check_out_at IS NULL AND deleted_at IS NULL"
        ),
        {"id": employee_id},
    ).scalar_one()
    assert live_open == 1


def test_a_manual_entry_without_a_reason_is_rejected(
    db: sa.Connection, employee_id: uuid.UUID, site_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Requirement 12.3. A blank string is not a reason, so the check trims before testing."""
    site_a, _ = site_ids

    with pytest.raises(IntegrityError) as error, db.begin_nested():
        db.execute(
            sa.text(
                """
                INSERT INTO time_entries (
                    employee_id, site_id, work_date, check_in_at, check_out_at,
                    total_minutes, source, is_manual, manual_reason
                ) VALUES (
                    :employee_id, :site_id, :work_date, :check_in, :check_out,
                    480, 'manual', true, '   '
                )
                """
            ),
            {
                "employee_id": employee_id,
                "site_id": site_a,
                "work_date": WORK_DATE,
                "check_in": _at(7),
                "check_out": _at(15),
            },
        )

    assert "ck_time_entries_manual_requires_reason" in str(error.value.orig)
