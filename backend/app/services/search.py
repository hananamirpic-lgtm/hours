"""Global search across employees, sites and clients (Requirement 22.1, 22.2, 22.4, 22.5, 21.5).

The service owns the matching rules; the router only guards, scopes and translates. Nothing here
raises an HTTP error or commits — a search is a pure read, so there is no unit of work to close.

Four things carry the weight, and each is a consequence of a decision made earlier in the system.

**Names match in either language, case- and accent-insensitively (Requirement 22.4, 21.5).** An
employee carries a local-language `full_name` and an English `full_name_en`; a site and a client
carry one `name`. A term matches a record when its folded form is a substring of the folded form of
any of those. Folding is `casefold()` plus Unicode NFKD accent stripping. The comparison is done in
Python, not in SQL: the stored columns are not normalised, and neither SQLite (the unit-test engine)
nor a default PostgreSQL install can fold accents in a portable `WHERE` — SQLite's `LIKE` is
accent-sensitive, so a `LIKE '%jose%'` would miss a row storing `José`. So the database does what it
can do portably and cheaply — narrow by the caller's site scope, and resolve the exact passport-hash
lookup — and the accent-insensitive name, site-number and phone matching runs in Python over the
scoped rows. The scoped set is what a search realistically ranges over: a site manager's is bounded to
their sites, and the whole-business tables a search covers are in the hundreds to low thousands, the
same order the reports service already iterates.

**Passport matches by the deterministic hash, never by the ciphertext (Requirement 22.1).** The
passport number is encrypted at rest with a fresh nonce per write, so the ciphertext cannot be
searched; the `passport_number_hash` companion is what the uniqueness index and this lookup both key
on. Passport search is therefore *exact* — a hash is all-or-nothing — which matches how an operator
uses a passport number: they hold the whole value, not a fragment. The same
`get_encryptor().deterministic_hash` the employee service uses is used here, so a value hashes the
same way it was stored, and the match is resolved in SQL rather than in Python — the loaded attribute
is the digest, but comparing against a freshly hashed term in the query is exact and needs no
Python second pass.

**Phone matches only where it is plaintext.** A client's phone is an ordinary column and matches as a
partial string. An employee's phone is encrypted with no hash beside it, so it can be matched neither
partially nor exactly without decrypting every row — which search does not do. Employee phone is
therefore not a search key; the requirement's "phone" is served by the client phone, and an operator
looking for an employee uses their name or passport. This is the cost of the encryption decision,
documented rather than worked around by decrypting the whole table.

**Every group is scoped to the caller and paginated (Requirement 22.2, 22.5).** The site scope is
folded into each query in SQL, not after the fetch, so a site manager never ranges over rows they may
not see. Employees are scoped to those expected at, or who have recorded time at, one of the caller's
sites; sites to the caller's site ids; clients to those owning one of the caller's sites. An
administrator and accounting are unrestricted. Each group carries its own `total` so the front end can
page each independently, and each is ordered `(name, id)` so the order is total and stable.
"""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app.core.authz import SiteScope
from app.core.crypto import get_encryptor
from app.models.client import Client
from app.models.employee import Employee
from app.models.site import EmployeeSite, Site
from app.models.time_entry import TimeEntry

# --------------------------------------------------------------------------- folding


