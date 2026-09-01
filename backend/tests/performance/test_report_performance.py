"""Report latency and the scale ceiling, against real PostgreSQL (Requirement 24.2, 24.3).

Two non-functional guarantees, both of which only the real database can vouch for:

* **24.2 — reports within 5 seconds.** One month of data for 100 employees over 20 sites, then the
  four reports (by-employee, by-site, by-client, profitability) read within 5 seconds *in total*. The
  reports are `GROUP BY`/join aggregations over `payroll_records`, `billing_records` and
  `time_entries`; their timing depends on the PostgreSQL planner and the design's indexes, so timing
  them on SQLite would measure a different engine. The unit-level report timing on SQLite lives in
  `tests/test_reports_api.py::test_reports_over_100_employees_and_20_sites_are_under_5_seconds`; this
  is the same shape against the database the requirement is about.

* **24.3 — 500 employees and 100 sites without schema change.** The claim is not about speed, it is
  that the *schema already holds* this population: no new table, no new column, no new migration is
  needed to reach 5× the 24.2 dataset. So the test captures the migration head and the table set,
  inserts 500 employees and 100 sites (with their rates and assignments), and asserts the migration
  head and the table set are byte-for-byte unchanged and every row landed. If holding this population
  had required a schema change, the head would have moved or a table would have appeared.

Both build their data with plain bulk inserts through the ORM rather than a hundred HTTP round-trips —
the setup is not what either requirement bounds, and 100 re-authenticating requests would time the
harness, not the reports. The report reads (24.2) go through the report *service*, which is the code
the HTTP layer calls; the money figures themselves are pinned by the reconciliation tests in
`tests/test_reports_api.py`, so here the subject is latency and scale, not the arithmetic.

Gated on `TEST_DATABASE_URL` via `conftest.py`: no live PostgreSQL, and the whole module skips.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.employee import Employee, EmployeeRate
from app.models.site import EmployeeSite, Site, SiteRate
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.services import billing as billing_service
from app.services import payroll as payroll_service
from app.services.reports import (
    ReportFilters,
    report_by_client,
    report_by_employee,
    report_by_site,
    report_profitability,
)

#: Requirement 24.2's population and its ceiling.
REPORT_EMPLOYEES = 100
REPORT_SITES = 20
REPORT_TARGET_SECONDS = 5.0

#: Requirement 24.3's population: the schema must hold this without a migration.
SCALE_EMPLOYEES = 500
SCALE_SITES = 100

_JERUSALEM = ZoneInfo("Asia/Jerusalem")
_TZ = "IL"

# August 2025 Saturdays fall on 2, 9, 16, 23 and 30. A shift on any other day is plain regular hours,
# so every entry is billable and no day picks up a Shabbat premium — which keeps the timed reports
# reading a clean, all-regular month whose figures are easy to reason about.
_SATURDAYS = {2, 9, 16, 23, 30}
_WEEKDAYS = [d for d in range(1, 32) if d not in _SATURDAYS]


def _cleanup(engine: Engine, *, marker: str) -> None:
    """Delete everything a run inserted, keyed on the run's unique marker.

    The performance database is session-scoped and committed to, not rolled back, so each test tidies
    after itself: children first (time entries, allocations, rates, assignments), then the sites,
    employees and client, so no foreign key blocks the delete. Keying on the client name, the site
    number and the employee's `full_name` keeps one test's rows from touching another's.

    Employees are matched on `full_name`, not `passport_number_hash`: the hash column is a
    `DeterministicHash`, so the seeded plaintext marker is HMAC'd on write and the stored value is a
    digest that no `LIKE :marker%` can match. `full_name` (`"{marker} worker {n}"`) is plain text and
    carries the marker verbatim, so it is the column the cleanup can actually filter on.
    """
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                DELETE FROM time_entries WHERE employee_id IN
                    (SELECT id FROM employees WHERE full_name LIKE :like)
                """
            ),
            {"like": f"{marker}%"},
        )
        connection.execute(
            sa.text(
                """
                DELETE FROM payroll_records WHERE employee_id IN
                    (SELECT id FROM employees WHERE full_name LIKE :like)
                """
            ),
            {"like": f"{marker}%"},
        )
        connection.execute(
            sa.text(
                "DELETE FROM billing_records WHERE site_id IN "
                "(SELECT id FROM sites WHERE site_number LIKE :like)"
            ),
            {"like": f"{marker}%"},
        )
        connection.execute(
            sa.text(
                "DELETE FROM employee_rates WHERE employee_id IN "
                "(SELECT id FROM employees WHERE full_name LIKE :like)"
            ),
            {"like": f"{marker}%"},
        )
        connection.execute(
            sa.text(
                "DELETE FROM employee_sites WHERE site_id IN "
                "(SELECT id FROM sites WHERE site_number LIKE :like)"
            ),
            {"like": f"{marker}%"},
        )
        connection.execute(
            sa.text(
                "DELETE FROM site_rates WHERE site_id IN "
                "(SELECT id FROM sites WHERE site_number LIKE :like)"
            ),
            {"like": f"{marker}%"},
        )
        connection.execute(sa.text("DELETE FROM sites WHERE site_number LIKE :like"), {"like": f"{marker}%"})
        connection.execute(
            sa.text("DELETE FROM employees WHERE full_name LIKE :like"), {"like": f"{marker}%"}
        )
        connection.execute(sa.text("DELETE FROM clients WHERE name LIKE :like"), {"like": f"{marker}%"})


