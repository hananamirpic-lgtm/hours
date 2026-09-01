"""The `clients` table.

A mapping onto the table migration 0001 created, not a definition of it. Every column name,
constraint name and index name reproduces what the migration wrote, so Alembic autogenerate compares
like with like instead of proposing to change a schema that is already correct.

A client is the entity billed for work done at the sites it owns (Requirement 5). Nothing here is
encrypted: a client is a business, not a person, and its contact details are ordinary commercial
information rather than the identity documents the employee columns guard.

There is no hard delete. Requirement 5.4 says a client whose sites carry time entries may not be
deleted and must be archived instead, and `is_archived` is what carries that: an archived client
keeps its history and stops appearing in the default list. The rule that decides delete-versus-archive
lives in the service, because it depends on the sites and time entries this model deliberately does
not reach into.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Client(Base):
    """A business the work is billed to. Owns many sites; never hard-deleted."""

    __tablename__ = "clients"

    # Names given explicitly so they match migration 0001. The unique constraint on `company_number`
    # and the `payment_terms_days` check are declared here under the plain names the migration used,
    # so autogenerate sees no rename. The naming convention in `app.db.base` only fills names left
    # unset, so an explicit name passes through unchanged.
    __table_args__ = (
        UniqueConstraint("company_number", name="uq_clients_company_number"),
        CheckConstraint(
            "payment_terms_days IS NULL OR payment_terms_days >= 0",
            name="payment_terms_days_non_negative",
        ),
        Index("ix_clients_name", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    #: Mandatory (Requirement 5.2). The one field a client cannot exist without.
    name: Mapped[str] = mapped_column(Text, nullable=False)

    #: Optional commercial detail (Requirement 5.1). `company_number` is unique when present; the
    #: uniqueness is declared in the migration and enforced in the service, since NULLs do not collide
    #: in the constraint and several clients may legitimately have none.
    company: Mapped[str | None] = mapped_column(Text, nullable=True)
    company_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_person: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Payment terms as an integer number of days plus optional free text (Requirement 5.5), so
    #: "30 days" stays computable while a note like "net on delivery" is still recordable.
    payment_terms_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_terms_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: True once the client has been archived instead of deleted (Requirement 5.4). An archived
    #: client keeps every site and time entry it owns; it simply drops out of the default listing.
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"Client({self.name!r} archived={self.is_archived})"
