"""The `payroll_records` and `payroll_site_allocations` tables.

Mappings onto the tables migration 0001 created, not definitions of them. Every column name,
constraint name and index name reproduces what the migration wrote, so Alembic autogenerate compares
like with like instead of proposing to change a schema that is already correct — the same discipline
`app.models.time_entry` and `app.models.employee` follow.

A `payroll_records` row is one employee's pay for one calendar month: the minutes in each of the four
buckets, the pay for each, the travel allowance, the bonuses, the deductions and the total, plus a
`status` (`draft` while the month is open, `final` once it is locked) and when it was calculated.
`(employee_id, year, month)` is unique, which is what makes a recalculation an upsert rather than a
duplicate (Requirement 16.10): the second calculation finds the existing row and replaces its figures.

A `payroll_site_allocations` row is one site's share of that record's cost — the minutes worked there
per bucket and the cost of them. The costs across a record's allocations sum exactly to the record's
worked pay (its four bucket pays, excluding the allowances a site does not bear), which is the
property the largest-remainder correction in `app.calculations.payroll` exists to hold (Requirement
16.8). The allocations are cascaded on delete so replacing a draft's allocations is a clean delete and
re-insert; the parent record is never deleted in normal operation, only its allocations are rebuilt.

The money columns are `Numeric(12, 2)` — fixed-point, never binary float (Requirement 16.7). The
minute columns are integers, because a bucket is whole minutes and hours are only derived at pricing
time; storing hours would reintroduce the rounding drift the design took pains to avoid.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Imported for the foreign keys below, so the referenced tables are on the shared metadata.
from app.models.employee import Employee  # noqa: F401
from app.models.site import Site  # noqa: F401


class CalculationStatus(enum.StrEnum):
    """Whether a payroll (or billing) record is a working draft or final. Matches `calculation_status`.

    A record for an unlocked month is `draft` and may be recalculated; the payroll calculation replaces
    a draft's figures on each run (Requirement 16.10). `final` marks a record whose month is locked, so
    the figures are frozen with the entries they were computed from.
    """

    DRAFT = "draft"
    FINAL = "final"


def _enum_column(enum_type: type[enum.Enum], name: str) -> Enum:
    """A PostgreSQL enum column storing member *values*, matching how the other models do it."""
    return Enum(
        enum_type,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: Two-place money, as every monetary column in the schema (design, "money is NUMERIC(12,2)").
_MONEY = Numeric(12, 2)


class PayrollRecord(Base):
    """One employee's computed pay for one calendar month (Requirement 16.6)."""

    __tablename__ = "payroll_records"

    # Names given explicitly so they match migration 0001. The unique constraint on
    # (employee_id, year, month) is what makes recalculation an upsert (Requirement 16.10); the
    # portable check constraints are declared here, matching the plain names the migration used so
    # autogenerate does not propose a rename.
    __table_args__ = (
        UniqueConstraint(
            "employee_id", "year", "month", name="uq_payroll_records_employee_id_year_month"
        ),
        CheckConstraint("month BETWEEN 1 AND 12", name="month_range"),
        CheckConstraint("year BETWEEN 2000 AND 2200", name="year_range"),
        CheckConstraint(
            "regular_minutes >= 0 AND overtime_minutes >= 0 AND shabbat_minutes >= 0 "
            "AND holiday_minutes >= 0",
            name="minutes_non_negative",
        ),
        Index("ix_payroll_records_year_month", "year", "month"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("employees.id", name="fk_payroll_records_employee_id_employees"),
        nullable=False,
    )

    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Minutes per bucket, not hours: 4 h 29 min is 4.4833… hours and rounding that repeatedly drifts.
    regular_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shabbat_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    holiday_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    regular_pay: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=Decimal("0"))
    overtime_pay: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=Decimal("0"))
    shabbat_pay: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=Decimal("0"))
    holiday_pay: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=Decimal("0"))

    travel: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=Decimal("0"))
    bonuses: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=Decimal("0"))
    deductions: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=Decimal("0"))
    total_pay: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=Decimal("0"))

    status: Mapped[CalculationStatus] = mapped_column(
        _enum_column(CalculationStatus, "calculation_status"),
        nullable=False,
        default=CalculationStatus.DRAFT,
    )
    calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    #: The per-site cost breakdown. Cascaded on delete so replacing a draft's allocations is a delete
    #: and re-insert; ordered by site so a re-read is stable.
    allocations: Mapped[list[PayrollSiteAllocation]] = relationship(
        back_populates="record",
        cascade="all, delete-orphan",
        order_by="PayrollSiteAllocation.site_id",
    )

    def __repr__(self) -> str:
        return f"PayrollRecord(employee={self.employee_id} {self.year}-{self.month:02d} status={self.status})"


class PayrollSiteAllocation(Base):
    """One site's share of an employee's monthly cost (Requirement 16.8).

    The minutes are the month's minutes worked at this site per bucket; `cost` is the
    largest-remainder-corrected figure, so the sum of `cost` across a record's allocations equals the
    record's worked pay to the agora. A stray agora here would undermine every profit figure
    downstream, which is why the allocation is corrected rather than left as independently rounded sums.
    """

    __tablename__ = "payroll_site_allocations"

    __table_args__ = (
        UniqueConstraint(
            "payroll_record_id",
            "site_id",
            name="uq_payroll_site_allocations_payroll_record_id_site_id",
        ),
        CheckConstraint(
            "regular_minutes >= 0 AND overtime_minutes >= 0 AND shabbat_minutes >= 0 "
            "AND holiday_minutes >= 0",
            name="minutes_non_negative",
        ),
        Index("ix_payroll_site_allocations_site_id", "site_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    payroll_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "payroll_records.id",
            name="fk_payroll_site_allocations_payroll_record_id_payroll_records",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    site_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sites.id", name="fk_payroll_site_allocations_site_id_sites"),
        nullable=False,
    )

    regular_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shabbat_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    holiday_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    cost: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=Decimal("0"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    record: Mapped[PayrollRecord] = relationship(back_populates="allocations")

    def __repr__(self) -> str:
        return f"PayrollSiteAllocation(record={self.payroll_record_id} site={self.site_id} cost={self.cost})"
