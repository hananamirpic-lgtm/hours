"""The audit table.

One row per changed field, polymorphic over entity type (Requirement 13.1). Append-only: there is no
`updated_at` and no `deleted_at`, because an audit row that can be corrected is not evidence of
anything. The guarantee is enforced by PostgreSQL privileges granted in migration 0001, not by this
class — see `app.db.roles`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Imported for its side effect: the foreign key below names `users`, and resolving it needs the table
# registered on the shared metadata. Without this, importing `change_log` alone and then creating the
# table fails with NoReferencedTableError.
from app.models.user import User  # noqa: F401

#: Written in place of a sensitive field's value. The audit still records *that* the field changed,
#: by whom and why, which is what a dispute needs; recording the value itself would put plaintext
#: PII into a table that carries no encryption and is deliberately never deleted, defeating
#: Requirement 20.2. The plaintext of the current value remains available, decrypted, on the entity.
REDACTED = "[redacted]"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ChangeLog(Base):
    """A single field-level change."""

    __tablename__ = "change_logs"

    # Declared here as well as in migration 0001, and with the same names, so autogenerate compares
    # like with like instead of proposing to drop them. The first index serves the per-entity audit
    # panel, the second serves "what did this user change".
    __table_args__ = (
        Index(
            "ix_change_logs_entity_type_entity_id_changed_at",
            "entity_type",
            "entity_id",
            "changed_at",
        ),
        Index("ix_change_logs_changed_by_user_id_changed_at", "changed_by_user_id", "changed_at"),
    )

    # Defaults are Python-side even though the columns carry database defaults too. The database
    # default is the backstop for any writer that is not this ORM; generating the value here means
    # the caller can read `entry.id` before the transaction commits, which the audit view needs when
    # it links a change to the request that made it.
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    #: Table name of the entity that changed — `employees`, `time_entries`, and so on. A name rather
    #: than an enum, so adding an entity needs no migration.
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    #: Null for a change the system made on its own account — a scheduled job, or a shift closed by
    #: a site transition.
    #:
    #: The foreign key matches migration 0001. It could only be declared once a `User` model existed,
    #: because a `ForeignKey` to a table with no model fails to resolve the moment the metadata is
    #: used for DDL; the constraint name is left to the convention in `app.db.base`, which produces
    #: exactly the `fk_change_logs_changed_by_user_id_users` the migration created.
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    #: `timestamptz`, stored in UTC like every other timestamp in the schema. Rendering into
    #: Asia/Jerusalem for the audit view is the presentation layer's job.
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    field: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Mandatory at the API boundary for a manual correction (Requirement 12.2); nullable here
    #: because a plain field edit has no reason to give beyond the change itself.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Ties every row written by one request together, which is what turns a list of field changes
    #: back into "this is what that correction did".
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"ChangeLog({self.entity_type}:{self.entity_id} {self.field} "
            f"{self.old_value!r} -> {self.new_value!r})"
        )
