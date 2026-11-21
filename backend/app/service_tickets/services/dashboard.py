"""The staff dashboard: three ticket counts from one read, and the low-ticket-numbers warning.

Requirements 17.1, 17.2, 17.3 (counts) and 10.9 (the warning); design.md, "Staff routes".

The three counts come from one `SELECT` with three `count(*) FILTER (WHERE ...)` aggregates, so they
are read from a single snapshot of `svc_tickets` and the invariant of R17.3 holds by construction:

    open         status IN ('received', 'in_progress')
    in_progress  status = 'in_progress'
    urgent       urgent AND status IN ('received', 'in_progress')

`closed` and `cancelled` tickets match none of them (R17.2). An aggregate with no matching row is 0, never
`NULL`, because `count` returns 0 over an empty set. Open is at least in progress and at least urgent
because both are conditions inside open's condition.

The ticket-numbers warning (R10.9) is returned only to configuration staff, and only while fewer than
`LOW_NUMBER_WARNING_THRESHOLD` (1,000) numbers remain. For anyone else the sequence is not read at all.
The caller says which kind of staff member it is (`config_staff`); this module takes no role object, so
it depends on neither the principal types nor the Hours `users` table.

The module only reads, never commits, and raises no HTTP errors.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.service_tickets.models import Ticket
from app.service_tickets.services.numbering import LOW_NUMBER_WARNING_THRESHOLD, remaining_numbers

#: The statuses of an open ticket (R17.1). The same two values the staff list filter uses.
OPEN_STATUSES = ("received", "in_progress")


@dataclass(frozen=True, slots=True)
class DashboardCounts:
    """The dashboard answer.

    `ticket_numbers_remaining` is `None` unless the caller is configuration staff and fewer than 1,000
    numbers remain; then it is the remaining count.
    """

    open: int
    in_progress: int
    urgent: int
    ticket_numbers_remaining: int | None = None


def get_dashboard(session: Session, *, config_staff: bool = False) -> DashboardCounts:
    """The three counts over all tickets of all customers, read in one statement.

    `config_staff` is true when the caller may see the numbers warning (an administrator or an operations
    administrator; not a technician). Only then is the sequence read.
    """
    is_open = Ticket.status.in_(OPEN_STATUSES)
    row = session.execute(
        select(
            func.count().filter(is_open).label("open"),
            func.count().filter(Ticket.status == "in_progress").label("in_progress"),
            func.count().filter(is_open, Ticket.urgent.is_(True)).label("urgent"),
        ).select_from(Ticket)
    ).one()

    warning: int | None = None
    if config_staff:
        remaining = remaining_numbers(session)
        if remaining < LOW_NUMBER_WARNING_THRESHOLD:
            warning = remaining

    return DashboardCounts(
        open=int(row.open),
        in_progress=int(row.in_progress),
        urgent=int(row.urgent),
        ticket_numbers_remaining=warning,
    )
