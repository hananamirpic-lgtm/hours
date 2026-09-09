"""Audit-history router (Requirement 13.4, 13.6).

HTTP only, and read only. `GET /api/audit?entity_type=&entity_id=` returns the change history of one
entity for the history view: guard the caller, decide whether they may see this entity's audit, read
the page, shape the response. There is no write endpoint here, and there is deliberately never one —
Requirement 13.3 makes the audit trail append-only, so the only verb this router mounts is `GET`. The
append-only guarantee itself is enforced in the database (UPDATE and DELETE on `change_logs` are
revoked from the application role in migration 0001); the absence of a write route here is the second
line of the same defence.

Two authorization concerns meet, matching how the site router handles a single-site read.

**Who may read at all.** Requirement 13.6 makes audit readable by administrators and, within their
sites, by site managers. The role guard admits the site-manager role and the administrator (every
`require_roles` guard adds the administrator, by Requirement 2.2); accounting and the employee role
are absent, so they get a 403 before any entity is touched.

**Which entities a manager may read.** A site manager may read audit only for entities within their
assigned sites (Requirement 13.6). Unlike the hours view there is no per-row `site_id` to scope on —
an audit row is polymorphic — so the decision is made once about the entity: `may_read_entity_audit`
resolves the entity's site(s) and answers whether the caller's scope covers one. A refusal is audited
and returned as a 403 through `caller.deny`, the same envelope every scope refusal uses. An
administrator's unrestricted scope passes unconditionally.

Locale-neutral: each row carries the field, the old and new values as the audit writer's stable
strings, the actor's name, the timestamp, the reason and the request id, and the front end composes
the readable sentence of Requirement 13.4 in the reader's language.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CODE_SITE_OUT_OF_SCOPE, DbSession, OperationsCaller, SiteManagerCaller
from app.schemas.audit import AuditEntry, AuditListResponse
from app.services import audit_read as audit_read_service
from app.services import authz as authz_service

router = APIRouter(prefix="/audit", tags=["audit"])


def _entry_of(row: audit_read_service.AuditRow) -> AuditEntry:
    """One audit row shaped as the locale-neutral response item both audit endpoints return."""
    return AuditEntry(
        id=row.entry.id,
        entity_type=row.entry.entity_type,
        entity_id=row.entry.entity_id,
        field=row.entry.field,
        old_value=row.entry.old_value,
        new_value=row.entry.new_value,
        actor_name=row.actor_name,
        changed_at=row.entry.changed_at,
        reason=row.entry.reason,
        request_id=row.entry.request_id,
    )


@router.get(
    "",
    response_model=AuditListResponse,
    summary="Read the audit history of one entity",
    description=(
        "The change history of a single entity, newest first, for the audit view (Requirement 13.4). "
        "Every mutation is recorded field by field with the acting user, the time, the previous and "
        "new values and the reason (Requirement 13.1); this returns them for the named entity so the "
        "employee, site and time-entry screens can render a readable history. Administrators may read "
        "any entity's audit; a site manager may read audit only for entities within their assigned "
        "sites (Requirement 13.6) — the employee, site or time entry must belong to a site they run. "
        "Read-only: the audit trail is append-only and there is no endpoint to change it "
        "(Requirement 13.3)."
    ),
    responses={
        200: {"model": AuditListResponse, "description": "The entity's audit history, newest first"},
        403: {"description": "The entity is outside the caller's assigned sites"},
    },
)
def read_entity_audit(
    caller: SiteManagerCaller,
    session: DbSession,
    entity_type: Annotated[
        str, Query(description="The entity's table name, e.g. `time_entries`, `sites`, `employees`.")
    ],
    entity_id: Annotated[uuid.UUID, Query(description="The id of the entity to read the history of.")],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditListResponse:
    # Requirement 13.6: an administrator reads any entity's audit; a site manager only for entities
    # within their sites. The decision is about the entity, resolved once before the rows are read;
    # a refusal is audited and returned as a 403, the same shape every scope refusal uses.
    if not audit_read_service.may_read_entity_audit(
        session, entity_type=entity_type, entity_id=entity_id, scope=caller.scope
    ):
        raise caller.deny(
            code=CODE_SITE_OUT_OF_SCOPE,
            reason=authz_service.REASON_SITE_OUT_OF_SCOPE,
            detail=f"audit {entity_type}={entity_id}",
        )

    page = audit_read_service.list_entity_audit(
        session, entity_type=entity_type, entity_id=entity_id, limit=limit, offset=offset
    )
    items = [_entry_of(row) for row in page.rows]
    return AuditListResponse(items=items, total=page.total, limit=limit, offset=offset)


@router.get(
    "/all",
    response_model=AuditListResponse,
    summary="Read recent audit changes across every entity (administrators only)",
    description=(
        "The global change feed for the audit console: recent changes across every entity, newest "
        "first (Requirement 13.4). This is deliberately **administrator-only**, and the reason is the "
        "shape of the data, not a matter of trust. An audit row is polymorphic — it names an entity "
        "type and id, not a site — and many kinds of row (users, payroll records, billing records) "
        "belong to no site at all, so there is no sound way to narrow a cross-entity feed to a site "
        "manager's assigned sites; a scoped global query would silently drop or leak rows. An "
        "administrator's scope is unrestricted, so the feed is served to them whole. A site manager "
        "keeps the per-entity audit on the employee, site and time-entry screens (`GET /api/audit`), "
        "which *can* be scoped, and receives a 403 here. Optional filters narrow the feed without "
        "changing its shape: an exact `entity_type`, and an inclusive `changed_at` date range. "
        "Read-only: the audit trail is append-only and there is no endpoint to change it "
        "(Requirement 13.3)."
    ),
    responses={
        200: {"model": AuditListResponse, "description": "Recent changes across all entities"},
        403: {"description": "The caller is not an administrator"},
    },
)
def read_all_audit(
    caller: OperationsCaller,
    session: DbSession,
    entity_type: Annotated[
        str | None,
        Query(description="Narrow to one entity's table name, e.g. `employees`, `sites`, `users`."),
    ] = None,
    date_from: Annotated[
        date | None, Query(description="Include changes from this day onward (inclusive, UTC).")
    ] = None,
    date_to: Annotated[
        date | None, Query(description="Include changes up to and including this day (inclusive, UTC).")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditListResponse:
    # The administrator guard is the whole access decision: the feed is polymorphic and many rows have
    # no site, so there is no per-row scope to apply here (see the docstring). `caller` is bound so the
    # guard runs; its unrestricted scope needs no further check before the read.
    _ = caller
    page = audit_read_service.list_audit(
        session,
        entity_type=entity_type,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    items = [_entry_of(row) for row in page.rows]
    return AuditListResponse(items=items, total=page.total, limit=limit, offset=offset)
