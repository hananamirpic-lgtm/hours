"""The `user_sites` table: which sites a site manager is allowed to see.

A mapping onto the table migration 0001 created, not a definition of it. The design says
site-manager authorization "reads exclusively from this table", and that is the reason this model
exists ahead of `Site`: the authorization layer needs the set of site ids for a user, and nothing
else about a site, so the join table alone is enough to answer Requirement 2.3.

Two absences are deliberate:

* The foreign key on `site_id` to `sites.id`. It exists in the database, but a `ForeignKey` to a
  table with no model cannot be resolved the moment the metadata is used for DDL — which the unit
  tests do, on SQLite. It belongs here the day a `Site` model lands, and the schema-agreement test
  records the gap so it is visible until then.
* Any relationship to `Site`. There is no class to point at. The scope resolver selects ids, which
  is all a permission check needs and one query fewer than loading site rows to throw them away.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Imported for its side effect: the foreign key below names `users`, and resolving it needs the table
# registered on the shared metadata.
from app.models.user import User  # noqa: F401


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UserSite(Base):
    """One (user, site) grant. The pair *is* the fact, so there is no surrogate id."""

    __tablename__ = "user_sites"

    # Named explicitly to match migration 0001. The primary-key name comes from the convention in
    # `app.db.base`, which reproduces PostgreSQL's `<table>_pkey`, so it needs no declaration.
    __table_args__ = (Index("ix_user_sites_site_id", "site_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    #: The database's foreign key to `sites.id` is not declared here — see the module note.
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    def __repr__(self) -> str:
        return f"UserSite(user={self.user_id} site={self.site_id})"
