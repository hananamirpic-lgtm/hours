"""The `staffing_companies` table.

A mapping onto the table migration 0007 creates, not a definition of it — the same relationship every
other model in this package has to its migration. A staffing company is an external labour provider
that supplies employees; each employee links back to at most one of these through
`employees.staffing_company_id` (declared on the `Employee` side, which owns the foreign key).

Nothing here is encrypted. A staffing company's name, contact person and switchboard telephone are
business contact details, not sensitive personal data, so they are plain `Text` exactly as an
employee's `country` is — unlike the employee's own `phone`, which is encrypted. `hourly_rate` is a
plain `Numeric`, the single flat rate the staffing-company report multiplies worked hours by.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class StaffingCompany(Base):
    """An external labour provider that supplies employees (Requirement 1)."""

    __tablename__ = "staffing_companies"

    # Python-side default so a caller can read `company.id` before the transaction commits; the
    # database's `gen_random_uuid()` in the migration is the backstop for any other writer.
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    #: Mandatory (Requirement 1.4). The provider's name and a contact person are the two fields a
    #: staffing company cannot exist without; the service and schema enforce non-blank.
    name: Mapped[str] = mapped_column(Text, nullable=False)
    contact_person: Mapped[str] = mapped_column(Text, nullable=False)

    #: Optional (Requirement 1.5). The single flat rate the staffing-company report multiplies total
    #: worked hours by (Requirement 4.5); null means no rate is set and the report shows payment as
    #: unavailable rather than zero (Requirement 4.7).
    hourly_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    #: Optional (Requirement 1.5). Business contact detail, not sensitive, so plain `Text`.
    telephone: Mapped[str | None] = mapped_column(Text, nullable=True)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"StaffingCompany({self.name!r} contact={self.contact_person!r})"