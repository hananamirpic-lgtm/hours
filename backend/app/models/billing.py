"""The `billing_records` table.

A mapping onto the table migration 0001 created, not a definition of it. Every column name,
constraint name and index name reproduces what the migration wrote, so Alembic autogenerate compares
like with like instead of proposing to change a schema that is already correct — the same discipline
`app.models.payroll` and `app.models.site` follow.

A `billing_records` row is one site's billing for one calendar month: the billable minutes in the
regular and overtime buckets, the billing rates applied, the total billed amount, a `status`
(`draft` while the month is open, `final` once it is locked) and when it was calculated.
`(site_id, year, month)` is unique, which is what makes a recalculation an upsert rather than a
duplicate: the second calculation finds the existing row and replaces its figures. `client_id` is
carried on the row so a client's billing aggregates without a join back through `sites` (Requirement
17.3), and it is indexed with `(year, month)` for the per-client period read.

`billing_rate_applied` and `overtime_rate_applied` are the rates the calculation used, *copied* onto
the row at calculation time (design, `billing_records`): reading them back from `site_rates` later
would re-derive history and could disagree with an invoice already sent. `billing_rate_applied` is
NOT NULL because every billed site has a standard rate; `overtime_rate_applied` is nullable because a
site may bill overtime at its standard rate, with no separate overtime rate configured (Requirement
17.2). The authoritative figure is `total_amount`, which the pure calculation computed per work date
so a mid-month rate change is honoured even though only one rate pair is stored for the invoice.

The money columns are `Numeric(12, 2)` — fixed-point, never binary float. The minute columns are
integers, because a bucket is whole minutes and hours are only derived at pricing time; storing hours
would reintroduce the rounding drift the design took pains to avoid.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Imported for the foreign keys below, so the referenced tables are on the shared metadata.
from app.models.client import Client  # noqa: F401
from app.models.payroll import CalculationStatus, _enum_column  # reuse the shared enum column helper
from app.models.site import Site  # noqa: F401


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: Two-place money, as every monetary column in the schema (design, "money is NUMERIC(12,2)").
_MONEY = Numeric(12, 2)


class BillingRecord(Base):
    """One site's computed billing for one calendar month (Requirement 17.1)."""

    __tablename__ = "billing_records"

    # Names given explicitly so they match migration 0001. The unique constraint on
    # (site_id, year, month) is what makes recalculation an upsert; the portable check constraints are
    # declared here under the plain names the migration used so autogenerate does not propose a
    # rename.
    __table_args__ = (
        UniqueConstraint(
            "site_id", "year", "month", name="uq_billing_records_site_id_year_month"
        ),
        CheckConstraint("month BETWEEN 1 AND 12", name="month_range"),
        CheckConstraint("year BETWEEN 2000 AND 2200", name="year_range"),
        CheckConstraint(
            "regular_minutes >= 0 AND overtime_minutes >= 0",
            name="minutes_non_negative",
        ),
        Index("ix_billing_records_client_id_year_month", "client_id", "year", "month"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("clients.id", name="fk_billing_records_client_id_clients"),
        nullable=False,
    )
    site_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sites.id", name="fk_billing_records_site_id_sites"),
        nullable=False,
    )

    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Billable minutes, not hours: hours are only derived at pricing time to avoid rounding drift.
    regular_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: The rates used, copied at calculation time so an already-sent invoice reads back its figure.
    billing_rate_applied: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    #: NULL means overtime billed at the standard rate — no overtime rate configured (Requirement 17.2).
    overtime_rate_applied: Mapped[Decimal | None] = mapped_column(_MONEY, nullable=True)

    #: The authoritative billed amount, computed per work date so a mid-month rate change is honoured.
    total_amount: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=Decimal("0"))

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

    def __repr__(self) -> str:
        return (
            f"BillingRecord(site={self.site_id} {self.year}-{self.month:02d} "
            f"amount={self.total_amount} status={self.status})"
        )
