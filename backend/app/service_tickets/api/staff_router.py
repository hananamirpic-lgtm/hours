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

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from python_multipart.exceptions import FormParserError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.service_tickets.api import branding_form
from app.service_tickets.api.customer_router import (
    FileParts,
    PhotoStoreDep,
    TicketAllocatorDep,
    _read_file_parts,
    _store_ticket,
    _submission_values,
    form_config_body,
)
from app.service_tickets.api.deps import (
    CONFIG_STAFF_ROLES,
    ServiceSession,
    StaffPrincipalDep,
    config_staff_principal,
    staff_principal,
)
from app.service_tickets.api.errors import service_error
from app.service_tickets.api.route import CODE_INVALID_REQUEST, ServiceRoute
from app.service_tickets.error_codes import (
    CODE_CATEGORY_NOT_FOUND,
    CODE_CUSTOMER_INACTIVE,
    CODE_CUSTOMER_NOT_FOUND,
    CODE_DUPLICATE_CATEGORY,
    CODE_DUPLICATE_PART,
    CODE_DUPLICATE_SETTLEMENT,
    CODE_EXPORT_TOO_LARGE,
    CODE_HISTORY_ENTRY_NOT_FOUND,
    CODE_INVALID_APARTMENT,
    CODE_INVALID_ASSIGNED_TECHNICIAN_NAME,
    CODE_INVALID_CITY,
    CODE_INVALID_DATE_RANGE,
    CODE_INVALID_EXPECTED_STATUS,
    CODE_INVALID_FILTER,
    CODE_INVALID_FULL_NAME,
    CODE_INVALID_HISTORY_TEXT,
    CODE_INVALID_LABEL_EN,
    CODE_INVALID_LABEL_HE,
    CODE_INVALID_NAME_EN,
    CODE_INVALID_NAME_HE,
    CODE_INVALID_PAGE,
    CODE_INVALID_PART,
    CODE_INVALID_PART_NAME_EN,
    CODE_INVALID_PART_NAME_HE,
    CODE_INVALID_PHONE,
    CODE_INVALID_SETTLEMENT,
    CODE_INVALID_SETTLEMENT_NAME_EN,
    CODE_INVALID_SETTLEMENT_NAME_HE,
    CODE_INVALID_TARGET_STATUS,
    CODE_INVALID_TECHNICIAN_NAME,
    CODE_INVALID_TICKET_NUMBER,
    CODE_INVALID_TRANSITION,
    CODE_LAST_ACTIVE_CATEGORY,
    CODE_PART_NOT_FOUND,
    CODE_PART_REQUIRED,
    CODE_PHONE_ALREADY_REGISTERED,
    CODE_RESOLUTION_REQUIRED,
    CODE_SETTLEMENT_NOT_FOUND,
    CODE_TICKET_NOT_FOUND,
    CODE_TICKET_STATUS_CHANGED,
    CODE_UNKNOWN_FIELD,
)
from app.service_tickets.grammars import (
    GrammarError,
    InvalidFilter,
    parse_date_range,
    parse_page,
    parse_part_filter,
    parse_status_filter,
    parse_ticket_number_path,
    parse_ticket_search,
    parse_uuid_path,
)
from app.service_tickets.models import (
    Category,
    Customer,
    FieldLabel,
    HistoryEntry,
    Locality,
    Part,
    ServiceSettings,
)
from app.service_tickets.photo_store import PhotoStore
from app.service_tickets.schemas import (
    AssignedTechnicianRequest,
    BrandingInfo,
    CategoryCount,
    CategoryCreateRequest,
    CategoryDeleteResult,
    CategoryList,
    CategoryRecord,
    CategoryRef,
    CategoryUpdateRequest,
    CustomerCreateRequest,
    CustomerRecord,
    CustomerRecordPage,
    CustomerUpdateRequest,
    FormConfig,
    FormSettings,
    HistoryEntryOut,
    LabelsResponse,
    LabelTexts,
    LocalityCreateRequest,
    LocalityDeleteResult,
    LocalityList,
    LocalityRecord,
    LocalityRef,
    LocalityUpdateRequest,
    NumberWarning,
    PartCreateRequest,
    PartDeleteResult,
    PartList,
    PartRecord,
    PartRef,
    PartUpdateRequest,
    ReportResponse,
    ReportTicketPage,
    ReportTicketRow,
    ServiceDashboardResponse,
    SettingsResponse,
    SettingsUpdateRequest,
    StaffTicketDetail,
    StaffTicketPage,
    StaffTicketRow,
    StatusChangeRequest,
    StatusCounts,
)
from app.service_tickets.services import config_admin as config_service
from app.service_tickets.services import customers as customer_service
from app.service_tickets.services import dashboard as dashboard_service
from app.service_tickets.services import history as history_service
from app.service_tickets.services import lifecycle, report_export, ticket_queries
from app.service_tickets.services import reports as reports_service
from app.service_tickets.services.config_admin import (
    ConfigError,
    ConfigValidationError,
    DuplicateName,
)
from app.service_tickets.services.customers import (
    CustomerError,
    CustomerValidationError,
    CustomerView,
)
from app.service_tickets.services.history import HistoryValidationError
from app.service_tickets.services.lifecycle import LifecycleError, UnexpectedResolution
from app.service_tickets.services.numbering import Allocator
from app.service_tickets.services.report_export import ExportTooLarge
from app.service_tickets.services.reports import ReportTicketRow as ReportRowView
from app.service_tickets.services.resolution import Resolution, ResolutionValidationError
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

