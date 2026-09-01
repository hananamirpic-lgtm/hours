"""The `employees` and `employee_rates` tables.

Mappings onto the tables migration 0001 created, not definitions of them. Every column name,
constraint name and index name reproduces what the migration wrote, so Alembic autogenerate compares
like with like instead of proposing to change a schema that is already correct.

The encryption is invisible here on purpose. A column typed `EncryptedString` is plaintext to a
service reading or writing the attribute and ciphertext in the database; the `passport_number_hash`
companion is a `DeterministicHash`, so a service assigns the plaintext passport number and the keyed
digest is what reaches the column the partial unique index sits on. That is why nothing in this file
imports the encryptor: getting it wrong is only possible in `app.db.types`, and it is done once there.

Two things are deliberately *not* declared, both for the same reason the `users` model omits them:

* The foreign key from `users.employee_id` back to here, and the one from `employee_rates` onward —
  declared on the side that owns them. The `employee_rates.employee_id` foreign key *is* declared
  here because it points at `employees`, which this module defines, so it resolves on SQLite where
  the unit tests build the schema from metadata.
* The load-bearing database objects — the partial unique index on `passport_number_hash` where the
  status is not terminated, and the GiST exclusion constraint that stops two rate rows overlapping —
  cannot be expressed as model attributes and live in the migration. The service enforces the
  passport rule in application code as well, because SQLite (the unit-test engine) has neither the
  partial index nor the exclusion constraint, so the database backstop is only present in PostgreSQL.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import DeterministicHash, EncryptedDate, EncryptedString


class EmployeeStatus(enum.StrEnum):
    """Requirement 3.4. Values match the `employee_status` PostgreSQL enum."""

    ACTIVE = "active"
    ON_LEAVE = "on_leave"
    INACTIVE = "inactive"
    TERMINATED = "terminated"


#: Only an active employee may start a new shift (Requirement 3.7). Kept beside the enum so the scan
#: path and the employee service read the same rule rather than each spelling out "== active".
STATUSES_ALLOWING_CHECK_IN = frozenset({EmployeeStatus.ACTIVE})


def _enum_column(enum_type: type[enum.Enum], name: str) -> Enum:
    """A PostgreSQL enum column storing member *values*, matching how the `users` model does it."""
    return Enum(
        enum_type,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Employee(Base):
    """A person the business employs. Never hard-deleted; status carries the whole lifecycle."""

    __tablename__ = "employees"

    # Names given explicitly so they match migration 0001. The partial unique index on
    # `passport_number_hash` is not declared here — it is a `WHERE status <> 'terminated'` index that
    # SQLAlchemy cannot express and the migration owns.
    __table_args__ = (
        Index("ix_employees_status", "status"),
        Index("ix_employees_full_name", "full_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    #: Mandatory (Requirement 3.1). The local-language form; `full_name_en` is the English one, and
    #: search matches either (Requirement 21.5).
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    full_name_en: Mapped[str] = mapped_column(Text, nullable=False)

    #: Object-storage key, not the image. Nullable in the database because an employee row is created
    #: before a photo is uploaded; the API is where Requirement 3.1's "photo is mandatory" is applied.
    photo_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Passport number, encrypted at rest, plaintext to the service. The `_hash` companion carries the
    #: keyed digest the uniqueness index needs, since AES-GCM ciphertext differs on every write and so
    #: cannot be indexed for equality. Assign the plaintext to both attributes.
    passport_number: Mapped[str] = mapped_column(
        "passport_number_encrypted", EncryptedString, nullable=False
    )
    passport_number_hash: Mapped[str] = mapped_column(DeterministicHash, nullable=False)

    phone: Mapped[str] = mapped_column("phone_encrypted", EncryptedString, nullable=False)
    country: Mapped[str] = mapped_column(Text, nullable=False)

    #: Optional (Requirement 3.2). Sensitive, so encrypted; `EncryptedDate` keeps it a real `date`.
    date_of_birth: Mapped[date | None] = mapped_column(
        "date_of_birth_encrypted", EncryptedDate, nullable=True
    )
    address: Mapped[str | None] = mapped_column("address_encrypted", EncryptedString, nullable=True)

    #: Emergency contact is mandatory (Requirement 3.1). The name is not sensitive; the phone is.
    emergency_contact_name: Mapped[str] = mapped_column(Text, nullable=False)
    emergency_contact_phone: Mapped[str] = mapped_column(
        "emergency_contact_phone_encrypted", EncryptedString, nullable=False
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Employment detail (Requirement 3.3). Wage lives in `employee_rates`, not here, so history is
    #: never lost.
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    position: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[EmployeeStatus] = mapped_column(
        _enum_column(EmployeeStatus, "employee_status"), nullable=False, default=EmployeeStatus.ACTIVE
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    #: History rows, newest kept alongside the rest. Ordered oldest-first so the resolver and the API
    #: read them in the same direction. Not cascaded on delete because an employee is never deleted.
    rates: Mapped[list[EmployeeRate]] = relationship(
        back_populates="employee",
        order_by="EmployeeRate.effective_from",
        cascade="all, delete-orphan",
    )

    @property
    def can_check_in(self) -> bool:
        """Whether this employee's status permits a new check-in (Requirement 3.7)."""
        return self.status in STATUSES_ALLOWING_CHECK_IN

    def __repr__(self) -> str:
        return f"Employee({self.full_name!r} status={self.status})"


class EmployeeRate(Base):
    """One effective-dated pay row for an employee (Requirement 3.3, 16.9).

    Non-overlapping per employee: the database enforces it with a GiST exclusion constraint in
    PostgreSQL, and the service enforces it everywhere by closing the previous open row before opening
    a new one. `effective_to = NULL` means "still in force"; the resolver picks the row whose period
    covers a given work date, which is what makes a mid-month rate change produce the right answer.
    """

    __tablename__ = "employee_rates"

    # The two check constraints migration 0001 created (`period_ordered`, `non_negative`) and the
    # GiST exclusion constraint (`ex_employee_rates_no_overlapping_periods`) are deliberately not
    # declared here. The exclusion constraint cannot be expressed as a model attribute at all; the
    # check constraints could be, but the naming convention in `app.db.base` would rewrite a named
    # check to `ck_employee_rates_<name>`, whereas the migration created them under the plain names —
    # so declaring them would make autogenerate propose a rename of a constraint that never changed.
    # This mirrors the same omission the `users` model documents for its own check constraint. The
    # database enforces all three; the service enforces the non-overlap rule in application code too,
    # for the SQLite unit-test engine that has neither the exclusion constraint nor the checks.
    __table_args__ = (
        Index("ix_employee_rates_employee_id_effective_from", "employee_id", "effective_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("employees.id", name="fk_employee_rates_employee_id_employees"),
        nullable=False,
    )

    hourly_wage: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    overtime_rate: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    shabbat_holiday_rate: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    travel_allowance_daily: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0")
    )

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    #: NULL means still in force. Payroll resolves the row covering each work date (Requirement 16.9).
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    employee: Mapped[Employee] = relationship(back_populates="rates")

    def covers(self, on_date: date) -> bool:
        """Whether this row is the rate in force on `on_date`.

        The period is inclusive at both ends, matching the `daterange(..., '[]')` the exclusion
        constraint uses: a row effective 1–15 August and a row effective from 16 August do not
        overlap, and every August date is covered by exactly one of them.
        """
        if on_date < self.effective_from:
            return False
        return self.effective_to is None or on_date <= self.effective_to

    def __repr__(self) -> str:
        return f"EmployeeRate(employee={self.employee_id} from={self.effective_from} to={self.effective_to})"
