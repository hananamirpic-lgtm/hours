"""The ticket report: filtered counts per status and per category, and a page of matching tickets.

Requirements 18.1 to 18.9; design.md, "Staff routes" (the report returns per-status and per-category
counts from one `UNION ALL` statement).

Filters (all optional, combined with AND; an absent filter restricts nothing, R18.1):

* `date_from`, `date_to`: inclusive bounds on `svc_tickets.created_date`, the Asia/Jerusalem calendar
  date stored when the ticket was created (R18.2, R18.3). One bound alone is an open range; the same
  date twice selects one day. A `date_from` after `date_to` raises `InvalidDateRange` (R18.4). The
  router parses the raw query text with `grammars.parse_date_range`, which raises the same error for a
  value that is not a calendar date.
* `city`: matched against `svc_tickets.city_key`, the NFC, trimmed and case-folded city, with the filter
  value folded the same way, so the match is exact, trimmed and case-insensitive (R18.5). A value that
  is empty after trimming is no filter.
* `category_id`: one category, active or inactive (R18.6). A category that does not exist raises
  `InvalidFilter`. The router passes `None` for an absent filter and raises `InvalidFilter` itself for
  text that is not a UUID.

Counts. One statement, `SELECT ... GROUP BY status UNION ALL SELECT ... GROUP BY category`, gives both
sets from one snapshot, so each set sums to the same total (R18.8). The total is the sum of the status
counts. All four statuses are always present, zero included, and cancelled tickets count (R18.9).
`open` is received plus in progress. Categories are those with at least one matching ticket, active or
inactive, oldest first (`seq`).

The ticket table is a second statement: 25 rows a page, newest creation time first, ties by ticket
number (R18.7). It is skipped when the requested page starts past the last row, so a page beyond the end
costs no query and answers an empty list.

The module only reads, never commits, and raises no HTTP errors. A failure is an exception carrying the
API code (`InvalidDateRange`, `InvalidFilter`, `InvalidPage`, from `app.service_tickets.grammars`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Text,
    Uuid,
    cast,
    func,
    literal_column,
    null,
    select,
    union_all,
)
from sqlalchemy.orm import Session

from app.service_tickets.grammars import (
    STAFF_PAGE_SIZE,
    STATUS_VALUES,
    InvalidDateRange,
    InvalidFilter,
    InvalidPage,
    fold_key,
)
from app.service_tickets.models import Category, Customer, Ticket

_STATUS_KIND = "status"
_CATEGORY_KIND = "category"


@dataclass(frozen=True, slots=True)
class ReportCategory:
    """A category as the report shows it. `seq` orders the list (oldest first)."""

    id: uuid.UUID
    name_he: str
    name_en: str
    seq: int


@dataclass(frozen=True, slots=True)
class ReportCategoryCount:
    category: ReportCategory
    count: int


@dataclass(frozen=True, slots=True)
class ReportTicketRow:
    """A row of the report table: the fields of the staff list row (R15.2)."""

    ticket_number: int
    created_at: datetime
    customer_name: str
    city: str
    apartment: str
    category: ReportCategory
    status: str
    assigned_technician_name: str
    urgent: bool


@dataclass(frozen=True, slots=True)
class Report:
    """The report. `by_status` has all four statuses; both count sets sum to `total` (R18.8)."""

    total: int
    open: int
    by_status: dict[str, int]
    by_category: tuple[ReportCategoryCount, ...]
    tickets: tuple[ReportTicketRow, ...]
    page: int
    page_size: int


def _conditions(
    date_from: date | None, date_to: date | None, city: str | None, category_id: uuid.UUID | None
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if date_from is not None:
        conditions.append(Ticket.created_date >= date_from)
    if date_to is not None:
        conditions.append(Ticket.created_date <= date_to)
    city_key = fold_key(city) if city is not None else ""
    if city_key:
        conditions.append(Ticket.city_key == city_key)
    if category_id is not None:
        conditions.append(Ticket.category_id == category_id)
    return conditions


def get_report(
    session: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    city: str | None = None,
    category_id: uuid.UUID | None = None,
    page: int = 1,
) -> Report:
    """The report for the filters, with page `page` (1-based) of the ticket table. Never commits."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise InvalidDateRange
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise InvalidPage
    if category_id is not None and session.get(Category, category_id) is None:
        raise InvalidFilter

    conditions = _conditions(date_from, date_to, city, category_id)

    by_status_query = (
        select(
            literal_column(f"'{_STATUS_KIND}'").label("kind"),
            Ticket.status.label("status"),
            cast(null(), Uuid(as_uuid=True)).label("category_id"),
            cast(null(), Text).label("name_he"),
            cast(null(), Text).label("name_en"),
            cast(null(), BigInteger).label("seq"),
            func.count().label("n"),
        )
        .where(*conditions)
        .group_by(Ticket.status)
    )
    by_category_query = (
        select(
            literal_column(f"'{_CATEGORY_KIND}'"),
            cast(null(), Text),
            Ticket.category_id,
            Category.name_he,
            Category.name_en,
            Category.seq,
            func.count(),
        )
        .join(Category, Category.id == Ticket.category_id)
        .where(*conditions)
        .group_by(Ticket.category_id, Category.name_he, Category.name_en, Category.seq)
    )

    by_status = dict.fromkeys(STATUS_VALUES, 0)
    by_category: list[ReportCategoryCount] = []
    for kind, status, cat_id, name_he, name_en, seq, count in session.execute(
        union_all(by_status_query, by_category_query)
    ):
        if kind == _STATUS_KIND:
            by_status[status] = int(count)
        else:
            by_category.append(
                ReportCategoryCount(
                    category=ReportCategory(id=cat_id, name_he=name_he, name_en=name_en, seq=int(seq)),
                    count=int(count),
                )
            )
    by_category.sort(key=lambda item: item.category.seq)

    total = sum(by_status.values())
    offset = (page - 1) * STAFF_PAGE_SIZE
    rows: list[ReportTicketRow] = []
    if offset < total:
        page_query = (
            select(Ticket, Customer.full_name, Category)
            .join(Customer, Customer.id == Ticket.customer_id)
            .join(Category, Category.id == Ticket.category_id)
            .where(*conditions)
            .order_by(Ticket.created_at.desc(), Ticket.ticket_number.desc())
            .limit(STAFF_PAGE_SIZE)
            .offset(offset)
        )
        for ticket, customer_name, category in session.execute(page_query):
            rows.append(
                ReportTicketRow(
                    ticket_number=ticket.ticket_number,
                    created_at=ticket.created_at,
                    customer_name=customer_name,
                    city=ticket.city,
                    apartment=ticket.apartment,
                    category=ReportCategory(
                        id=category.id, name_he=category.name_he, name_en=category.name_en, seq=category.seq
                    ),
                    status=ticket.status,
                    assigned_technician_name=ticket.assigned_technician_name,
                    urgent=ticket.urgent,
                )
            )

    return Report(
        total=total,
        open=by_status["received"] + by_status["in_progress"],
        by_status=by_status,
        by_category=tuple(by_category),
        tickets=tuple(rows),
        page=page,
        page_size=STAFF_PAGE_SIZE,
    )