def fold(value: str) -> str:
    """The comparison form of a string: lower-cased and stripped of combining accents.

    `casefold` rather than `lower` so case-insensitivity holds across scripts, not only ASCII. NFKD
    decomposition splits an accented letter into its base plus a combining mark, and the marks
    (Unicode category `Mn`) are then dropped, so `José` folds to `jose` and matches a search for
    `jose`. Hebrew has no accents to strip, so a Hebrew name folds to itself and matches unchanged
    (Requirement 21.5). Surrounding whitespace is trimmed so a stray leading space does not defeat a
    match.
    """
    decomposed = unicodedata.normalize("NFKD", value.strip().casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _contains(folded_term: str, *values: str | None) -> bool:
    """Whether the folded term is a substring of any of the folded candidate values."""
    return any(value is not None and folded_term in fold(value) for value in values)


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class EmployeeHit:
    """One employee matched by the search. Carries both name forms so the UI labels it in either
    language; no wage or sensitive field, so the row is safe for any personnel reader."""

    id: uuid.UUID
    full_name: str
    full_name_en: str
    status: str


@dataclass(frozen=True, slots=True)
class SiteHit:
    id: uuid.UUID
    name: str
    site_number: str
    client_id: uuid.UUID
    status: str


@dataclass(frozen=True, slots=True)
class ClientHit:
    id: uuid.UUID
    name: str
    company: str | None


@dataclass(frozen=True, slots=True)
class Group:
    """A page of hits of one kind, plus the total that matched before paging (Requirement 22.5)."""

    items: Sequence[object]
    total: int


@dataclass(frozen=True, slots=True)
class SearchResults:
    """The three groups a global search returns (Requirement 22.1)."""

    employees: Group
    sites: Group
    clients: Group


#: A blank term (or an empty scope) matches nothing rather than everything: an empty box is not a
#: request for the whole database, and a manager with no assignment sees nothing, not everything.
_EMPTY = SearchResults(
    employees=Group(items=(), total=0),
    sites=Group(items=(), total=0),
    clients=Group(items=(), total=0),
)


def _page(matched: list, *, limit: int, offset: int, build) -> Group:
    """Total, slice and project a matched list into a page of hits."""
    total = len(matched)
    return Group(items=[build(row) for row in matched[offset : offset + limit]], total=total)


# --------------------------------------------------------------------------- entry point


def search(
    session: Session,
    *,
    term: str,
    scope: SiteScope,
    limit: int = 10,
    offset: int = 0,
) -> SearchResults:
    """Search employees, sites and clients for `term`, within `scope` (Requirement 22.1–22.5).

    A blank term, or an empty scope, returns empty groups. Otherwise each group is matched, scoped to
    the caller, ordered stably and paged. The three groups share the same `limit`/`offset`, so the
    front end renders one page per group; the per-group totals let it page each on its own.
    """
    folded = fold(term)
    if not folded or scope.is_empty:
        return _EMPTY

    return SearchResults(
        employees=_search_employees(
            session, folded=folded, term=term, scope=scope, limit=limit, offset=offset
        ),
        sites=_search_sites(session, folded=folded, scope=scope, limit=limit, offset=offset),
        clients=_search_clients(session, folded=folded, scope=scope, limit=limit, offset=offset),
    )


# --------------------------------------------------------------------------- employees (22.1, 22.4)


def _scoped_employee_select(scope: SiteScope) -> Select[tuple[Employee]]:
    """The employee query, narrowed to the employees the caller may see (Requirement 22.2, 2.3, 2.4).

    Unrestricted for an administrator and accounting. For a site manager, an employee is in scope when
    they are expected at one of the manager's sites (`employee_sites`) or have recorded time there
    (`time_entries`) — the set a manager sees on their assignment and hours screens. The two are
    combined with `EXISTS` subqueries so the employee row is not duplicated by a manager who has
    several matching sites. Ordered `(full_name, id)` so paging is stable (Requirement 22.5).
    """
    statement = select(Employee).order_by(Employee.full_name, Employee.id)
    if scope.unrestricted:
        return statement

    assigned = (
        select(EmployeeSite.employee_id)
        .where(EmployeeSite.employee_id == Employee.id)
        .where(EmployeeSite.site_id.in_(scope.site_ids))
    )
    worked = (
        select(TimeEntry.employee_id)
        .where(TimeEntry.employee_id == Employee.id)
        .where(TimeEntry.site_id.in_(scope.site_ids))
    )
    return statement.where(or_(assigned.exists(), worked.exists()))


def _search_employees(
    session: Session,
    *,
    folded: str,
    term: str,
    scope: SiteScope,
    limit: int,
    offset: int,
) -> Group:
    """Employees matching by name (either language, case/accent-insensitive) or passport (exact hash).

    The passport hit is resolved in SQL — an exact match on the deterministic hash — and its ids are
    the definite matches that must be kept regardless of name. The name match runs in Python over the
    scoped, stably ordered rows, so accent-insensitivity holds on a database that cannot fold in the
    `WHERE`. The union preserves the SQL order, and a passport-only match keeps its place in that
    order rather than jumping to the front.
    """
    scoped = _scoped_employee_select(scope)

    passport_hash = get_encryptor().deterministic_hash(term)
    passport_ids = set(
        session.scalars(scoped.with_only_columns(Employee.id).where(
            Employee.passport_number_hash == passport_hash
        ))
    )

    rows = session.scalars(scoped).unique().all()
    matched = [
        employee
        for employee in rows
        if employee.id in passport_ids or _contains(folded, employee.full_name, employee.full_name_en)
    ]

    return _page(
        matched,
        limit=limit,
        offset=offset,
        build=lambda employee: EmployeeHit(
            id=employee.id,
            full_name=employee.full_name,
            full_name_en=employee.full_name_en,
            status=employee.status.value,
        ),
    )


# --------------------------------------------------------------------------- sites (22.1, 22.4)


def _search_sites(
    session: Session,
    *,
    folded: str,
    scope: SiteScope,
    limit: int,
    offset: int,
) -> Group:
    """Sites matching by name (case/accent-insensitive) or site number (partial) (Requirement 22.1).

    Scope is folded into the query in SQL: an administrator and accounting see every site, a site
    manager only their assigned ids. Name and site-number matching run in Python so both are
    accent-insensitive and the number is a partial match. Ordered `(name, id)` for stable paging.
    """
    statement = select(Site).order_by(Site.name, Site.id)
    if not scope.unrestricted:
        statement = statement.where(Site.id.in_(scope.site_ids))

    rows = session.scalars(statement).unique().all()
    matched = [site for site in rows if _contains(folded, site.name, site.site_number)]

    return _page(
        matched,
        limit=limit,
        offset=offset,
        build=lambda site: SiteHit(
            id=site.id,
            name=site.name,
            site_number=site.site_number,
            client_id=site.client_id,
            status=site.status.value,
        ),
    )


# --------------------------------------------------------------------------- clients (22.1, 22.4)


def _search_clients(
    session: Session,
    *,
    folded: str,
    scope: SiteScope,
    limit: int,
    offset: int,
) -> Group:
    """Clients matching by name (case/accent-insensitive) or phone (partial) (Requirement 22.1).

    A client's phone is a plaintext column, so it is a partial search key (unlike an employee's
    encrypted phone). Scope: an administrator and accounting see every client; a site manager sees
    only clients that own one of their sites, folded into the query with an `EXISTS` over `sites` so a
    client with several matching sites is not duplicated. Ordered `(name, id)` for stable paging.
    """
    statement = select(Client).order_by(Client.name, Client.id)
    if not scope.unrestricted:
        owns_scoped_site = (
            select(Site.id)
            .where(Site.client_id == Client.id)
            .where(Site.id.in_(scope.site_ids))
        )
        statement = statement.where(owns_scoped_site.exists())

    rows = session.scalars(statement).unique().all()
    matched = [client for client in rows if _contains(folded, client.name, client.phone)]

    return _page(
        matched,
        limit=limit,
        offset=offset,
        build=lambda client: ClientHit(id=client.id, name=client.name, company=client.company),
    )
