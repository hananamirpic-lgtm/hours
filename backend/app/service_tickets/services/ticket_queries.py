"""Ticket list and detail queries, for customers and for staff.

Requirements 12.1, 12.2, 12.3, 12.9 (customer list and detail), 15.1 to 15.3, 15.5, 15.6, 15.8 (staff
list) and 16.1, 16.9 (staff detail); design.md, "Staff routes".

Customer side
-------------
* `list_customer_tickets`: only the tickets of the given customer, newest creation time first with the
  ticket number descending as the tie-breaker, 50 per page.
* `get_customer_ticket`: one own ticket. A ticket that belongs to someone else, a ticket that does not
  exist and a number that is not a ticket number at all (including `None` and text that is not five ASCII
  digits in range) raise the same `TicketNotFound`, so a customer cannot tell them apart (R12.3).

The customer result types are declared without history, technician name, last-modified time, customer id
or photo key, so none of them can reach a customer response from here (R12.4).

Staff side
----------
* `list_staff_tickets`: all tickets of all customers, 25 per page. Filters are combined with AND: a status
  set, one category (active or inactive), the city (compared on `city_key`, the NFC, trimmed, case-folded
  city, so the match is exact, trimmed and case-insensitive), urgent only, and an inclusive `created_date`
  range. An exact ticket-number search replaces every filter (R15.3); the filter values are still checked,
  so a request with an impossible range or an unknown category fails the same way with and without a
  search (R15.6). `urgent_first` orders the whole result with open urgent tickets first (R15.8).
* `get_staff_ticket`: one ticket by number, with the owning customer's current name and the history,
  oldest entry first.

`total` comes from `count(*) OVER ()` in the same statement as the page, so it describes the same
snapshot as the rows. Only a page that starts past the last row has no row to read it from; that case
(and only that case, and never page 1) runs a second `count(*)` over the same conditions, and answers an
empty list with the right total (R12.9).

The module only reads, never commits, and raises no HTTP errors. A failure is an exception carrying the
API code: `TicketNotFound` (from the lifecycle module, so the router has one mapping for it) and
`InvalidDateRange`, `InvalidFilter`, `InvalidPage` (from `app.service_tickets.grammars`). Query
parameters arrive already parsed by the pure functions of that module.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import ColumnElement, and_, case, func, select
from sqlalchemy.orm import Session

from app.service_tickets.grammars import (
    CUSTOMER_PAGE_SIZE,
    STAFF_PAGE_SIZE,
    STATUS_VALUES,
    TICKET_NUMBER_MAX,
    TICKET_NUMBER_MIN,
    InvalidDateRange,
    InvalidFilter,
    InvalidPage,
    fold_key,
    parse_ticket_number_path,
)
from app.service_tickets.models import Category, Customer, HistoryEntry, Ticket
from app.service_tickets.services.lifecycle import TicketNotFound

__all__ = [
    "CustomerTicketDetailView",
    "CustomerTicketPageView",
    "CustomerTicketRowView",
    "StaffTicketDetailView",
    "StaffTicketPageView",
    "StaffTicketRowView",
    "TicketCategory",
    "TicketNotFound",
    "get_customer_ticket",
    "get_staff_ticket",
    "list_customer_tickets",
    "list_staff_tickets",
]

#: The statuses of an open ticket; the urgent-first ordering puts urgent tickets in these first (R15.8).
_OPEN_STATUSES = ("received", "in_progress")


@dataclass(frozen=True, slots=True)
class TicketCategory:
    """The category of a ticket, active or not: id and the two names, nothing about its state."""

    id: uuid.UUID
    name_he: str
    name_en: str


@dataclass(frozen=True, slots=True)
class CustomerTicketRowView:
    """A row of the customer list (R12.1): exactly these five values."""

    ticket_number: int
    category: TicketCategory
    created_date: date
    status: str
    urgent: bool


@dataclass(frozen=True, slots=True)
class CustomerTicketDetailView(CustomerTicketRowView):
    """An own ticket in full (R12.2): the values stored on the ticket, not the customer record."""

    city: str
    apartment: str
    phone: str
    description: str
    has_photo: bool


@dataclass(frozen=True, slots=True)
class CustomerTicketPageView:
    items: tuple[CustomerTicketRowView, ...]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class StaffTicketRowView:
    """A row of the staff list (R15.2). `customer_name` is the owning customer's current name."""

    ticket_number: int
    created_at: datetime
    customer_name: str
    city: str
    apartment: str
    category: TicketCategory
    status: str
    assigned_technician_name: str
    urgent: bool


