"""The `period_locks` table.

A mapping onto the table migration 0001 created, not a definition of it. Every column name and
constraint name reproduces what the migration wrote, so Alembic autogenerate compares like with like
instead of proposing to change a schema that is already correct.

A period is a calendar month, identified by `(year, month)`, unique together. A row exists once a
month has been touched by the lock workflow; the *state* is read from the timestamps rather than a
status column:

* `locked_at` set and `unlocked_at` null  → the month is **locked**,
* `locked_at` set and `unlocked_at` set   → the month was locked and has since been unlocked,
* neither set                             → the row is a placeholder with no effect.

The approval-and-locking workflow (Requirement 15) owns writing these rows; that is a later task.
This model lands with the scan endpoint because the check-in guard has to refuse a scan into a locked
month (Requirement 9.5), and `is_period_locked` is the single read that answers it. The
`unlock_requires_reason` check the migration writes uses `btrim`/`coalesce`, which SQLite (the
unit-test engine) has no function for, so it stays in the migration alone — the same treatment the
time-entry reason checks and the GiST constraints get.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Imported for the foreign keys below, so the referenced table is on the shared metadata.
from app.models.user import User  # noqa: F401


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PeriodLock(Base):
    """One calendar month's lock state (Requirement 15.4, 15.5, 15.6)."""

    __tablename__ = "period_locks"

    # Names given explicitly so they match migration 0001. The `unlock_requires_reason` check the
    # migration writes uses `btrim`/`coalesce` and so cannot be created on SQLite; it stays in the
    # migration, which is the PostgreSQL-only backstop, and is not declared here.
    __table_args__ = (
        UniqueConstraint("year", "month", name="uq_period_locks_year_month"),
        CheckConstraint("month BETWEEN 1 AND 12", name="month_range"),
        CheckConstraint("year BETWEEN 2000 AND 2200", name="year_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)

    locked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", name="fk_period_locks_locked_by_user_id_users"),
        nullable=True,
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    unlocked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", name="fk_period_locks_unlocked_by_user_id_users"),
        nullable=True,
    )
    unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unlock_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    @property
    def is_locked(self) -> bool:
        """Whether this month is currently locked: locked once and not since unlocked.

        The state is derived from the timestamps rather than stored, so an unlock cannot leave a
        stale "locked" flag behind — clearing the lock is setting `unlocked_at`, and this reads it.
        """
        return self.locked_at is not None and self.unlocked_at is None

    def __repr__(self) -> str:
        return f"PeriodLock(year={self.year} month={self.month} locked={self.is_locked})"
