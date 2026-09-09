"""Site records, their billing-rate history, and employee assignment (Requirement 6, 7).

The service owns every business rule; the router only translates. Nothing here raises an HTTP error
or commits — the caller's unit of work decides the fate of the change and its audit rows together,
which is the invariant `app.services.audit` rests on. This mirrors `app.services.employee` and
`app.services.client` exactly, so the three read the same way.

Four rules carry the weight.

**Site number is unique across all sites, checked in code (Requirement 6.3).** The database has a
unique constraint that says the same, but the application check runs before the insert, excludes the
row being updated, and names the conflicting site — which a bare uniqueness violation could never do.

**Billing-rate history is a non-overlapping chain, reconciled from a submitted list (Requirement
6.4, 6.6).** A client sends the history it wants; the service sorts it, checks no two rows overlap,
and replaces what was there. The resolver then answers "the billing rate in force on this date" by
finding the one row whose period covers it — which is what makes updating a rate leave an
already-billed period untouched (6.6): the old row is not edited, a new one is chained after it. This
is the same shape as `app.services.employee.replace_rate_history`, deliberately.

**Assignment is a many-to-many that never restricts where time is recorded (Requirement 7.1, 7.2).**
`replace_site_employees` and `replace_employee_sites` maintain the `employee_sites` table from the
two sides. The set is replaced, not appended to. A row here records an *expectation*; nothing in the
scan path reads it to allow or deny a check-in — that is `sites.assignment_mode`'s job, and even then
it is an expectation control, not a location one.

**A site manager is assigned by writing `user_sites` (Requirement 2.3, 6.7).** Setting a site's
`manager_user_id` also writes the `user_sites` grant the authorization layer reads, so a manager who
is put on a site can immediately see it. Clearing or changing the manager removes the stale grant.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session, selectinload

from app.core import qr_token
from app.models.employee import Employee
from app.models.site import EmployeeSite, Site, SiteRate, SiteStatus
from app.models.user_site import UserSite
from app.schemas.site import SiteCreate, SiteRateInput, SiteUpdate
from app.services.audit import AuditContext, record_change, record_model_changes, snapshot

# --------------------------------------------------------------------------- errors
# Domain errors, not HTTP errors, each carrying a machine `code` the router lifts into the error
# envelope — the same shape `app.services.employee` and `app.services.client` use.


class SiteError(Exception):
    """Base for every site-service failure. `code` is what the front end translates."""

    code = "site_error"


class SiteNotFound(SiteError):
    code = "site_not_found"

    def __init__(self, site_id: uuid.UUID) -> None:
        super().__init__(f"no site {site_id}")
        self.site_id = site_id


class DuplicateSiteNumber(SiteError):
    """Another site already holds this site number (Requirement 6.3).

    Carries the conflicting site so the router can name it, which a bare uniqueness violation could
    never supply.
    """

    code = "duplicate_site_number"

    def __init__(self, conflicting: Site) -> None:
        super().__init__(f"site number already held by site {conflicting.id}")
        self.conflicting = conflicting


class OverlappingRates(SiteError):
    """Two submitted billing rows cover the same date (Requirement 6.4, 6.6)."""

    code = "overlapping_rates"


class InvalidRatePeriod(SiteError):
    """A billing row whose `effective_to` precedes its `effective_from`."""

    code = "invalid_rate_period"


class EmployeeNotFound(SiteError):
    """An assignment named an employee id that does not exist (Requirement 7.1)."""

    code = "employee_not_found"

    def __init__(self, employee_id: uuid.UUID) -> None:
        super().__init__(f"no employee {employee_id}")
        self.employee_id = employee_id


# --------------------------------------------------------------------------- reads


def base_select() -> Select[tuple[Site]]:
    """The site query with its rate history eager-loaded.

    Exposed so the router can hand it to `Caller.scope_query` to narrow a manager's list to their
    sites before it reaches `list_sites`, rather than reaching into a private helper.
    """
    return select(Site).options(selectinload(Site.rates))


def get_site(session: Session, site_id: uuid.UUID) -> Site:
    """Load one site with its billing-rate history, or raise `SiteNotFound`."""
    site = session.scalars(base_select().where(Site.id == site_id)).one_or_none()
    if site is None:
        raise SiteNotFound(site_id)
    return site


@dataclass(frozen=True, slots=True)
class SitePage:
    """A page of sites plus the unfiltered-by-paging total, for list rendering."""

    items: Sequence[Site]
    total: int


def list_sites(
    session: Session,
    *,
    status: SiteStatus | None = None,
    scope_statement: Select[tuple[Site]] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> SitePage:
    """A stable-sorted page of sites (Requirement 22.5).

    `scope_statement` lets the router hand in a query already narrowed to the caller's sites; when it
    is `None` the service lists across all sites, which is the administrator and accounting case.
    Sort is `(name, id)` so the order is total and does not shift between pages when two sites share a
    name.
    """
    statement = scope_statement if scope_statement is not None else base_select()
    if status is not None:
        statement = statement.where(Site.status == status)

    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    ordered = statement.order_by(Site.name, Site.id).limit(limit).offset(offset)
    items = list(session.scalars(ordered).unique())
    return SitePage(items=items, total=total)


def assigned_employee_ids(session: Session, site_id: uuid.UUID) -> list[uuid.UUID]:
    """The ids of employees assigned to a site, stably ordered (Requirement 7.1)."""
    rows = session.scalars(
        select(EmployeeSite.employee_id)
        .where(EmployeeSite.site_id == site_id)
        .order_by(EmployeeSite.employee_id)
    )
    return list(rows)


def assigned_site_ids(session: Session, employee_id: uuid.UUID) -> list[uuid.UUID]:
    """The ids of sites an employee is assigned to, stably ordered (Requirement 7.1)."""
    rows = session.scalars(
        select(EmployeeSite.site_id)
        .where(EmployeeSite.employee_id == employee_id)
        .order_by(EmployeeSite.site_id)
    )
    return list(rows)


# --------------------------------------------------------------------------- site-number uniqueness


def _find_site_number_holder(
    session: Session, site_number: str, *, exclude_id: uuid.UUID | None = None
) -> Site | None:
    """A site already holding this site number, if any (Requirement 6.3).

    Excludes `exclude_id` so an update that leaves the number unchanged does not collide with the row
    being updated.
    """
    statement = select(Site).where(Site.site_number == site_number)
    if exclude_id is not None:
        statement = statement.where(Site.id != exclude_id)
    return session.scalars(statement).first()


# --------------------------------------------------------------------------- create


#: Attribute names a create or update may write. The billing rates are not among them — they move
#: through the rates endpoint. `manager_user_id` is handled specially because it also maintains the
#: `user_sites` grant the authorization layer reads.
_WRITABLE_FIELDS = (
    "name",
    "site_number",
    "client_id",
    "address",
    "start_date",
    "status",
    "qr_mode",
    "assignment_mode",
    "notes",
)


def _placeholder_qr_token() -> str:
    """A unique value carried only until the row has an id to sign into its real token.

    The signed token embeds the site id (Requirement 8.4), which does not exist until the row is
    flushed, so a fresh `Site` is constructed with this to satisfy the column's `NOT NULL` and
    uniqueness, then `_mint_current_token` overwrites it once the id is assigned. Random so two sites
    created in the same request cannot collide on it before either is minted.
    """
    return f"pending-{uuid.uuid4().hex}"


def _mint_current_token(site: Site) -> None:
    """Set `site.qr_token` to a signed token for the site's current version (Requirement 8.4).

    Stored on the row so a scan can resolve a presented token to a site by an indexed lookup and then
    confirm the version, rather than trusting the payload alone. Requires `site.id`, so it runs after
    the row is flushed.
    """
    site.qr_token = qr_token.mint(site.id, site.qr_token_version)


def create_site(session: Session, payload: SiteCreate, *, context: AuditContext) -> Site:
    """Create a site, its opening billing rate if given, and an audit row per field (Requirement 6.1).

    Site-number uniqueness is checked before the insert so the conflicting site can be named; the
    database's unique constraint is the backstop for the race between the check and the flush. When a
    manager is named, the `user_sites` grant is written too, so the manager can see the site at once.
    """
    existing = _find_site_number_holder(session, payload.site_number)
    if existing is not None:
        raise DuplicateSiteNumber(existing)

    site = Site(
        name=payload.name,
        site_number=payload.site_number,
        client_id=payload.client_id,
        address=payload.address,
        manager_user_id=payload.manager_user_id,
        start_date=payload.start_date,
        status=payload.status,
        qr_mode=payload.qr_mode,
        assignment_mode=payload.assignment_mode,
        notes=payload.notes,
        qr_token=_placeholder_qr_token(),
    )
    session.add(site)
    # Flush so the row has an id the audit rows, rate rows, the manager grant — and the signed QR
    # token — can reference. The token embeds the id, so it can only be minted once one exists.
    session.flush()
    _mint_current_token(site)
    session.flush()

    empty = dict.fromkeys(snapshot(site), None)
    record_model_changes(session, site, empty, context=context, reason="site_created")

    if payload.manager_user_id is not None:
        _grant_manager_site(session, payload.manager_user_id, site.id)

    if payload.rate is not None:
        _write_rate_history(session, site, [payload.rate], context=context)

    return site


# --------------------------------------------------------------------------- update


def update_site(
    session: Session, site_id: uuid.UUID, payload: SiteUpdate, *, context: AuditContext
) -> Site:
    """Apply a partial update, checking site-number uniqueness if the number changes.

    Only the fields present in the request are touched (`exclude_unset`), and only the fields that
    actually moved produce an audit row. When the manager changes, the `user_sites` grant is moved
    with it (Requirement 2.3, 6.7): the old manager loses the site from their scope and the new one
    gains it.
    """
    site = get_site(session, site_id)
    changes = payload.model_dump(exclude_unset=True)
    before = snapshot(site)

    if "site_number" in changes:
        new_number = changes["site_number"]
        if new_number != site.site_number:
            conflict = _find_site_number_holder(session, new_number, exclude_id=site.id)
            if conflict is not None:
                raise DuplicateSiteNumber(conflict)

    manager_changes = "manager_user_id" in changes
    previous_manager = site.manager_user_id

    for field in _WRITABLE_FIELDS:
        if field in changes:
            setattr(site, field, changes[field])

    if manager_changes:
        new_manager = changes["manager_user_id"]
        if new_manager != previous_manager:
            site.manager_user_id = new_manager
            _move_manager_grant(session, previous_manager, new_manager, site.id)

    session.flush()
    record_model_changes(session, site, before, context=context)
    return site


# --------------------------------------------------------------------------- QR regeneration


def regenerate_qr(session: Session, site_id: uuid.UUID, *, context: AuditContext) -> Site:
    """Bump a site's QR version and mint a fresh token, revoking every printed code (Requirement 8.6).

    Incrementing `qr_token_version` is what does the revoking: a token printed at the old version now
    names a version the site has moved past, so `qr_token.verify` refuses it (Requirement 8.7) while
    a token minted here at the new version is accepted. The stored `qr_token` is replaced too, so the
    next download and any indexed lookup see the current code. The version bump is audited; the token
    string itself is not copied into the audit table, only the fact that it rotated and by whom,
    which is what a dispute about a code that "stopped working" needs.
    """
    site = get_site(session, site_id)
    before = snapshot(site)
    site.qr_token_version += 1
    _mint_current_token(site)
    session.flush()
    record_model_changes(
        session, site, before, context=context, fields=["qr_token_version"], reason="qr_regenerated"
    )
    return site


# --------------------------------------------------------------------------- manager grant (user_sites)


def _grant_manager_site(session: Session, user_id: uuid.UUID, site_id: uuid.UUID) -> None:
    """Write the `user_sites` grant that lets a manager see a site (Requirement 2.3, 6.7).

    Idempotent: a grant that already exists is left as it is, so re-assigning the same manager is
    harmless.
    """
    existing = session.get(UserSite, {"user_id": user_id, "site_id": site_id})
    if existing is None:
        session.add(UserSite(user_id=user_id, site_id=site_id))


def _revoke_manager_site(session: Session, user_id: uuid.UUID, site_id: uuid.UUID) -> None:
    """Remove a stale `user_sites` grant when a manager is unassigned from a site."""
    session.execute(
        delete(UserSite).where(UserSite.user_id == user_id, UserSite.site_id == site_id)
    )


def _move_manager_grant(
    session: Session,
    previous_manager: uuid.UUID | None,
    new_manager: uuid.UUID | None,
    site_id: uuid.UUID,
) -> None:
    """Move the site's manager grant from the old manager to the new one.

    Either may be `None` — a site can gain its first manager, or lose the one it had. The old grant
    is revoked before the new one is written so a manager who is both removed and re-added in the
    same edit ends with exactly one grant.
    """
    if previous_manager is not None:
        _revoke_manager_site(session, previous_manager, site_id)
    if new_manager is not None:
        _grant_manager_site(session, new_manager, site_id)


# --------------------------------------------------------------------------- rate history


def _validate_rate_chain(rates: Sequence[SiteRateInput]) -> list[SiteRateInput]:
    """Sort the submitted rows and reject any that overlap or are internally out of order.

    Overlap is checked on the inclusive `[from, to]` periods, matching both `SiteRate.covers` and the
    database exclusion constraint: a row ending 15 August and a row starting 16 August are fine, but
    two rows both covering 16 August are not. An open-ended row (`effective_to is None`) may only be
    the last in the chain, because anything after it would fall inside its still-in-force period. This
    is the same reconciliation as `app.services.employee._validate_rate_chain`.
    """
    ordered = sorted(rates, key=lambda rate: rate.effective_from)
    for rate in ordered:
        if rate.effective_to is not None and rate.effective_to < rate.effective_from:
            raise InvalidRatePeriod
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if earlier.effective_to is None or earlier.effective_to >= later.effective_from:
            raise OverlappingRates
    return ordered


def replace_rate_history(
    session: Session,
    site_id: uuid.UUID,
    rates: Sequence[SiteRateInput],
    *,
    context: AuditContext,
) -> Site:
    """Replace a site's billing-rate history with the submitted, reconciled chain (Requirement 6.4).

    The whole history is replaced rather than appended to, because a client that owns the rates
    screen is stating the intended history. The overlap check runs first, so a rejected submission
    leaves the existing history untouched — which is half of why an already-billed period is safe
    (6.6); the other half is that a past row is never edited in place, only kept or dropped.
    """
    site = get_site(session, site_id)
    _write_rate_history(session, site, rates, context=context)
    return site


def _write_rate_history(
    session: Session,
    site: Site,
    rates: Sequence[SiteRateInput],
    *,
    context: AuditContext,
) -> None:
    ordered = _validate_rate_chain(rates)

    for existing in list(site.rates):
        session.delete(existing)
    site.rates.clear()

    for rate in ordered:
        site.rates.append(
            SiteRate(
                billing_rate=rate.billing_rate,
                overtime_billing_rate=rate.overtime_billing_rate,
                effective_from=rate.effective_from,
                effective_to=rate.effective_to,
            )
        )
    session.flush()

    # One audit row on the site marking that billing changed. The rate values themselves are billing
    # data restricted to finance; the audit records that the history was rewritten and by whom, which
    # is what a dispute needs, without copying billing figures into a table that is never deleted.
    record_change(
        session,
        entity_type="sites",
        entity_id=site.id,
        field="rates",
        old_value=None,
        new_value=f"{len(ordered)} rate period(s)",
        context=context,
        reason="rates_updated",
    )


def resolve_rate(site: Site, on_date: date) -> SiteRate | None:
    """The billing rate in force on `on_date`, or `None` if no row covers it (Requirement 17.1).

    Pure: it reads the already-loaded history and picks the one row whose inclusive period covers the
    date. Because the chain is non-overlapping, at most one row can match; the latest-starting match
    is returned defensively so a hand-inserted overlap resolves deterministically. This is the
    function billing calls per work date to split a mid-month rate change, and the reason a past
    period keeps its own rate when a newer one is added.
    """
    covering = [rate for rate in site.rates if rate.covers(on_date)]
    if not covering:
        return None
    return max(covering, key=lambda rate: rate.effective_from)


# --------------------------------------------------------------------------- assignment


def _require_employee(session: Session, employee_id: uuid.UUID) -> Employee:
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise EmployeeNotFound(employee_id)
    return employee


def replace_site_employees(
    session: Session,
    site_id: uuid.UUID,
    employee_ids: Sequence[uuid.UUID],
    *,
    context: AuditContext,
) -> list[uuid.UUID]:
    """Replace the set of employees assigned to a site (Requirement 7.1).

    The whole membership is replaced, not appended to. Every named employee must exist, checked
    before anything is written, so a bad id leaves the existing assignment untouched. An assignment
    is an expectation only — it never restricts where an employee may record time (Requirement 7.2).
    """
    site = get_site(session, site_id)
    unique_ids = _deduplicate(employee_ids)
    for employee_id in unique_ids:
        _require_employee(session, employee_id)

    existing = set(assigned_employee_ids(session, site.id))
    desired = set(unique_ids)
    if existing == desired:
        return sorted_ids(desired)

    session.execute(delete(EmployeeSite).where(EmployeeSite.site_id == site.id))
    today = datetime.now(UTC).date()
    for employee_id in unique_ids:
        session.add(EmployeeSite(employee_id=employee_id, site_id=site.id, assigned_from=today))
    session.flush()

    record_change(
        session,
        entity_type="sites",
        entity_id=site.id,
        field="assigned_employees",
        old_value=str(len(existing)),
        new_value=str(len(desired)),
        context=context,
        reason="site_employees_updated",
    )
    return sorted_ids(desired)


def replace_employee_sites(
    session: Session,
    employee_id: uuid.UUID,
    site_ids: Sequence[uuid.UUID],
    *,
    context: AuditContext,
) -> list[uuid.UUID]:
    """Replace the set of sites an employee is assigned to (Requirement 7.1).

    The mirror of `replace_site_employees`, maintaining the same `employee_sites` table from the
    employee side. Every named site must exist; a bad id leaves the existing assignment untouched.
    """
    _require_employee(session, employee_id)
    unique_ids = _deduplicate(site_ids)
    for site_id in unique_ids:
        get_site(session, site_id)

    existing = set(assigned_site_ids(session, employee_id))
    desired = set(unique_ids)
    if existing == desired:
        return sorted_ids(desired)

    session.execute(delete(EmployeeSite).where(EmployeeSite.employee_id == employee_id))
    today = datetime.now(UTC).date()
    for site_id in unique_ids:
        session.add(EmployeeSite(employee_id=employee_id, site_id=site_id, assigned_from=today))
    session.flush()

    record_change(
        session,
        entity_type="employees",
        entity_id=employee_id,
        field="assigned_sites",
        old_value=str(len(existing)),
        new_value=str(len(desired)),
        context=context,
        reason="employee_sites_updated",
    )
    return sorted_ids(desired)


def _deduplicate(ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """Preserve order while dropping repeats, so a set with a duplicate id writes one row."""
    seen: set[uuid.UUID] = set()
    unique: list[uuid.UUID] = []
    for value in ids:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def sorted_ids(ids: set[uuid.UUID]) -> list[uuid.UUID]:
    """A stable order for the ids returned to a client, so a response is deterministic."""
    return sorted(ids, key=str)