#: A customer id that no customer has (the nil UUID), for a malformed customer id in the path.
_NO_CUSTOMER = uuid.UUID(int=0)

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
    # resolution of a ticket (R29.1, R29.3)
    CODE_RESOLUTION_REQUIRED: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_PART_REQUIRED: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_PART: HTTPStatus.UNPROCESSABLE_ENTITY,
    # customers
    CODE_CUSTOMER_NOT_FOUND: HTTPStatus.NOT_FOUND,
    CODE_PHONE_ALREADY_REGISTERED: HTTPStatus.CONFLICT,
    CODE_INVALID_PHONE: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_FULL_NAME: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_CITY: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_APARTMENT: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_SETTLEMENT: HTTPStatus.UNPROCESSABLE_ENTITY,  # R30.6, under `fields.city`
    # list and report query parameters
    CODE_INVALID_PAGE: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_TICKET_NUMBER: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_DATE_RANGE: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_FILTER: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_EXPORT_TOO_LARGE: HTTPStatus.UNPROCESSABLE_ENTITY,  # R31.4
    # configuration
    CODE_CATEGORY_NOT_FOUND: HTTPStatus.NOT_FOUND,
    CODE_DUPLICATE_CATEGORY: HTTPStatus.CONFLICT,
    CODE_LAST_ACTIVE_CATEGORY: HTTPStatus.CONFLICT,
    CODE_INVALID_NAME_HE: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_NAME_EN: HTTPStatus.UNPROCESSABLE_ENTITY,
    # settlements and replaceable parts (R30.1, R29.6)
    CODE_SETTLEMENT_NOT_FOUND: HTTPStatus.NOT_FOUND,
    CODE_DUPLICATE_SETTLEMENT: HTTPStatus.CONFLICT,
    CODE_INVALID_SETTLEMENT_NAME_HE: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_SETTLEMENT_NAME_EN: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_PART_NOT_FOUND: HTTPStatus.NOT_FOUND,
    CODE_DUPLICATE_PART: HTTPStatus.CONFLICT,
    CODE_INVALID_PART_NAME_HE: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_PART_NAME_EN: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_LABEL_HE: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_LABEL_EN: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_UNKNOWN_FIELD: HTTPStatus.UNPROCESSABLE_ENTITY,
}


# --------------------------------------------------------------------------------- helpers


def _http_error(error: Exception) -> Exception:
    """The `HTTPException` for a domain error, or `error` itself when it is not a listed refusal."""
    code = getattr(error, "code", None)
    status = _ERROR_STATUS.get(code) if isinstance(code, str) else None
    if status is None and isinstance(error, ResolutionValidationError):
        status = HTTPStatus.UNPROCESSABLE_ENTITY  # `invalid_request`, a `parts_replaced` that is no boolean
    if status is None:
        return error
    fields: dict[str, str] | None = None
    if isinstance(error, HistoryValidationError | CustomerValidationError | ConfigValidationError):
        fields = error.fields
    elif isinstance(error, DuplicateName):
        fields = {name: code for name in error.fields}  # the clashing languages
    elif isinstance(error, UnexpectedResolution):
        fields = {"resolution": code}
    return service_error(status, code, fields=fields)


@contextmanager
def _domain_errors(session: Session) -> Iterator[None]:
    """Answer a service or parser refusal with the package's envelope, after a rollback."""
    try:
        yield
    except (
        LifecycleError,
        GrammarError,
        HistoryValidationError,
        CustomerError,
        ConfigError,
        ExportTooLarge,
    ) as error:
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


def _locality_filter(raw: str | None) -> uuid.UUID | None:
    """The settlement filter (R30.7): absent or blank is no filter; text that is not a UUID is
    `invalid_filter`. An id that names no settlement is `invalid_filter` in the query service."""
    if raw is None or not raw.strip():
        return None
    locality_id = parse_uuid_path(raw.strip())
    if locality_id is None:
        raise InvalidFilter
    return locality_id


# --- result dataclass -> response model


def _locality_out(locality: Any) -> LocalityRef | None:
    """The `locality` object of a ticket, row or customer view (any value with `id`, `name_he`,
    `name_en`), or `None`."""
    if locality is None:
        return None
    return LocalityRef(id=locality.id, name_he=locality.name_he, name_en=locality.name_en)


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
        locality=_locality_out(row.locality),
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
        opened_by_username=view.opened_by_username,
        locality=_locality_out(view.locality),
        parts_replaced=[
            PartRef(id=part.id, name_he=part.name_he, name_en=part.name_en) for part in view.parts_replaced
        ],
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


