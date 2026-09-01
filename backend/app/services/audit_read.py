"""Reading the audit trail for the history view (Requirement 13.4, 13.6).

The audit *writer* lives in `app.services.audit`; this is the read side, and the two are kept apart on
purpose. The writer adds rows inside another service's transaction and must never commit; the reader
issues a plain query and owns no transaction. Keeping them in one module would invite a reader to grow
a write, and the whole guarantee of Requirement 13.3 is that no code path updates or deletes an audit
row — there is nothing here that writes, and there is deliberately no function that could.

Two things carry the weight.

**A row is served with its actor's name.** The history view says "Abraham changed check-out…", so the
query left-joins `change_logs.changed_by_user_id` to `users` and returns the username alongside the
row. A left join, not an inner one, because a system change has no actor (`changed_by_user_id` is
null) and must still appear — dropping it would hide exactly the automated corrections a dispute needs
to see, such as a shift closed by a site transition (Requirement 11.8).

**Scoping is a decision about the entity, not a filter on the rows.** Requirement 13.6 lets a site
manager read audit only for entities within their assigned sites. Unlike the hours view, where every
row carries a `site_id` to narrow on, an audit row is polymorphic and carries only an entity type and
id — the site it belongs to lives on the entity, not the row. So the check is made once, before the
rows are read: resolve which site(s) the named entity belongs to, and refuse unless the caller's scope
covers one of them. `may_read_entity_audit` is that resolver, and it is pure of HTTP so the router can
turn a `False` into a 403 and audit the denial the same way every other scope refusal is handled.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from app.core.authz import SiteScope
from app.models.change_log import ChangeLog
from app.models.employee import Employee
from app.models.site import Site
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.services import site as site_service

#: The entity types the history view is offered for (Requirement 13.4): the employee, site and
#: time-entry screens. Each has a defined way to resolve the site(s) it belongs to for a site
#: manager's scope check. An entity type outside this set is one the audit view does not surface;
#: `may_read_entity_audit` refuses it for a scoped caller rather than guessing at its site.
SCOPABLE_ENTITY_TYPES = frozenset({Employee.__tablename__, Site.__tablename__, TimeEntry.__tablename__})


@dataclass(frozen=True, slots=True)
class AuditRow:
    """One audit row alongside the resolved name of the user who made the change.

    A plain carrier so the router builds a response item without a second query per row. `actor_name`
    is `None` for a change the system made on its own account, mirroring the nullable
    `changed_by_user_id` it is resolved from.
    """

    entry: ChangeLog
    actor_name: str | None


@dataclass(frozen=True, slots=True)
class AuditPage:
    """A page of audit rows plus the unfiltered-by-paging total, for list rendering."""

    rows: Sequence[AuditRow]
    total: int


def list_entity_audit(
    session: Session,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> AuditPage:
    """A page of audit rows for one entity, newest first, each with its actor's name (Req 13.4).

    Filtered to the named entity by `entity_type` and `entity_id`, the pair the audit index is built
    on. Ordered `changed_at` descending then `id` descending, so the latest change is at the top and
    two rows written in the same request keep a stable order between pages (Requirement 22.5). The
    actor is resolved by a left join to `users`, so a system change with no actor still appears with a
    `None` name rather than being dropped.

    A read only: no transaction is owned here, and nothing mutates the audit table (Requirement 13.3).
    """
    where = (ChangeLog.entity_type == entity_type, ChangeLog.entity_id == entity_id)

    total = session.scalar(select(func.count()).select_from(ChangeLog).where(*where)) or 0

    statement = (
        select(ChangeLog, User.username)
        .outerjoin(User, User.id == ChangeLog.changed_by_user_id)
        .where(*where)
        .order_by(ChangeLog.changed_at.desc(), ChangeLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = [_row_of(row) for row in session.execute(statement)]
    return AuditPage(rows=rows, total=total)


def list_audit(
    session: Session,
    *,
    entity_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int,
    offset: int,
) -> AuditPage:
    """A page of audit rows across *every* entity, newest first, each with its actor's name (Req 13.4).

    The global feed that heads the audit console. Unlike `list_entity_audit`, which reads one entity's
    history for a screen that already names the entity, this reads the whole trail — every entity type,
    every id — so an administrator can see recent change across the system at a glance. It carries no
    per-row scope decision, and deliberately so: an audit row is polymorphic, and many kinds of row
    (users, payroll and billing records) belong to no site at all, so a site-scoped global query has
    no sound column to narrow on. The router puts this behind the administrator guard for exactly that
    reason; a site manager keeps the per-entity view on cards, which *can* be scoped.

    Two optional filters narrow the feed without changing its shape:

      * `entity_type` — an exact match on `ChangeLog.entity_type`, so the console can show only
        employee changes, only site changes, and so on;
      * a `changed_at` date range — `date_from` treated as the start of its day and `date_to` as the
        end of its day, both in UTC and both inclusive, so a day named at either end is contained in
        the window rather than half-open.

    Ordered `changed_at` descending then `id` descending, the same total order the per-entity read
    uses, so the latest change is at the top and two rows written in the same request keep a stable
    order between pages (Requirement 22.5). The actor is resolved by the same left join to `users`, so
    a system change with no actor still appears with a `None` name rather than being dropped.

    A read only: no transaction is owned here, and nothing mutates the audit table (Requirement 13.3).
    """
    where = _list_filters(entity_type=entity_type, date_from=date_from, date_to=date_to)

    total = session.scalar(select(func.count()).select_from(ChangeLog).where(*where)) or 0

    statement = (
        select(ChangeLog, User.username)
        .outerjoin(User, User.id == ChangeLog.changed_by_user_id)
        .where(*where)
        .order_by(ChangeLog.changed_at.desc(), ChangeLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = [_row_of(row) for row in session.execute(statement)]
    return AuditPage(rows=rows, total=total)


def _list_filters(*, entity_type: str | None, date_from: date | None, date_to: date | None) -> tuple:
    """The `WHERE` terms for the global feed: an optional exact entity type and an inclusive day range.

    `date_from` becomes midnight UTC at the start of its day and `date_to` becomes midnight UTC at the
    start of the *next* day with a strict `<`, which is how "up to and including this day" is expressed
    against a timestamp column — a `<=` on end-of-day would drop a change stamped in the final second.
    An absent filter contributes no term, so the unfiltered feed reads the whole trail.
    """
    terms: list = []
    if entity_type is not None:
        terms.append(ChangeLog.entity_type == entity_type)
    if date_from is not None:
        terms.append(ChangeLog.changed_at >= datetime.combine(date_from, time.min, tzinfo=UTC))
    if date_to is not None:
        start_of_next_day = datetime.combine(date_to, time.min, tzinfo=UTC) + timedelta(days=1)
        terms.append(ChangeLog.changed_at < start_of_next_day)
    return tuple(terms)


def _row_of(row: Row) -> AuditRow:
    entry, actor_name = row
    return AuditRow(entry=entry, actor_name=actor_name)


def may_read_entity_audit(
    session: Session,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    scope: SiteScope,
) -> bool:
    """Whether a caller with `scope` may read the audit history of the named entity (Requirement 13.6).

    An unrestricted scope — an administrator — may read any entity's audit, so the answer is `True`
    without touching the entity. A scoped caller — a site manager — may read audit only for entities
    within their assigned sites, so the entity's site(s) are resolved and the answer is whether the
    scope covers at least one of them:

      * a **site** is scoped by its own id;
      * a **time entry** is scoped by the site it was recorded at;
      * an **employee** is scoped by the sites they are assigned to (Requirement 7.1), so a manager
        may read the audit of an employee expected at any site they run.

    An empty scope permits nothing, and an entity type the view does not surface is refused rather than
    guessed at. This is pure of HTTP: the router turns a `False` into a 403 and records the denial.
    """
    if scope.unrestricted:
        return True
    if scope.is_empty:
        return False
    site_ids = _entity_site_ids(session, entity_type=entity_type, entity_id=entity_id)
    return any(scope.allows(site_id) for site_id in site_ids)


def _entity_site_ids(
    session: Session, *, entity_type: str, entity_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """The site(s) an entity belongs to, for a site manager's scope check (Requirement 13.6).

    Returns the empty set when the entity does not exist or its type is not one the view scopes, which
    makes `may_read_entity_audit` refuse a scoped caller rather than leak the existence of an entity
    outside their sites — the same reasoning `Caller.require_site` uses when a site id is missing.
    """
    if entity_type == Site.__tablename__:
        site = session.get(Site, entity_id)
        return frozenset({site.id}) if site is not None else frozenset()
    if entity_type == TimeEntry.__tablename__:
        site_id = session.scalar(select(TimeEntry.site_id).where(TimeEntry.id == entity_id))
        return frozenset({site_id}) if site_id is not None else frozenset()
    if entity_type == Employee.__tablename__:
        if session.get(Employee, entity_id) is None:
            return frozenset()
        return frozenset(site_service.assigned_site_ids(session, entity_id))
    return frozenset()
