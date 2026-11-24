"""The assigned technician name and the treatment history of a ticket (Requirement 16.2 to 16.11).

Staff set the name of the assigned technician and keep a working log of notes (`HistoryEntry`) on a
ticket. Both work on a ticket in every status (R16.10): nothing here reads `Ticket.status`.

Rules, in one place:

* The assigned name is trimmed. Empty after trimming is stored as the empty string, the one
  "unassigned" state (R16.2). Longer than 100 characters after trimming is
  `invalid_assigned_technician_name` and nothing is stored (R16.7). A change sets `Ticket.updated_at`
  from the server clock; a history entry never does, because entries carry their own timestamps.
* An entry has a text of 1 to 2000 characters and a technician name of 1 to 100, both measured after
  trimming and stored trimmed (R16.3). A body with two bad fields reports both, one code per field
  (R16.6): `invalid_history_text` and `invalid_technician_name`. `HistoryValidationError.fields` maps
  each failing field to its code, in the order text, name, as `CustomerValidationError` does.
* `add_entry` stamps `created_at` from the server clock and leaves `edited_at` null.
  `edit_entry` is one unconditional `UPDATE`: it replaces both values, sets `edited_at`, keeps
  `created_at`, and keeps no copy of the previous values. With two edits at once the one the database
  applies last wins (R16.5); there is no version check on purpose.
* Order of checks: validation (422), then the ticket (404 `ticket_not_found`), then the entry
  (404 `history_entry_not_found`, which an entry of another ticket also gets, R16.8). A rejected call
  changes nothing.
* No audit rows are written (R16.11).

The module never commits and never raises HTTP errors; each error class carries the API `code`, and the
router maps it to a status and owns the transaction. The ticket and name errors are the lifecycle's own
classes, so the router has one mapping for them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.service_tickets.error_codes import (
    CODE_HISTORY_ENTRY_NOT_FOUND,
    CODE_INVALID_HISTORY_TEXT,
    CODE_INVALID_TECHNICIAN_NAME,
)
from app.service_tickets.models import HistoryEntry, Ticket
from app.service_tickets.services.lifecycle import (
    TECHNICIAN_NAME_MAX,
    InvalidAssignedTechnicianName,
    LifecycleError,
    TicketNotFound,
)

#: Longest History_Entry text (R16.3); the `svc_history_entries` check constraint has the same bound.
ENTRY_TEXT_MAX = 2000

__all__ = [
    "ENTRY_TEXT_MAX",
    "HistoryEntryNotFound",
    "HistoryValidationError",
    "InvalidAssignedTechnicianName",
    "TicketNotFound",
    "add_entry",
    "edit_entry",
    "list_entries",
    "set_assigned_technician_name",
    "validate_entry",
]


class HistoryEntryNotFound(LifecycleError):
    """No such entry on this ticket: it does not exist or belongs to another ticket (HTTP 404)."""

    code = CODE_HISTORY_ENTRY_NOT_FOUND


class HistoryValidationError(Exception):
    """The text, the technician name or both are invalid (HTTP 422 at the router).

    `fields` maps each failing field (`entry_text`, `technician_name`) to its error code, text first.
    `code` is the code of the first failing field, so a single failure reads as "422 and the code X".
    """

    def __init__(self, fields: dict[str, str]) -> None:
        self.fields = dict(fields)
        self.code = next(iter(self.fields.values()))
        super().__init__(self.code)


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def validate_entry(entry_text: object, technician_name: object) -> tuple[str, str]:
    """The trimmed text and name, or `HistoryValidationError` naming every failing field.

    Public because the resolution of a ticket (R29) reuses the same validators and codes.
    """
    fields: dict[str, str] = {}
    text = entry_text.strip() if isinstance(entry_text, str) else ""
    name = technician_name.strip() if isinstance(technician_name, str) else ""
    if not 1 <= len(text) <= ENTRY_TEXT_MAX:
        fields["entry_text"] = CODE_INVALID_HISTORY_TEXT
    if not 1 <= len(name) <= TECHNICIAN_NAME_MAX:
        fields["technician_name"] = CODE_INVALID_TECHNICIAN_NAME
    if fields:
        raise HistoryValidationError(fields)
    return text, name


def _ticket_id(session: Session, ticket_number: int) -> uuid.UUID:
    ticket_id = session.execute(
        select(Ticket.id).where(Ticket.ticket_number == ticket_number)
    ).scalar_one_or_none()
    if ticket_id is None:
        raise TicketNotFound
    return ticket_id


def set_assigned_technician_name(
    session: Session, ticket_number: int, name: object, *, now: datetime | None = None
) -> Ticket:
    """Store the trimmed assigned technician name and return the ticket as stored. Never commits.

    Empty or whitespace-only is stored as the empty string. Over 100 characters after trimming raises
    `InvalidAssignedTechnicianName`; a missing ticket raises `TicketNotFound`. Works in every status.
    `now` replaces the clock for `updated_at`, for tests.
    """
    if not isinstance(name, str):
        raise InvalidAssignedTechnicianName
    trimmed = name.strip()
    if len(trimmed) > TECHNICIAN_NAME_MAX:
        raise InvalidAssignedTechnicianName

    result = session.execute(
        update(Ticket)
        .where(Ticket.ticket_number == ticket_number)
        .values(assigned_technician_name=trimmed, updated_at=_now(now))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise TicketNotFound
    return session.execute(
        select(Ticket).where(Ticket.ticket_number == ticket_number).execution_options(populate_existing=True)
    ).scalar_one()


def list_entries(session: Session, ticket_number: int) -> list[HistoryEntry]:
    """The entries of a ticket, oldest creation time first (R16.9). `TicketNotFound` for a missing ticket."""
    ticket_id = _ticket_id(session, ticket_number)
    return list(
        session.execute(
            select(HistoryEntry)
            .where(HistoryEntry.ticket_id == ticket_id)
            .order_by(HistoryEntry.created_at, HistoryEntry.id)
            .execution_options(populate_existing=True)
        ).scalars()
    )


def add_entry(
    session: Session,
    ticket_number: int,
    entry_text: object,
    technician_name: object,
    *,
    now: datetime | None = None,
) -> HistoryEntry:
    """Add one entry to a ticket and return it. Never commits.

    Text and name are trimmed and checked (`HistoryValidationError`); then the ticket must exist
    (`TicketNotFound`). `created_at` comes from the server clock (`now` replaces it for tests) and
    `edited_at` is null. The ticket's `updated_at` is not touched.
    """
    text, name = validate_entry(entry_text, technician_name)
    ticket_id = _ticket_id(session, ticket_number)
    entry = HistoryEntry(
        ticket_id=ticket_id, entry_text=text, technician_name=name, created_at=_now(now), edited_at=None
    )
    session.add(entry)
    session.flush()
    return entry


def edit_entry(
    session: Session,
    ticket_number: int,
    entry_id: uuid.UUID,
    entry_text: object,
    technician_name: object,
    *,
    now: datetime | None = None,
) -> HistoryEntry:
    """Replace the text and name of an entry and return it as stored. Never commits.

    One unconditional `UPDATE`: sets `edited_at`, keeps `created_at`, keeps no copy of the old values,
    and the last save wins. `TicketNotFound` for a missing ticket, `HistoryEntryNotFound` for an entry
    that does not exist or belongs to another ticket. Neither changes anything.
    """
    text, name = validate_entry(entry_text, technician_name)
    ticket_id = _ticket_id(session, ticket_number)
    result = session.execute(
        update(HistoryEntry)
        .where(HistoryEntry.id == entry_id, HistoryEntry.ticket_id == ticket_id)
        .values(entry_text=text, technician_name=name, edited_at=_now(now))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise HistoryEntryNotFound
    return session.execute(
        select(HistoryEntry).where(HistoryEntry.id == entry_id).execution_options(populate_existing=True)
    ).scalar_one()
