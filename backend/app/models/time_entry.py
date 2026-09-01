"""The `time_entries` table.

A mapping onto the table migration 0001 created, not a definition of it. Every column name and
constraint name reproduces what the migration wrote, so Alembic autogenerate compares like with like
instead of proposing to change a schema that is already correct.

The three load-bearing database objects — the partial unique index that allows one open entry per
employee, the GiST exclusion constraint that forbids overlapping completed entries, and the checks
that a check-out follows its check-in — cannot be expressed as model attributes and live in the
migration. They are PostgreSQL-only; the unit-test engine (SQLite) has none of them, which is why
`tests/integration/test_time_entries_constraints.py` proves them against a real PostgreSQL.

This model lands with the client CRUD task because the delete-versus-archive rule (Requirement 5.4)
turns on whether a client's sites carry any time entry. That check needs to read this table but none
of its money logic; the scan, check-out and manual-entry services are later tasks. The `flags`
column is stored as a PostgreSQL text array and given a SQLite-compatible fallback so the unit-test
schema builds, since no test here reads a flag.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    Uuid,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Imported for the foreign keys below, so the referenced tables are on the shared metadata.
from app.models.employee import Employee  # noqa: F401
from app.models.site import Site  # noqa: F401
from app.models.user import User  # noqa: F401


class TimeEntrySource(enum.StrEnum):
    """How an entry came to exist. Values match the `time_entry_source` PostgreSQL enum."""

    QR_SCAN = "qr_scan"
    MANUAL = "manual"
    SYSTEM_TRANSITION = "system_transition"


class TimeEntryStatus(enum.StrEnum):
    """The approval status of an entry (Requirement 15.1). Values match `time_entry_status`."""

    DRAFT = "draft"
    REVIEW = "review"
    APPROVED = "approved"
    LOCKED = "locked"


def _enum_column(enum_type: type[enum.Enum], name: str) -> Enum:
    return Enum(
        enum_type,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: A text array in PostgreSQL, so a new anomaly flag needs no migration. SQLite has no array type, so
#: the unit-test schema falls back to `JSON`, which stores a list as a JSON string and reads it back
#: as a list — enough for a round trip on that engine. The migration is the source of truth for the
#: production column; no unit test reads a flag, only inserts an empty list.
_FLAGS_TYPE = postgresql.ARRAY(Text()).with_variant(JSON(), "sqlite")


class TimeEntry(Base):
    """One recorded shift: an employee at a site, from a check-in to a check-out."""

    __tablename__ = "time_entries"

    # Names given explicitly so they match migration 0001. Only the portable check constraints are
    # declared here. The two reason-required checks the migration writes use `btrim`/`coalesce`,
    # which SQLite (the unit-test engine) has no function for, so they stay in the migration alone —
    # the same treatment the partial unique index and the GiST exclusion constraint get, since none
    # can be created on SQLite. They are the PostgreSQL-only backstop the integration tests cover.
    __table_args__ = (
        CheckConstraint(
            "check_out_at IS NULL OR check_out_at > check_in_at",
            name="check_out_after_check_in",
        ),
        CheckConstraint(
            "total_minutes IS NULL OR total_minutes >= 0",
            name="total_minutes_non_negative",
        ),
        Index("ix_time_entries_employee_id_work_date", "employee_id", "work_date"),
        Index("ix_time_entries_site_id_work_date", "site_id", "work_date"),
        Index("ix_time_entries_status_work_date", "status", "work_date"),
        Index("ix_time_entries_work_date", "work_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("employees.id", name="fk_time_entries_employee_id_employees"),
        nullable=False,
    )
    site_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sites.id", name="fk_time_entries_site_id_sites"),
        nullable=False,
    )

    #: Local date of check-in, written by the service. A shift crossing midnight belongs to the day
    #: it started.
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    check_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    check_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    source: Mapped[TimeEntrySource] = mapped_column(
        _enum_column(TimeEntrySource, "time_entry_source"), nullable=False
    )
    is_manual: Mapped[bool] = mapped_column(default=False, nullable=False)
    manual_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[TimeEntryStatus] = mapped_column(
        _enum_column(TimeEntryStatus, "time_entry_status"), nullable=False, default=TimeEntryStatus.DRAFT
    )

    flags: Mapped[list[str]] = mapped_column(_FLAGS_TYPE, nullable=False, default=list)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", name="fk_time_entries_created_by_user_id_users"),
        nullable=True,
    )
    closed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", name="fk_time_entries_closed_by_user_id_users"),
        nullable=True,
    )

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delete_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"TimeEntry(employee={self.employee_id} site={self.site_id} date={self.work_date})"