@router.get("/dashboard", response_model=ServiceDashboardResponse)
def get_dashboard(session: ServiceSession, principal: StaffPrincipalDep) -> ServiceDashboardResponse:
    """Three counts from one read; the ticket-numbers warning only for configuration staff (R17.1, R10.9)."""
    with _domain_errors(session):
        counts = dashboard_service.get_dashboard(session, config_staff=principal.role in CONFIG_STAFF_ROLES)
    warning = (
        NumberWarning(remaining=counts.ticket_numbers_remaining)
        if counts.ticket_numbers_remaining is not None
        else None
    )
    return ServiceDashboardResponse(
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
    locality_id: Annotated[str | None, Query()] = None,
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
            locality_id=_locality_filter(locality_id),
            urgent_only=_flag(urgent_only),
            date_from=start,
            date_to=end,
            urgent_first=_flag(urgent_first),
            page=page_number,
        )
    return _staff_page_out(result)


#: The one form part of a staff submission that the customer form does not have (R26.4).
FIELD_CUSTOMER_ID = "customer_id"


@router.get("/form-config", response_model=FormConfig)
def staff_form_config(session: ServiceSession) -> FormConfig:
    """The ticket form for staff: the same body as the customer `GET /form-config` (R26.2)."""
    return form_config_body(session)


def _require_active_customer(session: Session, customer_id: uuid.UUID) -> None:
    """404 `customer_not_found` for an id with no row, 409 `customer_inactive` for an inactive customer.

    One primary-key read of the state column only: no hash is selected and the row is not changed (R26.4).
    """
    is_active = session.execute(
        select(Customer.is_active).where(Customer.id == customer_id)
    ).scalar_one_or_none()
    if is_active is None:
        raise service_error(HTTPStatus.NOT_FOUND, CODE_CUSTOMER_NOT_FOUND)
    if not is_active:
        raise service_error(HTTPStatus.CONFLICT, CODE_CUSTOMER_INACTIVE)


def _open_ticket(
    session: Session,
    customer_id: uuid.UUID,
    values: dict[str, object],
    files: FileParts,
    allocator: Allocator | None,
    store: PhotoStore,
    opened_by_username: str,
) -> StaffTicketDetail:
    """Validate, store and commit in one transaction, then read the new ticket's staff detail."""
    number = _store_ticket(session, customer_id, values, files, allocator, store, opened_by_username)
    return _ticket_detail(session, number)


