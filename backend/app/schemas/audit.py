"""Audit-history read schemas (Requirement 13.4, 13.6).

The audit view answers "who changed this, when, and why" for one entity. `GET /api/audit` returns a
page of `change_logs` rows for a named entity, and this module is the shape of that page. It is a read
of the append-only audit table; there is no write schema here, and there is deliberately never one,
because Requirement 13.3 forbids any endpoint that updates or deletes an audit row.

Locale-neutral like every other schema in the system. A row carries the field that changed and its
old and new values as the stable machine strings the audit writer stored — an ISO 8601 date, a plain
`Decimal`, an enum's value, `true`/`false` — plus the actor, the timestamp, the reason and the
request id. The readable sentence of Requirement 13.4 ("30/08 18:42 — Abraham changed check-out from
15:30 to 16:00") is composed by the front end from these parts in the reader's language; the server
never assembles prose, because a sentence built here could not be translated (Requirement 21.6).

The actor is served as a name, resolved by joining `changed_by_user_id` to `users`, so the view can
say who acted without a second request. A `None` actor is the system acting on its own account — a
scheduled job, or a shift closed by a site transition (Requirement 11.8) — and the front end renders
that as "the system" rather than a blank.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AuditEntry(BaseModel):
    """One field-level change from the audit trail, as the history view reads it (Requirement 13.1).

    Every part the readable sentence is built from: the field that changed, its previous and new
    values as the audit writer's stable strings (`None` where the field was unset), who acted, when,
    why, and the request that tied the change together. `old_value` and `new_value` are strings, not
    typed values, because the audit table stores one canonical rendering per value regardless of the
    column's type — the front end formats the string it is given rather than re-parsing it.

    The actor is a name where one is known and `None` for a change the system made on its own account.
    A sensitive field's values arrive as the redaction marker the writer stored, never plaintext, so
    the audit view can say *that* a passport number changed without disclosing it (Requirement 20.2).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    field: str
    old_value: str | None
    new_value: str | None
    #: The acting user's username, or `None` when the system acted on its own account.
    actor_name: str | None
    #: UTC ISO 8601 instant the change was recorded; the front end renders it in local time.
    changed_at: datetime
    reason: str | None
    request_id: str | None


class AuditListResponse(BaseModel):
    """A page of audit entries for one entity, newest first, with the total for pagination.

    Ordered most-recent-first so the history view shows the latest change at the top, which is how a
    reader tracing a disputed hour reads it. The order is total — `changed_at` then `id` — so two rows
    written in the same request (one per changed field) never swap places between pages
    (Requirement 22.5).
    """

    items: list[AuditEntry]
    total: int
    limit: int
    offset: int
