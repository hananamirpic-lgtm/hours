"""Client records (Requirement 5).

The service owns every business rule; the router only translates. Nothing here raises an HTTP error
or commits — the caller's unit of work decides the fate of the change and its audit rows together,
which is the invariant `app.services.audit` rests on. This mirrors `app.services.employee` exactly, so
the two read the same way.

Three rules carry the weight.

**Company number is unique when present, checked in code (Requirement 5.2).** The database has a
unique constraint that says the same, but NULLs do not collide there, so several clients may have no
company number and that is fine. The application check runs only when a number is supplied, excludes
the row being updated, and names the conflicting client — which a bare constraint violation could
never do.

**Deletion is refused when the client's sites carry time entries; archival is offered instead
(Requirement 5.4).** A client whose sites hold recorded work is part of the billing history, and a
hard delete would orphan or destroy it. So `delete_client` first asks whether any time entry exists at
any of the client's sites: if one does, it refuses and the caller archives instead; if none does, the
client and its (empty) sites can be removed cleanly. Archival is a status flip, not a delete, so the
row and everything under it stays.

**Email format and payment terms are validated at the schema, not here.** Requirement 5.5 lives in
`app.schemas.client`, so a malformed address never reaches this layer.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, exists, func, select
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.site import Site
from app.models.time_entry import TimeEntry
from app.schemas.client import ClientCreate, ClientUpdate
from app.services.audit import AuditContext, record_model_changes, snapshot

# --------------------------------------------------------------------------- errors
# Domain errors, not HTTP errors, each carrying a machine `code` the router lifts into the error
# envelope — the same shape `app.services.employee` uses.


class ClientError(Exception):
    """Base for every client-service failure. `code` is what the front end translates."""

    code = "client_error"


class ClientNotFound(ClientError):
    code = "client_not_found"

    def __init__(self, client_id: uuid.UUID) -> None:
        super().__init__(f"no client {client_id}")
        self.client_id = client_id


class DuplicateCompanyNumber(ClientError):
    """Another client already holds this company number (Requirement 5.2).

    Carries the conflicting client so the router can name it, which a bare uniqueness violation could
    never supply.
    """

    code = "duplicate_company_number"

    def __init__(self, conflicting: Client) -> None:
        super().__init__(f"company number already held by client {conflicting.id}")
        self.conflicting = conflicting


class ClientHasTimeEntries(ClientError):
    """The client's sites carry time entries, so it may not be deleted (Requirement 5.4).

    The router turns this into a conflict that offers archival, which is the recorded alternative.
    """

    code = "client_has_time_entries"

    def __init__(self, client_id: uuid.UUID) -> None:
        super().__init__(f"client {client_id} has sites with time entries; archive instead")
        self.client_id = client_id


# --------------------------------------------------------------------------- reads


def get_client(session: Session, client_id: uuid.UUID) -> Client:
    """Load one client, or raise `ClientNotFound`."""
    client = session.get(Client, client_id)
    if client is None:
        raise ClientNotFound(client_id)
    return client


@dataclass(frozen=True, slots=True)
class ClientPage:
    """A page of clients plus the unfiltered-by-paging total, for list rendering."""

    items: Sequence[Client]
    total: int


def list_clients(
    session: Session,
    *,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> ClientPage:
    """A stable-sorted page of clients (Requirement 22.5).

    Archived clients are excluded by default, because the point of archival is to take a client out of
    the working list without destroying it; a caller that wants them passes `include_archived`. Sort
    is `(name, id)` so the order is total and does not shift between pages when two clients share a
    name.
    """
    statement: Select[tuple[Client]] = select(Client)
    if not include_archived:
        statement = statement.where(Client.is_archived.is_(False))

    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    ordered = statement.order_by(Client.name, Client.id).limit(limit).offset(offset)
    items = list(session.scalars(ordered))
    return ClientPage(items=items, total=total)


def list_sites(session: Session, client_id: uuid.UUID) -> list[Site]:
    """The sites a client owns, oldest-name first (Requirement 5.3).

    Raises `ClientNotFound` if the client does not exist, so the endpoint answers 404 for an unknown
    id rather than an empty list that a caller cannot tell from a client with no sites.
    """
    get_client(session, client_id)
    ordered = (
        select(Site).where(Site.client_id == client_id).order_by(Site.name, Site.id)
    )
    return list(session.scalars(ordered))


# --------------------------------------------------------------------------- company-number uniqueness


def _find_company_number_holder(
    session: Session, company_number: str, *, exclude_id: uuid.UUID | None = None
) -> Client | None:
    """A client already holding this company number, if any (Requirement 5.2).

    Excludes `exclude_id` so an update that leaves the number unchanged does not collide with the row
    being updated.
    """
    statement = select(Client).where(Client.company_number == company_number)
    if exclude_id is not None:
        statement = statement.where(Client.id != exclude_id)
    return session.scalars(statement).first()


# --------------------------------------------------------------------------- create


#: Attribute names a create or update may write. `is_archived` is not among them — it moves through
#: `archive_client`, which owns the delete-versus-archive rule.
_WRITABLE_FIELDS = (
    "name",
    "company",
    "company_number",
    "contact_person",
    "phone",
    "email",
    "address",
    "payment_terms_days",
    "payment_terms_notes",
    "notes",
)


def create_client(session: Session, payload: ClientCreate, *, context: AuditContext) -> Client:
    """Create a client and an audit row per field set (Requirement 5.1).

    Company-number uniqueness is checked before the insert, but only when a number is supplied, so the
    conflicting client can be named; the database's unique constraint is the backstop for the race
    between the check and the flush.
    """
    if payload.company_number is not None:
        existing = _find_company_number_holder(session, payload.company_number)
        if existing is not None:
            raise DuplicateCompanyNumber(existing)

    client = Client(
        name=payload.name,
        company=payload.company,
        company_number=payload.company_number,
        contact_person=payload.contact_person,
        phone=payload.phone,
        email=payload.email,
        address=payload.address,
        payment_terms_days=payload.payment_terms_days,
        payment_terms_notes=payload.payment_terms_notes,
        notes=payload.notes,
    )
    session.add(client)
    # Flush so the row has an id the audit rows can reference.
    session.flush()

    empty = dict.fromkeys(snapshot(client), None)
    record_model_changes(session, client, empty, context=context, reason="client_created")
    return client


# --------------------------------------------------------------------------- update


def update_client(
    session: Session, client_id: uuid.UUID, payload: ClientUpdate, *, context: AuditContext
) -> Client:
    """Apply a partial update, checking company-number uniqueness if the number changes.

    Only the fields present in the request are touched (`exclude_unset`), and only the fields that
    actually moved produce an audit row.
    """
    client = get_client(session, client_id)
    changes = payload.model_dump(exclude_unset=True)
    before = snapshot(client)

    if "company_number" in changes:
        new_number = changes["company_number"]
        if new_number is not None and new_number != client.company_number:
            conflict = _find_company_number_holder(session, new_number, exclude_id=client.id)
            if conflict is not None:
                raise DuplicateCompanyNumber(conflict)

    for field in _WRITABLE_FIELDS:
        if field in changes:
            setattr(client, field, changes[field])

    session.flush()
    record_model_changes(session, client, before, context=context)
    return client


# --------------------------------------------------------------------------- delete / archive


def _has_time_entries(session: Session, client_id: uuid.UUID) -> bool:
    """Whether any time entry exists at any of the client's sites (Requirement 5.4).

    An `EXISTS` over the join rather than a count: the question is only "any?", and the query can stop
    at the first row instead of tallying every entry a busy client has ever recorded.
    """
    condition = exists(
        select(TimeEntry.id)
        .join(Site, Site.id == TimeEntry.site_id)
        .where(Site.client_id == client_id)
    )
    return bool(session.scalar(select(condition)))


def delete_client(session: Session, client_id: uuid.UUID, *, context: AuditContext) -> None:
    """Hard-delete a client, but only if none of its sites carry a time entry (Requirement 5.4).

    When any entry exists the deletion is refused with `ClientHasTimeEntries`, and the caller archives
    instead — the history is part of billing and must not be destroyed. When none exists the client is
    safe to remove; an audit row records the removal so even a clean delete is traceable.
    """
    client = get_client(session, client_id)
    if _has_time_entries(session, client_id):
        raise ClientHasTimeEntries(client_id)

    before = snapshot(client)
    empty = dict.fromkeys(before, None)
    # One audit row per field, recording the values as they were before the row disappears.
    record_model_changes(session, client, empty, context=context, reason="client_deleted")

    session.delete(client)
    session.flush()


def archive_client(session: Session, client_id: uuid.UUID, *, context: AuditContext) -> Client:
    """Archive a client instead of deleting it (Requirement 5.4).

    A status flip, not a delete: the row and every site and time entry under it stay, and the client
    simply drops out of the default list. Archiving an already-archived client is a no-op that writes
    no audit row, so a resubmission is harmless.
    """
    client = get_client(session, client_id)
    if client.is_archived:
        return client

    before = snapshot(client, fields=["is_archived"])
    client.is_archived = True
    session.flush()
    record_model_changes(
        session, client, before, context=context, reason="client_archived", fields=["is_archived"]
    )
    return client