@router.post("/tickets", response_model=StaffTicketDetail, status_code=HTTPStatus.CREATED)
async def open_ticket_for_customer(
    request: Request,
    principal: StaffPrincipalDep,
    session: ServiceSession,
    allocator: TicketAllocatorDep,
    store: PhotoStoreDep,
) -> StaffTicketDetail:
    """Open a ticket on behalf of a customer, from a `multipart/form-data` form (R26.3 to R26.5, R26.8).

    The form is the customer's submission plus `customer_id`. The handler reads the form itself, like
    the customer handler, and checks in this order (design.md, "Staff-opened tickets"):

    1. `customer_id` missing, repeated or not a UUID: 422 `customer_not_found` with
       `fields.customer_id`; a well-formed id with no row: 404 `customer_not_found`; an inactive
       customer: 409 `customer_inactive`. Nothing is stored in any of these cases.
    2. A file part over 10 MiB is 413 `photo_too_large`; then the same validation and photo pipeline as
       the customer path (422 with `fields`, `unknown_field` for any other part).
    3. Ticket number, photo store and insert in one transaction, with `opened_by_username` set to the
       acting staff username. That value comes from the staff principal and never from the request.

    The customer submission window is neither read nor taken: the request limits of Requirement 22 apply
    to customer tokens only. The body guard (`BodyLimitMiddleware`) covers this path as well.
    """
    try:
        form = await request.form()
    except (StarletteHTTPException, FormParserError) as error:  # a malformed or oversized body
        raise service_error(
            HTTPStatus.UNPROCESSABLE_ENTITY, fields={"body": CODE_INVALID_REQUEST}
        ) from error
    try:
        values = _submission_values(form)
        raw_customer_id = values.pop(FIELD_CUSTOMER_ID, None)
        customer_id = parse_uuid_path(raw_customer_id) if isinstance(raw_customer_id, str) else None
        if customer_id is None:
            raise service_error(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                CODE_CUSTOMER_NOT_FOUND,
                fields={FIELD_CUSTOMER_ID: CODE_CUSTOMER_NOT_FOUND},
            )
        await run_in_threadpool(_require_active_customer, session, customer_id)
        files = await _read_file_parts(form)
        return await run_in_threadpool(
            _open_ticket, session, customer_id, values, files, allocator, store, principal.username
        )
    finally:
        await form.close()


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
    (`lifecycle.change_status`, R14.6). A change to `closed` carries a `resolution` (history text,
    technician name, replaced parts); the status, the history entry and the parts are stored in this one
    transaction or not at all (R29.1 to R29.3). Any other target refuses a resolution.
    """
    number = _ticket_number_in_path(ticket_number)
    resolution = (
        None
        if body.resolution is None
        else Resolution(
            entry_text=body.resolution.entry_text,
            technician_name=body.resolution.technician_name,
            parts_replaced=body.resolution.parts_replaced,
            part_ids=body.resolution.part_ids,
        )
    )
    with _domain_errors(session):
        lifecycle.change_status(
            session,
            number,
            body.expected_status,
            body.target_status,
            principal,
            body.assigned_technician_name,
            resolution,
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
# Task 13.5: GET/POST /customers, PUT /customers/{id}, POST /customers/{id}/deactivate,
# /reactivate, /reset-password (204), GET /reports. All ticket staff may use them (R1.9), so they sit
# behind the router-level `staff_principal` only.
#
# No response here carries a password hash, the initial password or the token version: `CustomerRecord`
# declares none of them and the services return `CustomerView` values that have no such field (R3.12).
# Nothing in this section logs: a phone number must never reach a log line.


def _customer_id_in_path(raw: str) -> uuid.UUID:
    """The customer id of a path segment. A malformed one becomes the nil UUID, which no customer has,
    so it is answered exactly like an unknown id: 404 `customer_not_found` (R3.14)."""
    customer_id = parse_uuid_path(raw)
    return _NO_CUSTOMER if customer_id is None else customer_id


def _active_filter(raw: str | None) -> bool | None:
    """The customers state filter: absent, blank or `all` is every customer, `true` active only,
    `false` inactive only (R3.10). Anything else is `invalid_filter`."""
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in ("", "all"):
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    raise InvalidFilter


def _customer_out(view: CustomerView) -> CustomerRecord:
    return CustomerRecord(
        id=view.id,
        full_name=view.full_name,
        phone=view.phone,
        city=view.city,
        apartment=view.apartment,
        is_active=view.is_active,
        locality=_locality_out(view.locality),
    )


@router.get("/customers", response_model=CustomerRecordPage)
def list_customers(
    session: ServiceSession,
    page: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
    active: Annotated[str | None, Query()] = None,
) -> CustomerRecordPage:
    """One page (50) of customers by name, filtered by search text and state (R3.10)."""
    with _domain_errors(session):
        page_number = parse_page(page)
        state = _active_filter(active)
        result = customer_service.list_customers(
            session,
            search=search if search is not None and search.strip() else None,
            active=state,
            page=page_number,
        )
    return CustomerRecordPage(
        items=[_customer_out(view) for view in result.items],
        page=result.page,
        page_size=result.page_size,
        total=result.total,
    )


@router.post("/customers", response_model=CustomerRecord, status_code=HTTPStatus.CREATED)
def create_customer(body: CustomerCreateRequest, session: ServiceSession) -> CustomerRecord:
    """Add a customer with the initial password and the must-change flag set (R3.2)."""
    with _domain_errors(session):
        view = customer_service.create_customer(
            session,
            phone=body.phone,
            full_name=body.full_name,
            city=body.city,
            apartment=body.apartment,
            locality_id=body.locality_id,
        )
        out = _customer_out(view)
    session.commit()
    return out


@router.put("/customers/{customer_id}", response_model=CustomerRecord)
def update_customer(
    customer_id: str, body: CustomerUpdateRequest, session: ServiceSession
) -> CustomerRecord:
    """Replace name, phone, city and apartment; outstanding tokens stay valid (R3.6, R3.13).

    The optional `locality_id` (R30.6) is told apart by whether the key was sent: absent keeps the stored
    settlement (while the city text is unchanged), a settlement id sets it, `null` clears it and, when
    `city` is not sent either, keeps the stored text. See `customer_service.update_customer`.
    """
    sent = body.model_fields_set
    with _domain_errors(session):
        view = customer_service.update_customer(
            session,
            _customer_id_in_path(customer_id),
            phone=body.phone,
            full_name=body.full_name,
            city=body.city if "city" in sent or "locality_id" not in sent else customer_service.UNSET,
            apartment=body.apartment,
            locality_id=body.locality_id if "locality_id" in sent else customer_service.UNSET,
        )
        out = _customer_out(view)
    session.commit()
    return out


@router.post("/customers/{customer_id}/deactivate", response_model=CustomerRecord)
def deactivate_customer(customer_id: str, session: ServiceSession) -> CustomerRecord:
    """Set the customer inactive and revoke every token issued so far (R3.7)."""
    with _domain_errors(session):
        out = _customer_out(
            customer_service.deactivate_customer(session, _customer_id_in_path(customer_id))
        )
    session.commit()
    return out


@router.post("/customers/{customer_id}/reactivate", response_model=CustomerRecord)
def reactivate_customer(customer_id: str, session: ServiceSession) -> CustomerRecord:
    """Set the customer active; password and flags stay as they were (R3.8)."""
    with _domain_errors(session):
        out = _customer_out(
            customer_service.reactivate_customer(session, _customer_id_in_path(customer_id))
        )
    session.commit()
    return out


@router.post(
    "/customers/{customer_id}/reset-password",
    status_code=HTTPStatus.NO_CONTENT,
    response_class=Response,
)
def reset_customer_password(customer_id: str, session: ServiceSession) -> Response:
    """Give the customer the initial password again and clear the sign-in lock. No body (R3.9, R3.12)."""
    with _domain_errors(session):
        customer_service.reset_password(session, _customer_id_in_path(customer_id))
    session.commit()
    return Response(status_code=HTTPStatus.NO_CONTENT)


def _report_row_out(row: ReportRowView) -> ReportTicketRow:
    """A report table row: the staff list row plus the replaced parts (R29.7)."""
    return ReportTicketRow(
        ticket_number=row.ticket_number,
        created_at=row.created_at,
        customer_name=row.customer_name,
        city=row.city,
        apartment=row.apartment,
        category=CategoryRef(id=row.category.id, name_he=row.category.name_he, name_en=row.category.name_en),
        status=row.status,  # type: ignore[arg-type]  # the column check constraint holds the four values
        assigned_technician_name=row.assigned_technician_name,
        urgent=row.urgent,
        locality=_locality_out(row.locality),
        parts=[PartRef(id=part.id, name_he=part.name_he, name_en=part.name_en) for part in row.parts],
    )


@router.get("/reports", response_model=ReportResponse)
def get_report(
    session: ServiceSession,
    page: Annotated[str | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    city: Annotated[str | None, Query()] = None,
    category_id: Annotated[str | None, Query()] = None,
    locality_id: Annotated[str | None, Query()] = None,
    part_filter: Annotated[str | None, Query()] = None,
) -> ReportResponse:
    """Counts per status and per category, and a page (25) of the matching tickets (R18.1 to R18.9).

    `part_filter` (R29.8) is `any` (default), `yes`, `no` or a part id; the counts follow the filtered set.
    """
    with _domain_errors(session):
        page_number = parse_page(page)
        start, end = parse_date_range(date_from, date_to)
        report = reports_service.get_report(
            session,
            date_from=start,
            date_to=end,
            city=city,
            category_id=_category_filter(category_id),
            locality_id=_locality_filter(locality_id),
            part_filter=parse_part_filter(part_filter),
            page=page_number,
        )
    return ReportResponse(
        total=report.total,
        open=report.open,
        by_status=StatusCounts(**report.by_status),
        by_category=[
            CategoryCount(
                category=CategoryRef(
                    id=item.category.id, name_he=item.category.name_he, name_en=item.category.name_en
                ),
                count=item.count,
            )
            for item in report.by_category
        ],
        tickets=ReportTicketPage(
            items=[_report_row_out(row) for row in report.tickets],
            page=report.page,
            page_size=report.page_size,
            total=report.total,
        ),
    )


@router.get(
    "/reports/export",
    response_class=Response,
    responses={200: {"content": {report_export.XLSX_CONTENT_TYPE: {}}, "description": "The .xlsx workbook"}},
)
def export_report(
    session: ServiceSession,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    city: Annotated[str | None, Query()] = None,
    category_id: Annotated[str | None, Query()] = None,
    locality_id: Annotated[str | None, Query()] = None,
    part_filter: Annotated[str | None, Query()] = None,
    lang: Annotated[str | None, Query()] = None,
) -> Response:
    """The Excel workbook of every ticket the report filters match, not only a page (R31.1 to R31.5).

    The filters are the report's, parsed and checked by the same code, so an invalid one is answered the
    same way (422 `invalid_filter`, `invalid_date_range`). Like the report, an unknown query parameter is
    ignored. More than 10,000 matches is 422 `export_too_large` and no file. The file name holds only the
    Asia/Jerusalem date. `nosniff` comes from `SecurityHeadersMiddleware`, as for the photo.
    """
    with _domain_errors(session):
        start, end = parse_date_range(date_from, date_to)
        language = report_export.parse_lang(lang)
        conditions = reports_service.matching_conditions(
            session,
            date_from=start,
            date_to=end,
            city=city,
            category_id=_category_filter(category_id),
            locality_id=_locality_filter(locality_id),
            part_filter=parse_part_filter(part_filter),
        )
        content = report_export.build_workbook(session, conditions, language)
    return Response(
        content=content,
        media_type=report_export.XLSX_CONTENT_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{report_export.export_filename()}"',
            "Cache-Control": "no-store",
        },
    )


# --------------------------------------------------------------------------------- configuration
# Task 13.6. `GET /categories` is open to all ticket staff, because the ticket list and report
# filters must offer inactive categories too (R19.9, R1.9); it sits behind the router-level
# `staff_principal` only. Every other route here declares `config_staff_principal`, so a technician
# gets 403 `insufficient_role` on all of them, reads included, before the body or the path is looked
# at, and nothing is changed (R1.10, R1.11, R19.1, R19.16). That dependency reuses the cached
# `staff_principal` result, so the user row is still read once.
#
# Texts are validated by the service, not by the request models, so a request with several bad texts
# reports all of them in `fields` and stores nothing in either language (R19.5).

#: A category id that no category has (the nil UUID), for a malformed category id in the path.
_NO_CATEGORY = uuid.UUID(int=0)


def _category_id_in_path(raw: str) -> uuid.UUID:
    """The category id of a path segment. A malformed one becomes the nil UUID, which no category has,
    so it is answered exactly like an unknown id: 404 `category_not_found`."""
    category_id = parse_uuid_path(raw)
    return _NO_CATEGORY if category_id is None else category_id


def _category_record(category: Category) -> CategoryRecord:
    return CategoryRecord.model_validate(category)


def _settings_out(row: ServiceSettings) -> SettingsResponse:
    return SettingsResponse(
        settings=FormSettings(
            description_required=row.description_required,
            photo_upload_enabled=row.photo_upload_enabled,
        )
    )


def _labels_out(rows: dict[str, FieldLabel]) -> LabelsResponse:
    return LabelsResponse(
        labels={key: LabelTexts(he=row.label_he, en=row.label_en) for key, row in rows.items()}  # type: ignore[misc]
    )


class _LabelTextsBody(BaseModel):
    """The two texts of one label, lenient on purpose: a missing or non-string text reaches the service
    and gets `invalid_label_he` or `invalid_label_en` there (with every other failing text, R19.5)."""

    model_config = ConfigDict(extra="forbid")

    he: Any = None
    en: Any = None


class _LabelsBody(BaseModel):
    """Body of `PUT /staff/labels`. Keys are plain strings so that an unknown field key reaches the
    service and is reported as `unknown_field` next to any other failing text, instead of being a
    generic validation error. Same JSON shape as `schemas.LabelsUpdateRequest`."""

    model_config = ConfigDict(extra="forbid")

    labels: dict[str, _LabelTextsBody]


@router.get("/categories", response_model=CategoryList)
def list_categories(session: ServiceSession) -> CategoryList:
    """Every category, active and inactive, oldest first (R1.9, R19.9)."""
    with _domain_errors(session):
        rows = config_service.list_categories(session)
    return CategoryList(items=[_category_record(row) for row in rows])


@router.post(
    "/categories",
    response_model=CategoryRecord,
    status_code=HTTPStatus.CREATED,
    dependencies=[Depends(config_staff_principal)],
)
def create_category(body: CategoryCreateRequest, session: ServiceSession) -> CategoryRecord:
    """Add an active category (R19.6)."""
    with _domain_errors(session):
        out = _category_record(config_service.create_category(session, body.name_he, body.name_en))
    session.commit()
    return out


@router.put(
    "/categories/{category_id}",
    response_model=CategoryRecord,
    dependencies=[Depends(config_staff_principal)],
)
def rename_category(category_id: str, body: CategoryUpdateRequest, session: ServiceSession) -> CategoryRecord:
    """Replace both names of a category (R19.5, R19.7, R19.8)."""
    with _domain_errors(session):
        category = config_service.rename_category(
            session, _category_id_in_path(category_id), body.name_he, body.name_en
        )
        out = _category_record(category)
    session.commit()
    return out


@router.post(
    "/categories/{category_id}/deactivate",
    response_model=CategoryRecord,
    dependencies=[Depends(config_staff_principal)],
)
def deactivate_category(category_id: str, session: ServiceSession) -> CategoryRecord:
    """Hide a category from the ticket form; refused for the last active one (R19.9, R19.13)."""
    with _domain_errors(session):
        out = _category_record(config_service.deactivate_category(session, _category_id_in_path(category_id)))
    session.commit()
    return out


@router.post(
    "/categories/{category_id}/reactivate",
    response_model=CategoryRecord,
    dependencies=[Depends(config_staff_principal)],
)
def reactivate_category(category_id: str, session: ServiceSession) -> CategoryRecord:
    """List a category in the ticket form again (R19.10)."""
    with _domain_errors(session):
        out = _category_record(config_service.reactivate_category(session, _category_id_in_path(category_id)))
    session.commit()
    return out


@router.delete(
    "/categories/{category_id}",
    response_model=CategoryDeleteResult,
    dependencies=[Depends(config_staff_principal)],
)
def delete_category(category_id: str, session: ServiceSession) -> CategoryDeleteResult:
    """Remove an unused category (`removed`) or deactivate a used one (`deactivated`) (R19.11 to R19.13)."""
    with _domain_errors(session):
        outcome = config_service.delete_category(session, _category_id_in_path(category_id))
    session.commit()
    return CategoryDeleteResult(result=outcome)  # type: ignore[arg-type]  # REMOVED or DEACTIVATED


@router.get("/settings", response_model=SettingsResponse, dependencies=[Depends(config_staff_principal)])
def get_settings(session: ServiceSession) -> SettingsResponse:
    """The two form options. Configuration staff only: a technician gets 403 (R19.16)."""
    with _domain_errors(session):
        return _settings_out(config_service.get_service_settings(session))


@router.put("/settings", response_model=SettingsResponse, dependencies=[Depends(config_staff_principal)])
def update_settings(body: SettingsUpdateRequest, session: ServiceSession) -> SettingsResponse:
    """Set both form options; existing tickets are untouched (R19.2, R19.3)."""
    with _domain_errors(session):
        row = config_service.update_service_settings(
            session,
            description_required=body.description_required,
            photo_upload_enabled=body.photo_upload_enabled,
        )
        out = _settings_out(row)
    session.commit()
    return out


@router.put("/labels", response_model=LabelsResponse, dependencies=[Depends(config_staff_principal)])
def update_labels(body: _LabelsBody, session: ServiceSession) -> LabelsResponse:
    """Replace the labels named in the body; the others keep theirs (R19.4, R19.5)."""
    with _domain_errors(session):
        rows = config_service.update_labels(
            session, {key: (texts.he, texts.en) for key, texts in body.labels.items()}
        )
        out = _labels_out(rows)
    session.commit()
    return out


# --------------------------------------------------------------------------------- branding
# Task 30.5 (Requirement 28). The delivery of the logo, `GET /branding/logo`, is in `photo_router`
# (it is open to customers too). The form and the write order are in `branding_form`.


@router.get("/branding", response_model=BrandingInfo, dependencies=[Depends(config_staff_principal)])
def get_branding(session: ServiceSession) -> BrandingInfo:
    """The company name and whether a logo is stored. Configuration staff only (R28.1)."""
    with _domain_errors(session):
        branding = config_service.get_form_branding(session)
    return BrandingInfo(company_name=branding.company_name, has_logo=branding.has_logo)


@router.put("/branding", response_model=BrandingInfo, dependencies=[Depends(config_staff_principal)])
async def update_branding(
    request: Request, session: ServiceSession, store: PhotoStoreDep
) -> BrandingInfo:
    """Set, replace or remove the company name and the logo, from a `multipart/form-data` form (R28.1, R28.2).

    The handler reads the form itself, like the ticket handlers, so every refusal has the package's code.
    Parts: `company_name`, `logo` (file), `remove_logo=true`, `remove_name=true`; see `branding_form` for
    the rules, the check order and the write order. The body guard limits the request to 2 MiB; a logo over
    1 MiB is 413 `logo_too_large`. Nothing is stored unless every part is valid. The answer is the new
    `{company_name, has_logo}`.
    """
    try:
        form = await request.form()
    except (StarletteHTTPException, FormParserError) as error:  # a malformed or oversized body
        raise service_error(
            HTTPStatus.UNPROCESSABLE_ENTITY, fields={"body": CODE_INVALID_REQUEST}
        ) from error
    try:
        entries = await branding_form.read_entries(form)
        change = await run_in_threadpool(branding_form.plan_branding, entries)
        return await run_in_threadpool(branding_form.save_branding, session, store, change)
    finally:
        await form.close()


# --------------------------------------------------------------------------------- settlements, parts
# Task 30.2 (Requirements 30.1 and 29.6). The two lists work as the categories do and share the service
# functions of `config_admin` (`create_named`, `rename_named`, ...), which take the list as an argument.
# Differences from categories: every route here, `GET` included, is configuration staff only (the form
# configuration is what exposes the active ones to all staff), and there is no "last active" rule.
# A malformed id in the path is the nil UUID, which no row has, so it is answered as a missing item.

#: An item id that no settlement or part has (the nil UUID), for a malformed id in the path.
_NO_ITEM = uuid.UUID(int=0)


def _item_id_in_path(raw: str) -> uuid.UUID:
    item_id = parse_uuid_path(raw)
    return _NO_ITEM if item_id is None else item_id


def _locality_record(row: Locality) -> LocalityRecord:
    return LocalityRecord.model_validate(row)


def _part_record(row: Part) -> PartRecord:
    return PartRecord.model_validate(row)


@router.get("/localities", response_model=LocalityList, dependencies=[Depends(config_staff_principal)])
def list_localities(session: ServiceSession) -> LocalityList:
    """Every settlement, active and inactive, oldest first (R30.1)."""
    with _domain_errors(session):
        rows = config_service.list_names(session, config_service.LOCALITIES)
    return LocalityList(items=[_locality_record(row) for row in rows])  # type: ignore[arg-type]


@router.post(
    "/localities",
    response_model=LocalityRecord,
    status_code=HTTPStatus.CREATED,
    dependencies=[Depends(config_staff_principal)],
)
def create_locality(body: LocalityCreateRequest, session: ServiceSession) -> LocalityRecord:
    """Add an active settlement (R30.1)."""
    with _domain_errors(session):
        row = config_service.create_named(session, config_service.LOCALITIES, body.name_he, body.name_en)
        out = _locality_record(row)  # type: ignore[arg-type]
    session.commit()
    return out


@router.put(
    "/localities/{locality_id}",
    response_model=LocalityRecord,
    dependencies=[Depends(config_staff_principal)],
)
def rename_locality(locality_id: str, body: LocalityUpdateRequest, session: ServiceSession) -> LocalityRecord:
    """Replace both names of a settlement (R30.1)."""
    with _domain_errors(session):
        row = config_service.rename_named(
            session, config_service.LOCALITIES, _item_id_in_path(locality_id), body.name_he, body.name_en
        )
        out = _locality_record(row)  # type: ignore[arg-type]
    session.commit()
    return out


@router.post(
    "/localities/{locality_id}/deactivate",
    response_model=LocalityRecord,
    dependencies=[Depends(config_staff_principal)],
)
def deactivate_locality(locality_id: str, session: ServiceSession) -> LocalityRecord:
    """Hide a settlement from the forms. Any settlement may be deactivated, the last one included."""
    with _domain_errors(session):
        row = config_service.deactivate_named(
            session, config_service.LOCALITIES, _item_id_in_path(locality_id)
        )
        out = _locality_record(row)  # type: ignore[arg-type]
    session.commit()
    return out


@router.post(
    "/localities/{locality_id}/reactivate",
    response_model=LocalityRecord,
    dependencies=[Depends(config_staff_principal)],
)
def reactivate_locality(locality_id: str, session: ServiceSession) -> LocalityRecord:
    """List a settlement in the forms again."""
    with _domain_errors(session):
        row = config_service.reactivate_named(
            session, config_service.LOCALITIES, _item_id_in_path(locality_id)
        )
        out = _locality_record(row)  # type: ignore[arg-type]
    session.commit()
    return out


@router.delete(
    "/localities/{locality_id}",
    response_model=LocalityDeleteResult,
    dependencies=[Depends(config_staff_principal)],
)
def delete_locality(locality_id: str, session: ServiceSession) -> LocalityDeleteResult:
    """Remove a settlement no ticket or customer uses (`removed`), else deactivate it (`deactivated`)."""
    with _domain_errors(session):
        outcome = config_service.delete_named(
            session, config_service.LOCALITIES, _item_id_in_path(locality_id)
        )
    session.commit()
    return LocalityDeleteResult(result=outcome)  # type: ignore[arg-type]  # REMOVED or DEACTIVATED


@router.get("/parts", response_model=PartList, dependencies=[Depends(config_staff_principal)])
def list_parts(session: ServiceSession) -> PartList:
    """Every replaceable part, active and inactive, oldest first (R29.6)."""
    with _domain_errors(session):
        rows = config_service.list_names(session, config_service.PARTS)
    return PartList(items=[_part_record(row) for row in rows])  # type: ignore[arg-type]


@router.post(
    "/parts",
    response_model=PartRecord,
    status_code=HTTPStatus.CREATED,
    dependencies=[Depends(config_staff_principal)],
)
def create_part(body: PartCreateRequest, session: ServiceSession) -> PartRecord:
    """Add an active part (R29.6)."""
    with _domain_errors(session):
        row = config_service.create_named(session, config_service.PARTS, body.name_he, body.name_en)
        out = _part_record(row)  # type: ignore[arg-type]
    session.commit()
    return out


@router.put(
    "/parts/{part_id}",
    response_model=PartRecord,
    dependencies=[Depends(config_staff_principal)],
)
def rename_part(part_id: str, body: PartUpdateRequest, session: ServiceSession) -> PartRecord:
    """Replace both names of a part (R29.6)."""
    with _domain_errors(session):
        row = config_service.rename_named(
            session, config_service.PARTS, _item_id_in_path(part_id), body.name_he, body.name_en
        )
        out = _part_record(row)  # type: ignore[arg-type]
    session.commit()
    return out


@router.post(
    "/parts/{part_id}/deactivate",
    response_model=PartRecord,
    dependencies=[Depends(config_staff_principal)],
)
def deactivate_part(part_id: str, session: ServiceSession) -> PartRecord:
    """Hide a part from the Resolve form. Any part may be deactivated, the last one included."""
    with _domain_errors(session):
        row = config_service.deactivate_named(session, config_service.PARTS, _item_id_in_path(part_id))
        out = _part_record(row)  # type: ignore[arg-type]
    session.commit()
    return out


@router.post(
    "/parts/{part_id}/reactivate",
    response_model=PartRecord,
    dependencies=[Depends(config_staff_principal)],
)
def reactivate_part(part_id: str, session: ServiceSession) -> PartRecord:
    """List a part in the Resolve form again."""
    with _domain_errors(session):
        row = config_service.reactivate_named(session, config_service.PARTS, _item_id_in_path(part_id))
        out = _part_record(row)  # type: ignore[arg-type]
    session.commit()
    return out


@router.delete(
    "/parts/{part_id}",
    response_model=PartDeleteResult,
    dependencies=[Depends(config_staff_principal)],
)
def delete_part(part_id: str, session: ServiceSession) -> PartDeleteResult:
    """Remove a part no ticket uses (`removed`), else deactivate it (`deactivated`)."""
    with _domain_errors(session):
        outcome = config_service.delete_named(session, config_service.PARTS, _item_id_in_path(part_id))
    session.commit()
    return PartDeleteResult(result=outcome)  # type: ignore[arg-type]  # REMOVED or DEACTIVATED
