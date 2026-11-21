"""The staff router of the service ticket API: `/staff/*` (design.md, "Staff endpoints").

Every route here sits behind `staff_principal` (an administrator, an operations administrator or a
technician). The router declares the dependency once, so a route added later cannot forget it; the
configuration routes that task 13.6 adds depend on `config_staff_principal` as well, which in turn
reuses the same cached `staff_principal` result.

This module holds the dashboard, ticket and history routes (task 13.4). The customer and report routes
(13.5) and the configuration routes (13.6) are added to the same `router`, in the sections marked
below, and reuse the helpers at the top:

* `_ERROR_STATUS` maps a domain error `code` to its HTTP status. New sections add their codes to it.
* `_domain_errors(session)` turns a service exception into the package's error envelope and rolls the
  session back first, so a refused request changes nothing.
* The `*_out` functions map a service result dataclass to the response model of `schemas.py`.

Transactions. The services never commit. A handler that writes commits once, after it has built its
response, and rolls back on a domain error. A read-only handler commits nothing.

Query parameters of the list are declared as raw strings and parsed by the pure functions of
`grammars`, so every failure carries one of the package's codes instead of FastAPI's generic 422.
Ticket-number and entry-id path parameters are raw strings too. A malformed one is not an error of its
own: it is replaced by a value that matches no row (`_NO_TICKET`, `_NO_ENTRY`), so it takes exactly the
path a missing record takes, validation order included (R12.3).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.service_tickets.api.deps import (
    CONFIG_STAFF_ROLES,
    ServiceSession,
    StaffPrincipalDep,
    staff_principal,
)
from app.service_tickets.api.errors import service_error
from app.service_tickets.api.route import ServiceRoute
from app.service_tickets.error_codes import (
    CODE_HISTORY_ENTRY_NOT_FOUND,
    CODE_INVALID_ASSIGNED_TECHNICIAN_NAME,
    CODE_INVALID_DATE_RANGE,
    CODE_INVALID_EXPECTED_STATUS,
    CODE_INVALID_FILTER,
    CODE_INVALID_HISTORY_TEXT,
    CODE_INVALID_PAGE,
    CODE_INVALID_TARGET_STATUS,
    CODE_INVALID_TECHNICIAN_NAME,
    CODE_INVALID_TICKET_NUMBER,
    CODE_INVALID_TRANSITION,
    CODE_TICKET_NOT_FOUND,
    CODE_TICKET_STATUS_CHANGED,
)
from app.service_tickets.grammars import (
    GrammarError,
    InvalidFilter,
    parse_date_range,
    parse_page,
    parse_status_filter,
    parse_ticket_number_path,
    parse_ticket_search,
    parse_uuid_path,
)
from app.service_tickets.models import HistoryEntry
from app.service_tickets.schemas import (
    AssignedTechnicianRequest,
    CategoryRef,
    DashboardResponse,
    HistoryEntryOut,
    NumberWarning,
    StaffTicketDetail,
    StaffTicketPage,
    StaffTicketRow,
    StatusChangeRequest,
)
from app.service_tickets.services import dashboard as dashboard_service
from app.service_tickets.services import history as history_service
from app.service_tickets.services import lifecycle, ticket_queries
from app.service_tickets.services.history import HistoryValidationError
from app.service_tickets.services.lifecycle import LifecycleError
from app.service_tickets.services.ticket_queries import (
    StaffTicketDetailView,
    StaffTicketPageView,
    StaffTicketRowView,
    TicketCategory,
)

router = APIRouter(
    prefix="/staff",
    route_class=ServiceRoute,
    dependencies=[Depends(staff_principal)],
)

#: A ticket number that no ticket has. A malformed path value becomes this, so it behaves as a missing
#: ticket at the point where the service looks the ticket up (outside 10000 to 99999 on purpose).
_NO_TICKET = 0

#: An entry id that no entry has (the nil UUID), for a malformed entry id in the path.
_NO_ENTRY = uuid.UUID(int=0)

#: Status of every domain error `code` the staff router can answer. Sections that add routes add their
#: codes here. A code that is not listed is not a domain refusal: the exception propagates (HTTP 500).
_ERROR_STATUS: dict[str, HTTPStatus] = {
    # tickets and history
    CODE_TICKET_NOT_FOUND: HTTPStatus.NOT_FOUND,
    CODE_HISTORY_ENTRY_NOT_FOUND: HTTPStatus.NOT_FOUND,
    CODE_TICKET_STATUS_CHANGED: HTTPStatus.CONFLICT,
    CODE_INVALID_TRANSITION: HTTPStatus.CONFLICT,
    CODE_INVALID_EXPECTED_STATUS: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_TARGET_STATUS: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_ASSIGNED_TECHNICIAN_NAME: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_HISTORY_TEXT: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_TECHNICIAN_NAME: HTTPStatus.UNPROCESSABLE_ENTITY,
    # list and report query parameters
    CODE_INVALID_PAGE: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_TICKET_NUMBER: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_DATE_RANGE: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_FILTER: HTTPStatus.UNPROCESSABLE_ENTITY,
}


# --------------------------------------------------------------------------------- helpers


def _http_error(error: Exception) -> Exception:
    """The `HTTPException` for a domain error, or `error` itself when it is not a listed refusal."""
    code = getattr(error, "code", None)
    status = _ERROR_STATUS.get(code) if isinstance(code, str) else None
    if status is None:
        return error
    fields = error.fields if isinstance(error, HistoryValidationError) else None
    return service_error(status, code, fields=fields)


@contextmanager
def _domain_errors(session: Session) -> Iterator[None]:
    """Answer a service or parser refusal with the package's envelope, after a rollback."""
    try:
        yield
    except (LifecycleError, GrammarError, HistoryValidationError) as error:
        session.rollback()
        mapped = _http_error(error)
        if mapped is error:
            raise
        raise mapped from error