def _seed_month(
    session: Session,
    *,
    marker: str,
    employees: int,
    sites: int,
    with_entries: bool,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Seed a client, `sites` sites and `employees` employees; optionally one entry per pair.

    Returns the client id and the employee ids. When `with_entries` is set, each employee works one
    8-hour approved shift at each site, each site on its own weekday, so no employee crosses the daily
    overtime threshold and every site produces a billing record — the dataset the 24.2 reports read.
    The scale test (24.3) sets it False: it only needs the *rows to exist* to prove the schema holds
    them, not a month of attendance on top.
    """
    client = Client(name=f"{marker} client")
    session.add(client)
    session.flush()

    site_rows: list[Site] = []
    for index in range(sites):
        site = Site(
            name=f"{marker} site {index}",
            site_number=f"{marker}-S{index:03d}",
            client_id=client.id,
            qr_token=f"qr-{marker}-{index}",
            qr_token_version=1,
        )
        session.add(site)
        site_rows.append(site)
    session.flush()
    session.add_all(
        SiteRate(
            site_id=site.id,
            billing_rate=Decimal("60.00"),
            overtime_billing_rate=None,
            effective_from=date(2025, 1, 1),
            effective_to=None,
        )
        for site in site_rows
    )

    employee_rows: list[Employee] = []
    for index in range(employees):
        passport = f"{marker}{index:06d}"
        employee = Employee(
            full_name=f"{marker} worker {index}",
            full_name_en=f"{marker} worker {index}",
            passport_number=passport,
            passport_number_hash=passport,
            phone="+972500000000",
            country="IL",
            emergency_contact_name="Contact",
            emergency_contact_phone="+972500000001",
            start_date=date(2025, 1, 1),
        )
        session.add(employee)
        employee_rows.append(employee)
    session.flush()
    session.add_all(
        EmployeeRate(
            employee_id=employee.id,
            hourly_wage=Decimal("35.00"),
            overtime_rate=Decimal("35.00"),
            shabbat_holiday_rate=Decimal("35.00"),
            travel_allowance_daily=Decimal("0"),
            effective_from=date(2025, 1, 1),
            effective_to=None,
        )
        for employee in employee_rows
    )
    session.add_all(
        EmployeeSite(employee_id=employee.id, site_id=site.id, assigned_from=date(2025, 1, 1))
        for employee in employee_rows
        for site in site_rows
    )

    if with_entries:
        entries: list[TimeEntry] = []
        for employee in employee_rows:
            for site_index, site in enumerate(site_rows):
                day = _WEEKDAYS[site_index % len(_WEEKDAYS)]
                check_in = datetime(2025, 8, day, 6, 0, tzinfo=_JERUSALEM).astimezone(UTC)
                entries.append(
                    TimeEntry(
                        employee_id=employee.id,
                        site_id=site.id,
                        work_date=date(2025, 8, day),
                        check_in_at=check_in,
                        check_out_at=check_in + timedelta(minutes=480),
                        total_minutes=480,
                        source=TimeEntrySource.QR_SCAN,
                        is_manual=False,
                        status=TimeEntryStatus.APPROVED,
                        flags=[],
                    )
                )
        session.add_all(entries)

    session.commit()
    return client.id, [employee.id for employee in employee_rows]


def test_reports_over_100_employees_and_20_sites_are_under_5_seconds(perf_engine: Engine) -> None:
    """Requirement 24.2: the four reports over a 100-employee, 20-site month read within 5 seconds.

    Build the month, compute payroll for every employee-month and billing for the month (the reports
    read those records, they do not recompute), then time only the four report reads. The assertion is
    on the read time, which is what 24.2 bounds; setup and calculation are excluded from the timed
    section, matching the SQLite-level performance test's contract.
    """
    marker = f"R2P{uuid.uuid4().hex[:6]}"
    try:
        with Session(perf_engine) as session:
            client_id, employee_ids = _seed_month(
                session, marker=marker, employees=REPORT_EMPLOYEES, sites=REPORT_SITES, with_entries=True
            )

            for employee_id in employee_ids:
                payroll_service.calculate_payroll(session, employee_id=employee_id, year=2025, month=8)
                session.commit()  # one unit of work per employee-month, as the payroll endpoint does
            billing_service.calculate_billing(session, year=2025, month=8)
            session.commit()

            filters = ReportFilters(year=2025, month=8)
            started = time.perf_counter()
            by_employee = report_by_employee(session, filters=filters)
            by_site = report_by_site(session, filters=filters)
            report_by_client(session, filters=filters)
            report_profitability(session, filters=filters)
            elapsed = time.perf_counter() - started

        assert elapsed < REPORT_TARGET_SECONDS, (
            f"the four reports took {elapsed:.2f}s over {REPORT_EMPLOYEES} employees and "
            f"{REPORT_SITES} sites, target is under {REPORT_TARGET_SECONDS}s"
        )
        # Sanity: the reports actually returned the full population, so the timing is of real work.
        assert len(by_employee.rows) == REPORT_EMPLOYEES
        assert len(by_site.rows) == REPORT_SITES
    finally:
        _cleanup(perf_engine, marker=marker)


def _schema_fingerprint(connection: sa.Connection) -> tuple[str, frozenset[str]]:
    """The migration head and the set of base tables — what a schema change would move.

    A new migration bumps `alembic_version.version_num`; a new table appears in the table set. Holding
    a larger population must change neither, which is exactly what 24.3 asserts.
    """
    version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    tables = frozenset(
        connection.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
        .scalars()
        .all()
    )
    return version, tables


def test_500_employees_and_100_sites_need_no_schema_change(perf_engine: Engine) -> None:
    """Requirement 24.3: the schema holds 500 employees and 100 sites without a migration.

    Capture the migration head and table set, insert the 500-employee, 100-site population (with rates
    and assignments — the full shape, not bare rows), and assert the head and the table set are
    unchanged and every row landed. The claim is structural: reaching 5× the 24.2 dataset required no
    new table, no new column, no new migration.
    """
    marker = f"S5C{uuid.uuid4().hex[:6]}"
    try:
        with perf_engine.connect() as connection:
            before = _schema_fingerprint(connection)

        with Session(perf_engine) as session:
            _, employee_ids = _seed_month(
                session, marker=marker, employees=SCALE_EMPLOYEES, sites=SCALE_SITES, with_entries=False
            )

        with perf_engine.connect() as connection:
            after = _schema_fingerprint(connection)
            employee_count = connection.execute(
                # `full_name`, not `passport_number_hash`: the hash column stores an HMAC digest of
                # the seeded marker, not the marker itself, so only the plaintext name carries it.
                sa.text("SELECT count(*) FROM employees WHERE full_name LIKE :like"),
                {"like": f"{marker}%"},
            ).scalar_one()
            site_count = connection.execute(
                sa.text("SELECT count(*) FROM sites WHERE site_number LIKE :like"),
                {"like": f"{marker}%"},
            ).scalar_one()

        assert employee_count == SCALE_EMPLOYEES
        assert site_count == SCALE_SITES
        # The whole point: the population landed and the schema did not move to accommodate it.
        assert after == before, "holding 500 employees / 100 sites should need no schema change"
    finally:
        _cleanup(perf_engine, marker=marker)
