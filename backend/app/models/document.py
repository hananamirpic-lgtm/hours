"""The `documents` table.

A mapping onto the table migration 0001 created, not a definition of it. Every column name,
constraint name and index name reproduces what the migration wrote, so Alembic autogenerate compares
like with like instead of proposing to change a schema that is already correct.

A document row is metadata *about* a file, never the file itself: `file_key` is the object-storage
key, and the bytes only ever move between the client and private storage through a short-lived signed
URL (Requirement 4.3). Nothing in this table is servable on its own — a leaked row discloses a
file name and an expiry date, not a passport scan.

Deletion is soft (`deleted_at`), the same rule the rest of the schema follows: an expired-permit
dispute later needs to show that the document existed and when it was removed, which a hard delete
destroys. The service filters deleted rows out of every read.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DocumentType(enum.StrEnum):
    """Requirement 4.1. Values match the `document_type` PostgreSQL enum."""

    PASSPORT = "passport"
    WORK_PERMIT = "work_permit"
    OTHER = "other"


def _enum_column(enum_type: type[enum.Enum], name: str) -> Enum:
    return Enum(
        enum_type,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Document(Base):
    """A file held against an employee: a passport, a work permit, or something else."""

    __tablename__ = "documents"

    # Names given explicitly so they match migration 0001. The check constraint is named plainly
    # there (`size_bytes_positive`), so it is declared here without a name-mangling collision — the
    # convention in `app.db.base` only fills names that were left unset.
    __table_args__ = (
        UniqueConstraint("file_key", name="uq_documents_file_key"),
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
        Index("ix_documents_employee_id", "employee_id"),
        Index("ix_documents_expiry_date", "expiry_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("employees.id", name="fk_documents_employee_id_employees"),
        nullable=False,
    )

    type: Mapped[DocumentType] = mapped_column(_enum_column(DocumentType, "document_type"), nullable=False)

    #: Object-storage key. The unique constraint means one key maps to one row: a re-used key would
    #: let two rows point at the same file and a delete of one orphan the other.
    file_key: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: Optional (Requirement 4.1). A document with no expiry — say a reference letter — never trips
    #: the sweep; one with an expiry drives the warning and escalation of 4.4 and 4.5.
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    #: The user who uploaded it, null for a system import. FK to `users` exists in the database.
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", name="fk_documents_uploaded_by_user_id_users"),
        nullable=True,
    )

    #: Set when the document is removed. A soft delete keeps the row for the audit trail; the service
    #: excludes deleted rows from every read and from the expiry sweep.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def is_expired_on(self, on_date: date) -> bool:
        """Whether the document's expiry has passed as of `on_date` (Requirement 4.5).

        A document with no expiry never expires. The comparison is strict — a document expiring
        today is still valid today and expired tomorrow — which matches how the warning window and
        the escalation divide the timeline with no gap and no overlap.
        """
        return self.expiry_date is not None and self.expiry_date < on_date

    def __repr__(self) -> str:
        return f"Document({self.type} employee={self.employee_id} expiry={self.expiry_date})"
