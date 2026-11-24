"""The ticket status lifecycle: one function for customer cancel and staff status change.

Requirements 13.1, 13.4, 13.7, 13.10 and 14.1 to 14.12; design.md, "Optimistic concurrency for status
changes and cancellation" and "Ticket status lifecycle".

The transition table (`TRANSITIONS`) is the whole contract:

    received    -> in_progress   staff (the approval step)
    in_progress -> closed        staff
    closed      -> in_progress   staff (reopen)
    received    -> cancelled     the owning customer
    in_progress -> cancelled     the owning customer

Every other pair is `invalid_transition`, `cancelled` has no outgoing edge, and staff may never ask for
`cancelled`. A target equal to the stored status is not in the table either.

`change_status` runs these steps in this order:

1. Input shape. A missing or unknown expected status, an unknown target, or an over-long technician name
   raises the 422 errors of R13.10 and R14.11 before anything is read.
2. Read the ticket. No row, or (for a customer) a row owned by someone else, is `TicketNotFound`; the two
   are the same outcome on purpose (R13.6).
3. `stored != expected` is `TicketStatusChanged`, before any transition check (R14.8).
4. A pair that the actor may not perform is `InvalidTransition` (R14.3, R14.4).
5. One guarded `UPDATE ... WHERE ticket_number = :n AND status = :expected`. When it matches no row, a
   concurrent writer got in between steps 2 and 5, and the answer is `TicketStatusChanged` again. On
   PostgreSQL the first writer takes the row lock, the second waits, re-evaluates the guard after the
   first commits and matches nothing, so exactly one of two racing requests is applied (R13.8).

A request rejected at any step never reaches the `UPDATE`, so the status, the technician name and
`updated_at` stay as they were (R14.10). A change adds no history entry (R14.10) and touches no history
row, so reopening keeps the name and the history (R14.7). The one exception is the resolution (R29):

* Between steps 4 and 5 the target decides. For `closed` a resolution is required
  (`ResolutionRequired`, 422 `resolution_required`) and is checked by `services.resolution.validate`
  (history text, technician name, parts; `ResolutionValidationError`). For any other target a resolution
  must be absent (`UnexpectedResolution`, 422 `unknown_field`). Both are refused before the `UPDATE`.
* After the guarded `UPDATE` matched its row, `services.resolution.store` adds the history entry and
  replaces the ticket's parts. A lost race (`TicketStatusChanged`) therefore leaves no entry and no part.
* The router owns the commit, so the status, the entry and the parts are stored together or not at all.
  Reopening (`closed` to `in_progress`) takes no resolution and leaves the recorded parts.

The module never commits and never raises HTTP errors. Each error class carries the API `code`; the
router maps it to a status (404, 409 or 422) and owns the commit and the rollback. A customer who asks for
a status other than `cancelled` (R14.5, HTTP 403) is the customer router's concern: that router has no
route that takes a target. Called directly, such a request is simply "not in the table for the actor" and
raises `InvalidTransition`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import Text, func, literal, select, update
from sqlalchemy.orm import Session

from app.service_tickets.error_codes import (
    CODE_INVALID_ASSIGNED_TECHNICIAN_NAME,
    CODE_INVALID_EXPECTED_STATUS,
    CODE_INVALID_TARGET_STATUS,
    CODE_INVALID_TRANSITION,
    CODE_RESOLUTION_REQUIRED,
    CODE_TICKET_NOT_FOUND,
    CODE_TICKET_STATUS_CHANGED,
    CODE_UNKNOWN_FIELD,
)
from app.service_tickets.grammars import STATUS_VALUES
from app.service_tickets.models import Ticket
from app.service_tickets.services.staff_identity import StaffPrincipal

if TYPE_CHECKING:  # `resolution` imports `history`, which imports this module: see `change_status`
    from app.service_tickets.services.resolution import Resolution

Performer = Literal["staff", "customer"]

#: (from, to) -> who may perform it. Anything not listed is `invalid_transition`.
TRANSITIONS: Final[Mapping[tuple[str, str], Performer]] = MappingProxyType(
    {
        ("received", "in_progress"): "staff",
        ("in_progress", "closed"): "staff",
        ("closed", "in_progress"): "staff",
        ("received", "cancelled"): "customer",
        ("in_progress", "cancelled"): "customer",
    }
)

#: Longest Assigned_Technician_Name (R14.6, R14.11); the `svc_tickets` check constraint has the same bound.
TECHNICIAN_NAME_MAX = 100


@dataclass(frozen=True, slots=True)
class CustomerActor:
    """The owning customer, as the lifecycle needs to know them: only the identity that owns tickets."""

    customer_id: uuid.UUID


Actor = StaffPrincipal | CustomerActor


class LifecycleError(Exception):
    """Base of the lifecycle errors. `code` is the API error code; the router picks the HTTP status."""

    code: str = ""

    def __init__(self) -> None:
        super().__init__(self.code)


class InvalidExpectedStatus(LifecycleError):
    """The expected status is missing or not one of the four statuses (HTTP 422)."""

    code = CODE_INVALID_EXPECTED_STATUS


class InvalidTargetStatus(LifecycleError):
    """The target status is missing or not one of the four statuses (HTTP 422)."""

    code = CODE_INVALID_TARGET_STATUS


class InvalidAssignedTechnicianName(LifecycleError):
    """The supplied technician name is longer than 100 characters after trimming (HTTP 422)."""

    code = CODE_INVALID_ASSIGNED_TECHNICIAN_NAME


class TicketNotFound(LifecycleError):
    """No such ticket, or a customer asked about a ticket they do not own (HTTP 404)."""

    code = CODE_TICKET_NOT_FOUND


class TicketStatusChanged(LifecycleError):
    """The stored status is not the expected one, now or at the moment of the write (HTTP 409)."""

    code = CODE_TICKET_STATUS_CHANGED


class InvalidTransition(LifecycleError):
    """The pair is not in the table for this actor, or the target is the current status (HTTP 409)."""

    code = CODE_INVALID_TRANSITION


class ResolutionRequired(LifecycleError):
    """A change to `closed` came without a resolution (HTTP 422)."""

    code = CODE_RESOLUTION_REQUIRED


class UnexpectedResolution(LifecycleError):
    """A resolution came with a change to a status other than `closed` (HTTP 422, `fields.resolution`)."""

    code = CODE_UNKNOWN_FIELD


def _clean_technician_name(raw: object) -> str | None:
    """The supplied name trimmed, or `None` when there is none (absent or blank after trimming)."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise InvalidAssignedTechnicianName
    name = raw.strip()
    if len(name) > TECHNICIAN_NAME_MAX:
        raise InvalidAssignedTechnicianName
    return name or None


