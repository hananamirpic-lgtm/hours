"""Developer fixtures — sample data for a local database.

This produces a small, coherent world to develop and demonstrate against: a couple of clients, the
sites they own, a handful of employees with rates, and — the point of the exercise — a realistic
multi-site day, the one the whole business case rests on. It is a *development* tool, never run in a
real deployment: it invents people and invents attendance, and both would be corrupt data anywhere
that holds the genuine article. The CLI refuses to run when the environment is `production`.

The multi-site day is the brief's worked example, reproduced exactly so a developer can see the
numbers the calculation engine is meant to produce before that engine exists: an employee works
07:00–11:30 at Site A and 12:00–17:00 at Site B on the same local day, which is 9h30 in total, and
once `classify_day` lands it should split into 8h00 regular and 1h30 overtime with the overtime
falling at Site B. The two entries are written closed and non-overlapping, so they satisfy the
database's exclusion constraint the same way real scanned entries will.

Times are the awkward part and are handled in one place. Every timestamp is stored in UTC, but the
business day is local (`Asia/Jerusalem`), so a fixture expressed in local wall-clock time is
converted to UTC before it is written, and each entry's `work_date` is the *local* date of check-in
— exactly the contract the service layer will honour for real entries. Getting this right in the
fixture matters because a fixture that stored naive local times would "work" until the first
developer in a different timezone ran it.

Everything is idempotent on a natural key (client name, site number, employee passport hash), so
re-running tops the world up rather than duplicating it. Sensitive employee columns are encrypted
through the same `Encryptor` the application uses, so the rows are indistinguishable from ones a
form created and the passport-uniqueness index sees the value it expects.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.core.config import Settings, get_settings
from app.core.crypto import Encryptor, get_encryptor

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _EmployeeFixture:
    full_name: str
    full_name_en: str
    passport_number: str
    phone: str
    country: str
    emergency_contact_name: str
    emergency_contact_phone: str
    position: str
    hourly_wage: Decimal
    overtime_rate: Decimal
    shabbat_holiday_rate: Decimal
    travel_allowance_daily: Decimal


@dataclass(frozen=True, slots=True)
class _SiteFixture:
    name: str
    site_number: str
    billing_rate: Decimal
    overtime_billing_rate: Decimal | None = None


@dataclass(frozen=True, slots=True)
class _ClientFixture:
    name: str
    company_number: str
    sites: tuple[_SiteFixture, ...]


@dataclass(frozen=True, slots=True)
class _ShiftFixture:
    """One entry in the multi-site day, in local wall-clock time."""

    site_number: str
    check_in: time
    check_out: time


@dataclass(frozen=True, slots=True)
class _MultiSiteDayFixture:
    employee_passport: str
    shifts: tuple[_ShiftFixture, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- the sample world

# Rates chosen so the worked examples in the brief come out to round, checkable figures: 35 ₪/h
# regular, and site billing rates of 60 ₪/h and 75 ₪/h.
_CLIENTS: tuple[_ClientFixture, ...] = (
    _ClientFixture(
        name="Kibbutz Rosh Tzurim",
        company_number="514000001",
        sites=(
            _SiteFixture(name="Rosh Tzurim — Orchard", site_number="S-001", billing_rate=Decimal("60.00")),
            _SiteFixture(
                name="Rosh Tzurim — Packing House",
                site_number="S-002",
                billing_rate=Decimal("75.00"),
                overtime_billing_rate=Decimal("90.00"),
            ),
        ),
    ),
    _ClientFixture(
        name="Negev Construction Ltd",
        company_number="514000002",
        sites=(
            _SiteFixture(name="Beersheba Tower", site_number="S-101", billing_rate=Decimal("80.00")),
        ),
    ),
)

_EMPLOYEES: tuple[_EmployeeFixture, ...] = (
    _EmployeeFixture(
        full_name="אברהם כהן",
        full_name_en="Avraham Cohen",
        passport_number="X1234567",
        phone="+972500000001",
        country="IL",
        emergency_contact_name="Sarah Cohen",
        emergency_contact_phone="+972500000010",
        position="Field worker",
        hourly_wage=Decimal("35.00"),
        overtime_rate=Decimal("43.75"),
        shabbat_holiday_rate=Decimal("52.50"),
        travel_allowance_daily=Decimal("20.00"),
    ),
    _EmployeeFixture(
        full_name="מרים לוי",
        full_name_en="Miriam Levi",
        passport_number="X2345678",
        phone="+972500000002",
        country="IL",
        emergency_contact_name="David Levi",
        emergency_contact_phone="+972500000020",
        position="Team lead",
        hourly_wage=Decimal("42.00"),
        overtime_rate=Decimal("52.50"),
        shabbat_holiday_rate=Decimal("63.00"),
        travel_allowance_daily=Decimal("20.00"),
    ),
    _EmployeeFixture(
        full_name="יוסף אברהם",
        full_name_en="Yosef Avraham",
        passport_number="X3456789",
        phone="+972500000003",
        country="IL",
        emergency_contact_name="Rachel Avraham",
        emergency_contact_phone="+972500000030",
        position="Field worker",
        hourly_wage=Decimal("38.00"),
        overtime_rate=Decimal("47.50"),
        shabbat_holiday_rate=Decimal("57.00"),
        travel_allowance_daily=Decimal("20.00"),
    ),
)

# The brief's worked example: 07:00–11:30 at S-001, 12:00–17:00 at S-002, same local day. 9h30
# total → 8h00 regular + 1h30 overtime, overtime at the later site.
_MULTI_SITE_DAY = _MultiSiteDayFixture(
    employee_passport="X1234567",
    shifts=(
        _ShiftFixture(site_number="S-001", check_in=time(7, 0), check_out=time(11, 30)),
        _ShiftFixture(site_number="S-002", check_in=time(12, 0), check_out=time(17, 0)),
    ),
)

# A fixed date so a re-run reproduces the same day rather than drifting; a plain weekday, so the day
# carries no Shabbat or holiday premium and the 8+1.5 split is the only thing under test.
_MULTI_SITE_DAY_DATE = date(2025, 3, 4)  # a Tuesday
# Rates and assignments start well before the sample day so a resolver finds them in force.
_RATE_EFFECTIVE_FROM = date(2025, 1, 1)


# --------------------------------------------------------------------------- loader


def load_fixtures(
    connection: Connection,
    *,
    settings: Settings | None = None,
    encryptor: Encryptor | None = None,
) -> dict[str, int]:
    """Load the sample world into `connection`'s transaction. Returns a count per entity kind.

    Idempotent: every insert is `ON CONFLICT DO NOTHING` on a natural key, and the time entries are
    skipped entirely when the sample day is already present, so a second run is a no-op rather than a
    duplicate day (which the overlap exclusion constraint would reject anyway). Never commits — the
    caller owns the transaction.
    """
    settings = settings or get_settings()
    encryptor = encryptor or get_encryptor()
    timezone = ZoneInfo(settings.app_timezone)

    client_ids = _load_clients(connection)
    site_ids = _load_sites(connection, client_ids)
    _load_site_rates(connection, site_ids)
    employee_ids = _load_employees(connection, encryptor)
    _load_employee_rates(connection, employee_ids)
    _load_employee_sites(connection, employee_ids, site_ids)
    entries = _load_multi_site_day(connection, employee_ids, site_ids, timezone)

    counts = {
        "clients": len(client_ids),
        "sites": len(site_ids),
        "employees": len(employee_ids),
        "time_entries": entries,
    }
    logger.info("fixtures loaded: %s", counts)
    return counts


def _load_clients(connection: Connection) -> dict[str, uuid.UUID]:
    ids: dict[str, uuid.UUID] = {}
    for client in _CLIENTS:
        connection.execute(
            sa.text(
                """
                INSERT INTO clients (name, company_number)
                VALUES (:name, :company_number)
                ON CONFLICT (company_number) DO NOTHING
                """
            ),
            {"name": client.name, "company_number": client.company_number},
        )
        ids[client.name] = connection.execute(
            sa.text("SELECT id FROM clients WHERE company_number = :company_number"),
            {"company_number": client.company_number},
        ).scalar_one()
    return ids


def _load_sites(connection: Connection, client_ids: dict[str, uuid.UUID]) -> dict[str, uuid.UUID]:
    ids: dict[str, uuid.UUID] = {}
    for client in _CLIENTS:
        for site in client.sites:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO sites (name, site_number, client_id, qr_token)
                    VALUES (:name, :site_number, :client_id, :qr_token)
                    ON CONFLICT (site_number) DO NOTHING
                    """
                ),
                {
                    "name": site.name,
                    "site_number": site.site_number,
                    "client_id": client_ids[client.name],
                    # A stable, unique placeholder token. Task 14 replaces this with a signed token;
                    # a fixture only needs a value the unique constraint will accept.
                    "qr_token": f"fixture-qr-{site.site_number}",
                },
            )
            ids[site.site_number] = connection.execute(
                sa.text("SELECT id FROM sites WHERE site_number = :site_number"),
                {"site_number": site.site_number},
            ).scalar_one()
    return ids


