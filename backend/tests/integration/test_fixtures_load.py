"""Developer fixtures against a real PostgreSQL.

The fixture writes two closed time entries for one employee on one day at two sites. That is exactly
the shape the overlap exclusion constraint polices, so it can only be proven correct against a real
PostgreSQL — a fake would accept overlapping entries and prove nothing. The multi-site day is also
the brief's worked example, so its minutes are asserted directly: 07:00–11:30 plus 12:00–17:00 is
9h30, and the split into 8h00 regular and 1h30 overtime is what the calculation engine will later be
measured against.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from app.core.config import Settings
from app.core.crypto import Encryptor
from app.fixtures import load_fixtures

# The fixture's own key facts, restated so the test fails loudly if the fixture is quietly changed.
_MULTI_SITE_DATE = date(2025, 3, 4)
_EMPLOYEE_PASSPORT = "X1234567"
_ENCRYPTION_SECRET = "fixtures-integration-encryption-secret-000"


def _settings() -> Settings:
    return Settings(  # type: ignore[arg-type]
        database_url="postgresql+psycopg://x:x@127.0.0.1:1/x",
        redis_url="redis://127.0.0.1:1/0",
        jwt_secret_key="fixtures-integration-jwt-secret-not-real-0",
        encryption_key=_ENCRYPTION_SECRET,
        s3_endpoint_url="http://127.0.0.1:1",
        s3_access_key_id="k",
        s3_secret_access_key="s",
        s3_bucket_documents="b",
        app_timezone="Asia/Jerusalem",
    )


def _load(db: sa.Connection) -> tuple[dict[str, int], Encryptor]:
    encryptor = Encryptor(_ENCRYPTION_SECRET)
    counts = load_fixtures(db, settings=_settings(), encryptor=encryptor)
    return counts, encryptor


def test_fixtures_create_the_sample_world(db: sa.Connection) -> None:
    counts, _ = _load(db)
    assert counts == {"clients": 2, "sites": 3, "employees": 3, "time_entries": 2}

    assert db.execute(sa.text("SELECT count(*) FROM clients")).scalar_one() >= 2
    assert db.execute(sa.text("SELECT count(*) FROM sites")).scalar_one() >= 3
    assert db.execute(sa.text("SELECT count(*) FROM employees")).scalar_one() >= 3
    # Every employee has a rate and is assigned to sites, so the console has something to show.
    assert db.execute(sa.text("SELECT count(*) FROM employee_rates")).scalar_one() >= 3
    assert db.execute(sa.text("SELECT count(*) FROM site_rates")).scalar_one() >= 3
    assert db.execute(sa.text("SELECT count(*) FROM employee_sites")).scalar_one() >= 3


def test_multi_site_day_totals_the_brief_example(db: sa.Connection) -> None:
    _, encryptor = _load(db)
    employee_id = db.execute(
        sa.text("SELECT id FROM employees WHERE passport_number_hash = :h"),
        {"h": encryptor.deterministic_hash(_EMPLOYEE_PASSPORT)},
    ).scalar_one()

    rows = db.execute(
        sa.text(
            """
            SELECT s.site_number, t.total_minutes, t.work_date
            FROM time_entries t JOIN sites s ON s.id = t.site_id
            WHERE t.employee_id = :id AND t.work_date = :d
            ORDER BY t.check_in_at
            """
        ),
        {"id": employee_id, "d": _MULTI_SITE_DATE},
    ).all()

    assert [(r.site_number, r.total_minutes) for r in rows] == [("S-001", 270), ("S-002", 300)]
    # 4h30 + 5h00 = 9h30, the brief's day. The 8h/1h30 split is the engine's job (task 15); here we
    # pin the raw minutes it will be given.
    assert sum(r.total_minutes for r in rows) == 570
    # Both entries are attributed to the local check-in date, even though check-in was 07:00 local.
    assert all(r.work_date == _MULTI_SITE_DATE for r in rows)


def test_multi_site_day_is_stored_in_utc(db: sa.Connection) -> None:
    _, encryptor = _load(db)
    employee_id = db.execute(
        sa.text("SELECT id FROM employees WHERE passport_number_hash = :h"),
        {"h": encryptor.deterministic_hash(_EMPLOYEE_PASSPORT)},
    ).scalar_one()

    first_check_in = db.execute(
        sa.text(
            "SELECT check_in_at FROM time_entries WHERE employee_id = :id ORDER BY check_in_at LIMIT 1"
        ),
        {"id": employee_id},
    ).scalar_one()

    # 07:00 Asia/Jerusalem on 2025-03-04 is 05:00 UTC (IST is UTC+2 in winter). Stored as the right
    # instant, which is the contract the scan service will follow.
    expected_utc = datetime(2025, 3, 4, 7, 0, tzinfo=ZoneInfo("Asia/Jerusalem")).astimezone(
        ZoneInfo("UTC")
    )
    assert first_check_in.astimezone(ZoneInfo("UTC")) == expected_utc


def test_fixtures_are_idempotent(db: sa.Connection) -> None:
    _load(db)
    counts_second, _ = _load(db)
    # A second run adds nothing: same clients, sites and employees, and no second copy of the day.
    assert counts_second["time_entries"] == 0

    employee_count = db.execute(sa.text("SELECT count(*) FROM employees")).scalar_one()
    entry_count = db.execute(
        sa.text("SELECT count(*) FROM time_entries WHERE work_date = :d"), {"d": _MULTI_SITE_DATE}
    ).scalar_one()
    assert employee_count == 3
    assert entry_count == 2


def test_multi_site_entries_do_not_overlap(db: sa.Connection) -> None:
    # The fixture's two entries are consecutive, not overlapping; loading them proves the exclusion
    # constraint accepted them, and this asserts the gap explicitly.
    _, encryptor = _load(db)
    employee_id = db.execute(
        sa.text("SELECT id FROM employees WHERE passport_number_hash = :h"),
        {"h": encryptor.deterministic_hash(_EMPLOYEE_PASSPORT)},
    ).scalar_one()
    overlap = db.execute(
        sa.text(
            """
            SELECT count(*) FROM time_entries a JOIN time_entries b
              ON a.employee_id = b.employee_id AND a.id <> b.id
             AND tstzrange(a.check_in_at, a.check_out_at) && tstzrange(b.check_in_at, b.check_out_at)
            WHERE a.employee_id = :id
            """
        ),
        {"id": employee_id},
    ).scalar_one()
    assert overlap == 0