def _new_assigned_name(
    stored_status: str, target: str, supplied: str | None, stored_name: str, actor: Actor
) -> str | None:
    """The value for `:new_name` of the guarded `UPDATE`; `None` keeps the stored name (R14.6, R14.7).

    Only the approval step (received to in_progress) can set a name. Every other change, including a
    reopen, passes `None`, so a name supplied with it is ignored and the stored name stays.
    """
    if (stored_status, target) != ("received", "in_progress"):
        return None
    if supplied is not None:
        return supplied
    if stored_name:
        return None
    # A staff actor here by construction: `received -> in_progress` is a staff pair. Capped at the column
    # limit so an unusually long username cannot turn an approval into a constraint violation.
    return actor.display_name[:TECHNICIAN_NAME_MAX]  # type: ignore[union-attr]


def _allowed(actor: Actor, stored: str, target: str) -> bool:
    performer = TRANSITIONS.get((stored, target))
    if performer is None:
        return False
    if isinstance(actor, StaffPrincipal):
        return performer == "staff"
    if isinstance(actor, CustomerActor):
        return performer == "customer"
    raise TypeError(f"unsupported actor: {type(actor).__name__}")


def change_status(
    session: Session,
    ticket_number: int,
    expected: str | None,
    target: str | None,
    actor: Actor,
    technician_name: str | None = None,
    resolution: Resolution | None = None,
    *,
    now: datetime | None = None,
) -> Ticket:
    """Apply one status change and return the ticket as stored afterwards. Never commits.

    `expected` is the status the caller last saw; `target` the status it asks for; `actor` a
    `StaffPrincipal` or a `CustomerActor`; `technician_name` the optional name of R14.6; `resolution` the
    service history and replaced parts of R29, required for the target `closed` and refused for every
    other target. `now` replaces the clock for `updated_at` and for the history entry, for tests. Raises a
    `LifecycleError` subclass or a `ResolutionValidationError` for every refusal; see the module docstring
    for the order.
    """
    if not isinstance(actor, StaffPrincipal | CustomerActor):
        raise TypeError(f"unsupported actor: {type(actor).__name__}")
    if not isinstance(expected, str) or expected not in STATUS_VALUES:
        raise InvalidExpectedStatus
    if not isinstance(target, str) or target not in STATUS_VALUES:
        raise InvalidTargetStatus
    supplied_name = _clean_technician_name(technician_name)

    query = (
        select(Ticket)
        .where(Ticket.ticket_number == ticket_number)
        # Re-read the row even when the session already holds the object: a stale status here would
        # defeat the check order.
        .execution_options(populate_existing=True)
    )
    if isinstance(actor, CustomerActor):
        query = query.where(Ticket.customer_id == actor.customer_id)
    ticket = session.execute(query).scalar_one_or_none()
    if ticket is None:
        raise TicketNotFound

    stored = ticket.status
    if stored != expected:
        raise TicketStatusChanged
    if not _allowed(actor, stored, target):
        raise InvalidTransition

    # Imported here, not at the top: `resolution` imports `history`, which imports this module.
    from app.service_tickets.services import resolution as resolution_service

    valid = None
    if target == "closed":
        if resolution is None:
            raise ResolutionRequired
        valid = resolution_service.validate(session, resolution)
    elif resolution is not None:
        raise UnexpectedResolution

    new_name = _new_assigned_name(stored, target, supplied_name, ticket.assigned_technician_name, actor)
    result = session.execute(
        update(Ticket)
        .where(Ticket.ticket_number == ticket_number, Ticket.status == expected)
        .values(
            status=target,
            updated_at=now if now is not None else datetime.now(UTC),
            assigned_technician_name=func.coalesce(
                literal(new_name, type_=Text), Ticket.assigned_technician_name
            ),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise TicketStatusChanged

    if valid is not None:
        resolution_service.store(session, ticket, valid, now=now)

    session.refresh(ticket)
    return ticket