def _load_site_rates(connection: Connection, site_ids: dict[str, uuid.UUID]) -> None:
    for client in _CLIENTS:
        for site in client.sites:
            site_id = site_ids[site.site_number]
            # Idempotent without a natural unique key on the rate row: skip if this site already has
            # a rate open at the sample effective date. Inserting a second would trip the
            # no-overlapping-periods exclusion constraint.
            exists = connection.execute(
                sa.text("SELECT 1 FROM site_rates WHERE site_id = :site_id AND effective_from = :from"),
                {"site_id": site_id, "from": _RATE_EFFECTIVE_FROM},
            ).first()
            if exists is not None:
                continue
            connection.execute(
                sa.text(
                    """
                    INSERT INTO site_rates
                        (site_id, billing_rate, overtime_billing_rate, effective_from)
                    VALUES (:site_id, :billing_rate, :overtime_billing_rate, :effective_from)
                    """
                ),
                {
                    "site_id": site_id,
                    "billing_rate": site.billing_rate,
                    "overtime_billing_rate": site.overtime_billing_rate,
                    "effective_from": _RATE_EFFECTIVE_FROM,
                },
            )


def _load_employees(connection: Connection, encryptor: Encryptor) -> dict[str, uuid.UUID]:
    """Insert employees keyed for idempotency on the deterministic passport hash.

    The hash is exactly what the partial unique index uses, so "already loaded" here means the same
    thing the database means by "duplicate passport", and the two can never disagree.
    """
    ids: dict[str, uuid.UUID] = {}
    for employee in _EMPLOYEES:
        passport_hash = encryptor.deterministic_hash(employee.passport_number)
        existing = connection.execute(
            sa.text("SELECT id FROM employees WHERE passport_number_hash = :hash"),
            {"hash": passport_hash},
        ).scalar_one_or_none()
        if existing is not None:
            ids[employee.passport_number] = existing
            continue
        new_id = connection.execute(
            sa.text(
                """
                INSERT INTO employees (
                    full_name, full_name_en, passport_number_encrypted, passport_number_hash,
                    phone_encrypted, country, emergency_contact_name,
                    emergency_contact_phone_encrypted, position, start_date, status
                ) VALUES (
                    :full_name, :full_name_en, :passport_encrypted, :passport_hash,
                    :phone_encrypted, :country, :emergency_name,
                    :emergency_phone_encrypted, :position, :start_date,
                    CAST('active' AS employee_status)
                ) RETURNING id
                """
            ),
            {
                "full_name": employee.full_name,
                "full_name_en": employee.full_name_en,
                "passport_encrypted": encryptor.encrypt(employee.passport_number),
                "passport_hash": passport_hash,
                "phone_encrypted": encryptor.encrypt(employee.phone),
                "country": employee.country,
                "emergency_name": employee.emergency_contact_name,
                "emergency_phone_encrypted": encryptor.encrypt(employee.emergency_contact_phone),
                "position": employee.position,
                "start_date": _RATE_EFFECTIVE_FROM,
            },
        ).scalar_one()
        ids[employee.passport_number] = new_id
    return ids


