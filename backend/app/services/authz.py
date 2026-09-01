"""Applying the authorization policy: resolve a caller's site scope, scope a query, audit a refusal.

The policy itself is in `app.core.authz` and is pure. This module is the part that needs a session:
reading `user_sites`, folding a scope into a `SELECT`, and writing the denial record Requirement 2.8
asks for. Nothing here raises an HTTP error — `app.api.deps` owns that, because a service that knows
about status codes cannot be called from a scheduled job.

**Scoping is done in SQL, not in Python.** `apply_site_scope` returns a narrowed statement. Filtering
the rows after the fetch would be simpler to write and wrong twice over: it pages over rows the caller
may not see, so a manager's first page can come back empty while their data sits on page three, and it
moves data the caller has no right to into the application's memory before deciding that.

**A denial is audited even though the request fails.** Requirement 2.8 wants the attempt recorded, and
an attempt that leaves no trace because the transaction was abandoned is not recorded. So
`record_denial` commits, unlike the rest of the service layer. It is the last thing that happens in
the request — the caller raises immediately afterwards — so there is no other work in the session for
that commit to sweep up.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from typing import TypeVar

from sqlalchemy import ColumnElement, Select, false, select
from sqlalchemy.orm import Session

from app.core.authz import SiteScope, scope_for_role
from app.models.user import User
from app.models.user_site import UserSite
from app.services.audit import AuditContext, record_change

logger = logging.getLogger(__name__)

#: Every authorization event lands on this field of the `users` entity, mirroring how
#: `app.services.auth` records authentication events, so the audit view can render one stream per user
#: rather than needing a second query to find out what a user was refused.
AUDIT_FIELD = "authorization"

EVENT_ACCESS_DENIED = "access_denied"

#: Why a request was refused. Machine values, because the audit view renders them in the reader's
#: language (Requirement 21.6) and cannot translate a sentence composed here.
REASON_ROLE_NOT_PERMITTED = "role_not_permitted"
REASON_SITE_OUT_OF_SCOPE = "site_out_of_scope"
REASON_NOT_OWN_RECORD = "not_own_record"

_Statement = TypeVar("_Statement", bound=Select)


# --------------------------------------------------------------------------- scope resolution


def assigned_site_ids(session: Session, user: User) -> frozenset[uuid.UUID]:
    """The site ids granted to `user` by `user_sites`.

    Ids only. Loading site rows to read their primary keys would be a join and a second table's worth
    of data for a question that is answered by the join table alone.
    """
    return frozenset(session.scalars(select(UserSite.site_id).where(UserSite.user_id == user.id)))


def resolve_site_scope(session: Session, user: User) -> SiteScope:
    """The sites `user` may read and write.

    Unrestricted for an administrator and for accounting, so no query is made for them: their scope
    does not depend on any assignment, and asking would be a round trip whose answer is discarded.
    A site manager's scope is exactly their `user_sites` rows (Requirement 2.3), and an employee's is
    empty because their access is to their own record rather than to a site (Requirement 2.7).
    """
    tentative = scope_for_role(user.role, ())
    if tentative.unrestricted:
        return tentative
    return scope_for_role(user.role, assigned_site_ids(session, user))


# --------------------------------------------------------------------------- query scoping


def apply_site_scope(
    statement: _Statement,
    site_column: ColumnElement[uuid.UUID],
    scope: SiteScope,
) -> _Statement:
    """Narrow `statement` to the sites `scope` permits.

    Returned unchanged for an unrestricted scope, so an administrator's query carries no redundant
    predicate. An empty restricted scope becomes `WHERE false`: a site manager with no assignment sees
    nothing, and the query still returns a well-formed empty page rather than the caller having to
    special-case it. `IN` with an empty list would do the same thing in most databases, but relying on
    that is relying on the one behaviour that, if it ever differed, would fail open.
    """
    if scope.unrestricted:
        return statement
    if not scope.site_ids:
        return statement.where(false())
    return statement.where(site_column.in_(scope.site_ids))


# --------------------------------------------------------------------------- denial audit


def record_denial(
    session: Session,
    *,
    user: User,
    action: str,
    reason: str,
    context: AuditContext,
    detail: str | None = None,
) -> None:
    """Record a refused request against the caller (Requirement 2.8), and commit it.

    `action` is what was attempted, in a form a reader recognises — `GET /api/employees` — and `detail`
    narrows it to the resource where naming one helps, such as the site that was out of scope. Neither
    carries a translatable message; `reason` is the machine value the audit view renders.

    The audit row is attributed to the caller and its `entity_id` is the caller's own id. Attributing
    it to the resource would read better in the resource's audit panel and is not available: the
    resource may not exist, and the whole point of the check is that this user has no established
    relationship to it.
    """
    attempted = action if detail is None else f"{action} {detail}"
    record_change(
        session,
        entity_type="users",
        entity_id=user.id,
        field=AUDIT_FIELD,
        old_value=None,
        new_value=f"{EVENT_ACCESS_DENIED}: {attempted}",
        context=replace(context, actor_user_id=user.id),
        reason=reason,
    )
    session.commit()
    logger.info(
        "authorization denied user=%s role=%s action=%s reason=%s",
        user.id,
        user.role,
        action,
        reason,
    )