@dataclass(frozen=True, slots=True)
class StaffTicketDetailView(StaffTicketRowView):
    """A ticket as staff open it (R16.1): the row plus contact data, history and last-modified time.

    `history` holds the entries oldest creation time first (R16.9).
    """

    phone: str
    description: str
    has_photo: bool
    history: tuple[HistoryEntry, ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StaffTicketPageView:
    items: tuple[StaffTicketRowView, ...]
    page: int
    page_size: int
    total: int


def _check_page(page: object) -> int:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise InvalidPage
    return page


def _ticket_number(value: object) -> int:
    """The ticket number named by `value`, or `TicketNotFound` for anything that is not one.

    Accepts the number itself or the raw path text. `None`, a bool, text that is not five ASCII digits
    and a number outside 10000 to 99999 are all the same outcome as a ticket that does not exist.
    """
    number: int | None
    if isinstance(value, bool):
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, str):
        number = parse_ticket_number_path(value)
    else:
        number = None
    if number is None or not TICKET_NUMBER_MIN <= number <= TICKET_NUMBER_MAX:
        raise TicketNotFound
    return number


def _category(category_id: uuid.UUID, name_he: str, name_en: str) -> TicketCategory:
    return TicketCategory(id=category_id, name_he=name_he, name_en=name_en)


# --------------------------------------------------------------------------------- customer


def list_customer_tickets(
    session: Session, customer_id: uuid.UUID, *, page: int = 1
) -> CustomerTicketPageView:
    """One page (50) of the customer's own tickets, newest first. A page past the end is an empty list."""
    page = _check_page(page)
    offset = (page - 1) * CUSTOMER_PAGE_SIZE
    mine = Ticket.customer_id == customer_id

    rows = session.execute(
        select(
            Ticket.ticket_number,
            Ticket.created_date,
            Ticket.status,
            Ticket.urgent,
            Category.id.label("category_id"),
            Category.name_he,
            Category.name_en,
            func.count().over().label("total"),
        )
        .join(Category, Category.id == Ticket.category_id)
        .where(mine)
        .order_by(Ticket.created_at.desc(), Ticket.ticket_number.desc())
        .limit(CUSTOMER_PAGE_SIZE)
        .offset(offset)
    ).all()

    if rows:
        total = int(rows[0].total)
    elif offset == 0:
        total = 0
    else:
        total = int(session.execute(select(func.count()).select_from(Ticket).where(mine)).scalar_one())

    return CustomerTicketPageView(
        items=tuple(
            CustomerTicketRowView(
                ticket_number=row.ticket_number,
                category=_category(row.category_id, row.name_he, row.name_en),
                created_date=row.created_date,
                status=row.status,
                urgent=row.urgent,
            )
            for row in rows
        ),
        page=page,
        page_size=CUSTOMER_PAGE_SIZE,
        total=total,
    )


def get_customer_ticket(
    session: Session, customer_id: uuid.UUID, ticket_number: object
) -> CustomerTicketDetailView:
    """An own ticket in full. Missing, not owned and malformed all raise the same `TicketNotFound`."""
    number = _ticket_number(ticket_number)
    row = session.execute(
        select(
            Ticket.ticket_number,
            Ticket.created_date,
            Ticket.status,
            Ticket.urgent,
            Ticket.city,
            Ticket.apartment,
            Ticket.phone,
            Ticket.description,
            Ticket.photo_key.is_not(None).label("has_photo"),
            Category.id.label("category_id"),
            Category.name_he,
            Category.name_en,
        )
        .join(Category, Category.id == Ticket.category_id)
        .where(Ticket.ticket_number == number, Ticket.customer_id == customer_id)
    ).one_or_none()
    if row is None:
        raise TicketNotFound
    return CustomerTicketDetailView(
        ticket_number=row.ticket_number,
        category=_category(row.category_id, row.name_he, row.name_en),
        created_date=row.created_date,
        status=row.status,
        urgent=row.urgent,
        city=row.city,
        apartment=row.apartment,
        phone=row.phone,
        description=row.description,
        has_photo=bool(row.has_photo),
    )


# --------------------------------------------------------------------------------- staff


def _staff_conditions(
    *,
    search: int | None,
    statuses: tuple[str, ...],
    category_id: uuid.UUID | None,
    city: str | None,
    urgent_only: bool,
    date_from: date | None,
    date_to: date | None,
) -> list[ColumnElement[bool]]:
    if search is not None:  # an exact number replaces every filter (R15.3)
        return [Ticket.ticket_number == search]
    conditions: list[ColumnElement[bool]] = []
    if statuses:
        conditions.append(Ticket.status.in_(statuses))
    if category_id is not None:
        conditions.append(Ticket.category_id == category_id)
    city_key = fold_key(city) if city is not None else ""
    if city_key:
        conditions.append(Ticket.city_key == city_key)
    if urgent_only:
        conditions.append(Ticket.urgent.is_(True))
    if date_from is not None:
        conditions.append(Ticket.created_date >= date_from)
    if date_to is not None:
        conditions.append(Ticket.created_date <= date_to)
    return conditions


