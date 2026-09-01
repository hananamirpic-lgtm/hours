"""Test-only ORM models.

The encryption types and the audit writer are general: they work on any mapped class, and neither
should be tested through whichever business entity happens to be the first to use them. A stand-in
keeps the tests about the primitives, and it exists before the real models do.

Its metadata is deliberately separate from `app.db.base.Base`. A table defined for a test must never
reach the application's metadata, or Alembic autogenerate would propose a migration creating it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import DateTime, Integer, Text, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import DeterministicHash, EncryptedDate, EncryptedString


class SampleBase(DeclarativeBase):
    """Metadata for test-only tables."""


class SampleSiteScoped(SampleBase):
    """Stand-in for a table carrying a `site_id`, for the query-level site scoping tests.

    `time_entries` is the real subject and does not have a model yet. What is under test is that
    `apply_site_scope` puts the restriction in the `WHERE` clause of whatever statement it is given,
    which is a property of the scoping helper and not of any particular table — so a stand-in tests it
    honestly, and keeps the test from being rewritten when the real model lands.
    """

    __tablename__ = "sample_site_scoped"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)


class SampleEntity(SampleBase):
    """Stand-in for an audited business entity.

    Shaped like `employees` in the ways that matter here: a mandatory plain column, a nullable one, a
    number, an encrypted string, an encrypted date, and the deterministic hash that accompanies the
    encrypted value it has to keep unique.
    """

    __tablename__ = "sample_entities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[str | None] = mapped_column(Text, nullable=True)
    notice_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passport_number: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    passport_number_hash: Mapped[str | None] = mapped_column(DeterministicHash, nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(EncryptedDate, nullable=True)
    # Present so the audit diff has a bookkeeping column to prove it ignores.
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