def _ticket_number_in_path(raw: str) -> int:
    number = parse_ticket_number_path(raw)
    return _NO_TICKET if number is None else number


def _entry_id_in_path(raw: str) -> uuid.UUID:
    entry_id = parse_uuid_path(raw)
    return _NO_ENTRY if entry_id is None else entry_id


def _flag(raw: str | None) -> bool:
    """A boolean query value: absent, empty or `false` is off, `true` is on, else `invalid_filter`."""
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in ("", "false"):
        return False
    if value == "true":
        return True
    raise InvalidFilter


def _category_filter(raw: str | None) -> uuid.UUID | None:
    """The category filter: absent or blank is no filter; text that is not a UUID is `invalid_filter`."""
    if raw is None or not raw.strip():
        return None
    category_id = parse_uuid_path(raw.strip())
    if category_id is None:
        raise InvalidFilter
    return category_id


# --- result dataclass -> response model


def _category_out(category: TicketCategory) -> CategoryRef:
    return CategoryRef(id=category.id, name_he=category.name_he, name_en=category.name_en)


def _staff_row_out(row: StaffTicketRowView) -> StaffTicketRow:
    return StaffTicketRow(
        ticket_number=row.ticket_number,
        created_at=row.created_at,
        customer_name=row.customer_name,
        city=row.city,
        apartment=row.apartment,
        category=_category_out(row.category),
        status=row.status,  # type: ignore[arg-type]  # the column check constraint holds the four values
        assigned_technician_name=row.assigned_technician_name,
        urgent=row.urgent,
    )


def _staff_page_out(page: StaffTicketPageView) -> StaffTicketPage:
    return StaffTicketPage(
        items=[_staff_row_out(row) for row in page.items],
        page=page.page,
        page_size=page.page_size,
        total=page.total,
    )


def _history_entry_out(entry: HistoryEntry) -> HistoryEntryOut:
    return HistoryEntryOut.model_validate(entry)


def _staff_detail_out(view: StaffTicketDetailView) -> StaffTicketDetail:
    return StaffTicketDetail(
        ticket_number=view.ticket_number,
        created_at=view.created_at,
        customer_name=view.customer_name,
        city=view.city,
        apartment=view.apartment,
        category=_category_out(view.category),
        status=view.status,  # type: ignore[arg-type]
        assigned_technician_name=view.assigned_technician_name,
        urgent=view.urgent,
        phone=view.phone,
        description=view.description,
        has_photo=view.has_photo,
        history=[_history_entry_out(entry) for entry in view.history],
        updated_at=view.updated_at,
    )


def _ticket_detail(session: Session, ticket_number: int) -> StaffTicketDetail:
    """The detail response of a ticket as the session sees it now (inside the open transaction)."""
    return _staff_detail_out(ticket_queries.get_staff_ticket(session, ticket_number))


# --- request bodies local to this router


class _HistoryBody(BaseModel):
    """Body of the two history routes (`schemas.HistoryEntryRequest` made lenient, see below).

    The values are `Any` and default to `None` so that a missing or non-string value reaches the history
    service and gets that service's code (`invalid_history_text`, `invalid_technician_name`) instead of
    a generic validation error, and so a body with two bad fields reports both (R16.6). Only `POST`
    fills in an absent technician name (with the acting username, R16.4).
    """

    model_config = ConfigDict(extra="forbid")

    entry_text: Any = None
    technician_name: Any = None


# --------------------------------------------------------------------------------- dashboard


@router.get("/dashboard", response_model=DashboardResponse)
def get_dashboard(session: ServiceSession, principal: StaffPrincipalDep) -> DashboardResponse:
    """Three counts from one read; the ticket-numbers warning only for configuration staff (R17.1, R10.9)."""
    with _domain_errors(session):
        counts = dashboard_service.get_dashboard(session, config_staff=principal.role in CONFIG_STAFF_ROLES)
    warning = (
        NumberWarning(remaining=counts.ticket_numbers_remaining)
        if counts.ticket_numbers_remaining is not None
        else None
    )
    return DashboardResponse(
        open=counts.open,
        in_progress=counts.in_progress,
        urgent=counts.urgent,
        ticket_number_warning=warning,
    )