def _load_employee_rates(connection: Connection, employee_ids: dict[str, uuid.UUID]) -> None:
    for employee in _EMPLOYEES:
        employee_id = employee_ids[employee.passport_number]
        exists = connection.execute(
            sa.text(
                "SELECT 1 FROM employee_rates WHERE employee_id = :id AND effective_from = :from"
            ),
            {"id": employee_id, "from": _RATE_EFFECTIVE_FROM},
        ).first()
        if exists is not None:
            continue
        connection.execute(
            sa.text(
                """
                INSERT INTO employee_rates (
                    employee_id, hourly_wage, overtime_rate, shabbat_holiday_rate,
                    travel_allowance_daily, effective_from
                ) VALUES (
                    :employee_id, :hourly_wage, :overtime_rate, :shabbat_holiday_rate,
                    :travel_allowance_daily, :effective_from
                )
                """
            ),
            {
                "employee_id": employee_id,
                "hourly_wage": employee.hourly_wage,
                "overtime_rate": employee.overtime_rate,
                "shabbat_holiday_rate": employee.shabbat_holiday_rate,
                "travel_allowance_daily": employee.travel_allowance_daily,
                "effective_from": _RATE_EFFECTIVE_FROM,
            },
        )


def _load_employee_sites(
    connection: Connection,
    employee_ids: dict[str, uuid.UUID],
    site_ids: dict[str, uuid.UUID],
) -> None:
    """Assign every employee to every site, so the console has assignments to show.

    Assignment is expected placement only; it never restricts where time can be recorded, so
    assigning broadly here does not constrain the multi-site day.
    """
    for employee_id in employee_ids.values():
        for site_id in site_ids.values():
            connection.execute(
                sa.text(
                    """
                    INSERT INTO employee_sites (employee_id, site_id, assigned_from)
                    VALUES (:employee_id, :site_id, :assigned_from)
                    ON CONFLICT (employee_id, site_id) DO NOTHING
                    """
                ),
                {
                    "employee_id": employee_id,
                    "site_id": site_id,
                    "assigned_from": _RATE_EFFECTIVE_FROM,
                },
            )


