"""The `notifications` table.

A mapping onto the table migration 0001 created, not a definition of it. A notification is stored as
a translation *key* plus parameters, never a rendered sentence, so the recipient's language is
applied when it is read rather than frozen when it is written (Requirement 21.6).

The load-bearing column is `dedupe_key`. It is unique, and that uniqueness is what makes every
generator idempotent (Requirements 4.6, 14.6): a job that runs twice in a day computes the same key
the second time and the insert collides instead of sending a duplicate. The generator does not check
"have I sent this already" and then insert — that check-then-act races with itself — it inserts and
lets the unique constraint be the arbiter, and treats a collision as "already sent".

The two collection columns are PostgreSQL types in production and portable ones under the SQLite the
unit tests run on. `JSONB` carries the parameters; a plain `JSON` variant stands in on SQLite. The
delivered-channels list is a PostgreSQL text array with a `JSON` variant likewise, so a test can
assert what was delivered without a PostgreSQL server.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base


class NotificationSeverity(enum.StrEnum):
    """Requirement 4.4/4.5 and 14.x. Values match the `notification_severity` PostgreSQL enum."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


def _enum_column(enum_type: type[enum.Enum], name: str) -> Enum:
    return Enum(
        enum_type,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: JSONB in PostgreSQL, plain JSON on SQLite. The variant keeps the production column a real JSONB —
#: indexable, queryable — while the unit-test engine, which has no JSONB, gets a column it can still
#: round-trip a dict through.
_JSON_PARAMS = JSON().with_variant(postgresql.JSONB(), "postgresql")

#: A text array in PostgreSQL, a JSON list on SQLite. Same reasoning: production keeps a native
#: array, the test engine keeps a list it can read back.
_TEXT_ARRAY = JSON().with_variant(postgresql.ARRAY(Text()), "postgresql")


class Notification(Base):
    """One notification to one recipient, rendered into their language at read time."""

    __tablename__ = "notifications"

    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_notifications_dedupe_key"),
        Index(
            "ix_notifications_recipient_user_id_is_read_created_at",
            "recipient_user_id",
            "is_read",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", name="fk_notifications_recipient_user_id_users"),
        nullable=False,
    )

    #: A machine type (for example `document_expiry_warning`), not display text. The front end and
    #: the email renderer switch on it.
    type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[NotificationSeverity] = mapped_column(
        _enum_column(NotificationSeverity, "notification_severity"),
        nullable=False,
        default=NotificationSeverity.INFO,
    )

    #: Translation key plus its parameters. Rendered in the recipient's language when read.
    title_key: Mapped[str] = mapped_column(Text, nullable=False)
    body_params: Mapped[dict] = mapped_column(_JSON_PARAMS, nullable=False, default=dict)

    related_entity_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    #: Unique. The whole idempotency guarantee rests on this column — see the module note.
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)

    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    delivered_channels: Mapped[list] = mapped_column(_TEXT_ARRAY, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"Notification({self.type} -> {self.recipient_user_id} read={self.is_read})"
