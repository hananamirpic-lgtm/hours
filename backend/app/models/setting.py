"""The `settings` table.

A mapping onto the table migration 0001 created, not a definition of it. Every setting is one row of
`key`, a text `value`, and a `value_type` that says how to read that text back. One row shape serves
every knob, and `app.services.settings` is the single place the text-to-value conversion happens, so
a read cannot disagree with a write about what an integer setting means.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SettingValueType(enum.StrEnum):
    """How a setting's text `value` is interpreted. Values match the `setting_value_type` enum."""

    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    TIME = "time"
    JSON = "json"


def _enum_column(enum_type: type[enum.Enum], name: str) -> Enum:
    return Enum(
        enum_type,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Setting(Base):
    """One tuning knob, read through the typed accessors in `app.services.settings`."""

    __tablename__ = "settings"

    __table_args__ = (UniqueConstraint("key", name="uq_settings_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[SettingValueType] = mapped_column(
        _enum_column(SettingValueType, "setting_value_type"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The FK to `users` exists in the database; it is not declared here because the write side of
    #: settings is a later task and nothing yet reads the relationship.
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"Setting({self.key!r}={self.value!r} type={self.value_type})"