def _load_multi_site_day(
    connection: Connection,
    employee_ids: dict[str, uuid.UUID],
    site_ids: dict[str, uuid.UUID],
    timezone: ZoneInfo,
) -> int:
    """Write the brief's multi-site day as two closed, non-overlapping entries. Returns rows written.

    Skipped whole if the employee already has an entry on the sample date, so a re-run neither
    duplicates the day nor trips the overlap constraint. Local wall-clock times are converted to UTC
    here — the one place the fixture touches timezones — and `work_date` is the local check-in date,
    which is the same contract the scan service will follow.
    """
    employee_id = employee_ids[_MULTI_SITE_DAY.employee_passport]
    already = connection.execute(
        sa.text(
            "SELECT 1 FROM time_entries WHERE employee_id = :id AND work_date = :work_date LIMIT 1"
        ),
        {"id": employee_id, "work_date": _MULTI_SITE_DAY_DATE},
    ).first()
    if already is not None:
        return 0

    written = 0
    for shift in _MULTI_SITE_DAY.shifts:
        check_in_local = datetime.combine(_MULTI_SITE_DAY_DATE, shift.check_in, tzinfo=timezone)
        check_out_local = datetime.combine(_MULTI_SITE_DAY_DATE, shift.check_out, tzinfo=timezone)
        total_minutes = int((check_out_local - check_in_local) / timedelta(minutes=1))
        connection.execute(
            sa.text(
                """
                INSERT INTO time_entries (
                    employee_id, site_id, work_date, check_in_at, check_out_at,
                    total_minutes, source, status
                ) VALUES (
                    :employee_id, :site_id, :work_date, :check_in_at, :check_out_at,
                    :total_minutes, CAST('qr_scan' AS time_entry_source),
                    CAST('draft' AS time_entry_status)
                )
                """
            ),
            {
                "employee_id": employee_id,
                "site_id": site_ids[shift.site_number],
                "work_date": _MULTI_SITE_DAY_DATE,
                # Stored in UTC; the driver takes the aware datetime and writes the right instant.
                "check_in_at": check_in_local.astimezone(ZoneInfo("UTC")),
                "check_out_at": check_out_local.astimezone(ZoneInfo("UTC")),
                "total_minutes": total_minutes,
            },
        )
        written += 1
    return written


# --------------------------------------------------------------------------- command-line entry


def run_fixtures() -> None:
    """Load the fixtures against the configured database in one transaction.

    Refuses to run in production: the whole module invents data, and inventing employees or
    attendance in a real deployment is a data-integrity incident, not a convenience.
    """
    settings = get_settings()
    if settings.is_production:
        raise SystemExit("refusing to load developer fixtures in a production environment")

    from app.db.session import get_engine

    with get_engine().begin() as connection:
        load_fixtures(connection, settings=settings)


def _main() -> None:
    logging.basicConfig(level=get_settings().log_level, format="%(levelname)s %(name)s: %(message)s")
    run_fixtures()


if __name__ == "__main__":
    _main()