# --------------------------------------------------------------------------------- tickets


@router.get("/tickets", response_model=StaffTicketPage)
def list_tickets(
    session: ServiceSession,
    page: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
    status: Annotated[list[str] | None, Query()] = None,
    category_id: Annotated[str | None, Query()] = None,
    city: Annotated[str | None, Query()] = None,
    urgent_only: Annotated[str | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    urgent_first: Annotated[str | None, Query()] = None,
) -> StaffTicketPage:
    """One page (25) of all tickets. Every parameter is a raw string parsed by `grammars` (R15.3 to R15.6)."""
    with _domain_errors(session):
        page_number = parse_page(page)
        ticket_search = parse_ticket_search(search)
        statuses = parse_status_filter(status)
        start, end = parse_date_range(date_from, date_to)
        result = ticket_queries.list_staff_tickets(
            session,
            search=ticket_search,
            statuses=statuses,
            category_id=_category_filter(category_id),
            city=city,
            urgent_only=_flag(urgent_only),
            date_from=start,
            date_to=end,
            urgent_first=_flag(urgent_first),
            page=page_number,
        )
    return _staff_page_out(result)


@router.get("/tickets/{ticket_number}", response_model=StaffTicketDetail)
def get_ticket(ticket_number: str, session: ServiceSession) -> StaffTicketDetail:
    """A ticket with the owning customer's current name and its history, oldest entry first (R16.1)."""
    with _domain_errors(session):
        return _ticket_detail(session, _ticket_number_in_path(ticket_number))


@router.post("/tickets/{ticket_number}/status", response_model=StaffTicketDetail)
def change_ticket_status(
    ticket_number: str,
    body: StatusChangeRequest,
    session: ServiceSession,
    principal: StaffPrincipalDep,
) -> StaffTicketDetail:
    """Move a ticket to another status; the caller names the status it last saw (R14.2 to R14.8, R14.11).

    Approving a received ticket without a technician name stores the acting username as the name
    (`lifecycle.change_status`, R14.6).
    """
    number = _ticket_number_in_path(ticket_number)
    with _domain_errors(session):
        lifecycle.change_status(
            session,
            number,
            body.expected_status,
            body.target_status,
            principal,
            body.assigned_technician_name,
        )
        detail = _ticket_detail(session, number)
    session.commit()
    return detail


@router.put("/tickets/{ticket_number}/assigned-technician", response_model=StaffTicketDetail)
def set_assigned_technician(
    ticket_number: str, body: AssignedTechnicianRequest, session: ServiceSession
) -> StaffTicketDetail:
    """Set or clear the assigned technician name, in every status (R16.2, R16.7, R16.10)."""
    number = _ticket_number_in_path(ticket_number)
    with _domain_errors(session):
        history_service.set_assigned_technician_name(session, number, body.assigned_technician_name)
        detail = _ticket_detail(session, number)
    session.commit()
    return detail


# --------------------------------------------------------------------------------- history


@router.post(
    "/tickets/{ticket_number}/history", response_model=HistoryEntryOut, status_code=HTTPStatus.CREATED
)
def add_history_entry(
    ticket_number: str,
    body: _HistoryBody,
    session: ServiceSession,
    principal: StaffPrincipalDep,
) -> HistoryEntryOut:
    """Add a note. An absent technician name defaults to the acting username (R16.3, R16.4)."""
    number = _ticket_number_in_path(ticket_number)
    technician_name = principal.display_name if body.technician_name is None else body.technician_name
    with _domain_errors(session):
        entry = history_service.add_entry(session, number, body.entry_text, technician_name)
        out = _history_entry_out(entry)
    session.commit()
    return out


@router.put("/tickets/{ticket_number}/history/{entry_id}", response_model=HistoryEntryOut)
def edit_history_entry(
    ticket_number: str, entry_id: str, body: _HistoryBody, session: ServiceSession
) -> HistoryEntryOut:
    """Replace the text and the technician name of a note; the last save wins (R16.5, R16.8)."""
    number = _ticket_number_in_path(ticket_number)
    with _domain_errors(session):
        entry = history_service.edit_entry(
            session, number, _entry_id_in_path(entry_id), body.entry_text, body.technician_name
        )
        out = _history_entry_out(entry)
    session.commit()
    return out


# --------------------------------------------------------------------------------- customers, reports
# Added by task 13.5: GET/POST /customers, PUT /customers/{id}, POST /customers/{id}/deactivate,
# /reactivate, /reset-password, GET /reports.

# --------------------------------------------------------------------------------- configuration
# Added by task 13.6: GET /categories (ticket staff), then the category writes, /settings and /labels
# behind `config_staff_principal`.