def list_staff_tickets(
    session: Session,
    *,
    search: int | None = None,
    statuses: Iterable[str] = (),
    category_id: uuid.UUID | None = None,
    city: str | None = None,
    urgent_only: bool = False,
    date_from: date | None = None,
    date_to: date | None = None,
    urgent_first: bool = False,
    page: int = 1,
) -> StaffTicketPageView:
    """One page (25) of all tickets for the search and filters. Never commits.

    `search` is the parsed ticket number (`grammars.parse_ticket_search`) or `None`. `statuses` is the
    parsed status set (`grammars.parse_status_filter`); empty means no status filter. A date range with
    `date_from` after `date_to`, an unknown status or a category that does not exist raises
    `InvalidDateRange` or `InvalidFilter`, with or without a search. A page past the end is an empty list
    with the right total.
    """
    page = _check_page(page)
    status_set = tuple(dict.fromkeys(statuses))
    if any(status not in STATUS_VALUES for status in status_set):
        raise InvalidFilter
    if date_from is not None and date_to is not None and date_from > date_to:
        raise InvalidDateRange
    if category_id is not None and session.get(Category, category_id) is None:
        raise InvalidFilter

    conditions = _staff_conditions(
        search=search,
        statuses=status_set,
        category_id=category_id,
        city=city,
        urgent_only=urgent_only,
        date_from=date_from,
        date_to=date_to,
    )

    order = [Ticket.created_at.desc(), Ticket.ticket_number.desc()]
    if urgent_first:
        is_urgent_open = and_(Ticket.urgent.is_(True), Ticket.status.in_(_OPEN_STATUSES))
        order.insert(0, case((is_urgent_open, 0), else_=1))

    offset = (page - 1) * STAFF_PAGE_SIZE
    rows = session.execute(
        select(
            Ticket.ticket_number,
            Ticket.created_at,
            Customer.full_name,
            Ticket.city,
            Ticket.apartment,
            Ticket.status,
            Ticket.assigned_technician_name,
            Ticket.urgent,
            Category.id.label("category_id"),
            Category.name_he,
            Category.name_en,
            func.count().over().label("total"),
        )
        .join(Customer, Customer.id == Ticket.customer_id)
        .join(Category, Category.id == Ticket.category_id)
        .where(*conditions)
        .order_by(*order)
        .limit(STAFF_PAGE_SIZE)
        .offset(offset)
    ).all()

    if rows:
        total = int(rows[0].total)
    elif offset == 0:
        total = 0
    else:
        total = int(session.execute(select(func.count()).select_from(Ticket).where(*conditions)).scalar_one())

    return StaffTicketPageView(
        items=tuple(
            StaffTicketRowView(
                ticket_number=row.ticket_number,
                created_at=row.created_at,
                customer_name=row.full_name,
                city=row.city,
                apartment=row.apartment,
                category=_category(row.category_id, row.name_he, row.name_en),
                status=row.status,
                assigned_technician_name=row.assigned_technician_name,
                urgent=row.urgent,
            )
            for row in rows
        ),
        page=page,
        page_size=STAFF_PAGE_SIZE,
        total=total,
    )


def get_staff_ticket(session: Session, ticket_number: object) -> StaffTicketDetailView:
    """A ticket as staff open it, with the owning customer's current name and the history oldest first.

    A missing ticket and a malformed number both raise `TicketNotFound`.
    """
    number = _ticket_number(ticket_number)
    row = session.execute(
        select(
            Ticket.id.label("ticket_id"),
            Ticket.ticket_number,
            Ticket.created_at,
            Customer.full_name,
            Ticket.city,
            Ticket.apartment,
            Ticket.status,
            Ticket.assigned_technician_name,
            Ticket.urgent,
            Ticket.phone,
            Ticket.description,
            Ticket.photo_key.is_not(None).label("has_photo"),
            Ticket.updated_at,
            Category.id.label("category_id"),
            Category.name_he,
            Category.name_en,
        )
        .join(Customer, Customer.id == Ticket.customer_id)
        .join(Category, Category.id == Ticket.category_id)
        .where(Ticket.ticket_number == number)
    ).one_or_none()
    if row is None:
        raise TicketNotFound

    history = session.execute(
        select(HistoryEntry)
        .where(HistoryEntry.ticket_id == row.ticket_id)
        .order_by(HistoryEntry.created_at, HistoryEntry.id)
        .execution_options(populate_existing=True)
    ).scalars()

    return StaffTicketDetailView(
        ticket_number=row.ticket_number,
        created_at=row.created_at,
        customer_name=row.full_name,
        city=row.city,
        apartment=row.apartment,
        category=_category(row.category_id, row.name_he, row.name_en),
        status=row.status,
        assigned_technician_name=row.assigned_technician_name,
        urgent=row.urgent,
        phone=row.phone,
        description=row.description,
        has_photo=bool(row.has_photo),
        history=tuple(history),
        updated_at=row.updated_at,
    )
