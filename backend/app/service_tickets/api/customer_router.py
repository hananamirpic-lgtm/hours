"""Customer endpoints of the service ticket API (design.md, "API surface"; Requirements 5, 6, 7.11).

One `APIRouter` with `route_class=ServiceRoute`, mounted under `{api_prefix}/service` by task 13.7. It
holds the customer authentication endpoints (task 13.2) and the form and ticket endpoints (task 13.3);
the photo part of the submission (task 16.4) is marked by a hook in `_store_ticket`.

Transactions
------------
The router owns the transaction. `services.signin` never commits and returns every outcome a caller can
cause as a value, so **the router commits after every outcome**, `InvalidCredentials` and `Locked`
included. Rolling back on a credential failure would discard the `svc_signin_failures` row and make the
lock unreachable. A database failure raises out of the handler and `ServiceRoute` rolls back and answers
503.

Errors
------
Every error is built by `service_error`, the package envelope also used by `ServiceRoute`. The status of
each code is in `_ERROR_STATUS`. All sign-in credential failures (unknown phone, inactive customer, wrong,
empty, missing or malformed password) are the same value from the service and become the same bytes here.

Logging: this module logs nothing, and no phone number, password or hash is ever put in an exception
message or a response.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import SettingsDep
from app.service_tickets.api.deps import (
    CustomerPrincipal,
    CustomerPrincipalDep,
    CustomerPrincipalUngatedDep,
    ServiceSession,
)
from app.service_tickets.api.errors import service_error
from app.service_tickets.api.route import CODE_INVALID_REQUEST, ServiceRoute
from app.service_tickets.config import ServiceConfig
from app.service_tickets.error_codes import (
    CODE_INVALID_CREDENTIALS,
    CODE_INVALID_EXPECTED_STATUS,
    CODE_INVALID_PAGE,
    CODE_INVALID_PASSWORD_FORMAT,
    CODE_INVALID_PHONE,
    CODE_INVALID_TOKEN,
    CODE_INVALID_TRANSITION,
    CODE_PASSWORD_NOT_CHANGED,
    CODE_TICKET_NOT_FOUND,
    CODE_TICKET_NUMBERS_EXHAUSTED,
    CODE_TICKET_STATUS_CHANGED,
    CODE_TOO_MANY_ATTEMPTS,
)
from app.service_tickets.grammars import GrammarError, parse_page, parse_ticket_number_path
from app.service_tickets.models import Category, Customer
from app.service_tickets.schemas import (
    CancelTicketRequest,
    CategoryRef,
    CustomerProfile,
    CustomerTicketDetail,
    CustomerTicketPage,
    CustomerTicketRow,
    FormConfig,
    FormSettings,
    LabelTexts,
    TicketCreated,
    TokenResponse,
    customer_ticket_detail,
)
from app.service_tickets.services import config_admin, lifecycle, signin, ticket_queries
from app.service_tickets.services.lifecycle import CustomerActor
from app.service_tickets.services.numbering import Allocator, TicketNumbersExhausted
from app.service_tickets.services.tickets import (
    FIELD_PHOTO,
    SubmissionValidationError,
    create_ticket,
    validate_submission,
)

router = APIRouter(route_class=ServiceRoute, tags=["service-customer"])

#: HTTP status of each customer authentication error code (design.md, "Customer and shared endpoints").
_ERROR_STATUS: dict[str, HTTPStatus] = {
    CODE_INVALID_PHONE: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_CREDENTIALS: HTTPStatus.UNAUTHORIZED,
    CODE_TOO_MANY_ATTEMPTS: HTTPStatus.TOO_MANY_REQUESTS,
    CODE_INVALID_PASSWORD_FORMAT: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_PASSWORD_NOT_CHANGED: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_TOKEN: HTTPStatus.UNAUTHORIZED,
    # Tickets (task 13.3).
    CODE_INVALID_PAGE: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_TICKET_NOT_FOUND: HTTPStatus.NOT_FOUND,
    CODE_INVALID_EXPECTED_STATUS: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_TICKET_STATUS_CHANGED: HTTPStatus.CONFLICT,
    CODE_INVALID_TRANSITION: HTTPStatus.CONFLICT,
    CODE_TICKET_NUMBERS_EXHAUSTED: HTTPStatus.SERVICE_UNAVAILABLE,
}

#: The error code of each failure outcome of `services.signin` that carries no data.
_OUTCOME_CODES: dict[type, str] = {
    signin.InvalidPhoneInput: CODE_INVALID_PHONE,
    signin.InvalidCredentials: CODE_INVALID_CREDENTIALS,
    signin.InvalidPasswordFormat: CODE_INVALID_PASSWORD_FORMAT,
    signin.PasswordNotChanged: CODE_PASSWORD_NOT_CHANGED,
}


def get_service_config(settings: SettingsDep) -> ServiceConfig:
    """The typed service-ticket configuration. Tests override this dependency to pick their own."""
    return ServiceConfig.from_settings(settings)


ServiceConfigDep = Annotated[ServiceConfig, Depends(get_service_config)]


def _error(code: str, headers: dict[str, str] | None = None) -> HTTPException:
    return service_error(_ERROR_STATUS[code], code, headers=headers)


def _failure(outcome: object) -> HTTPException:
    """The error response for a failure outcome of `services.signin`.

    `Locked` answers 429 with `Retry-After` in whole seconds (the service already rounded up). Every
    other failure is its own fixed code, so the body does not depend on why the attempt failed.
    """
    if isinstance(outcome, signin.Locked):
        return _error(CODE_TOO_MANY_ATTEMPTS, headers={"Retry-After": str(outcome.retry_after)})
    return _error(_OUTCOME_CODES[type(outcome)])


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- request bodies


class SignInBody(BaseModel):
    """`POST /auth/sign-in`.

    Deliberately lenient about the values: a missing, empty, non-string or malformed password is a
    credential failure (401 `invalid_credentials`, R5.4), not a validation error, so it must reach the
    service, which counts it. A phone that is missing or not a string is `invalid_phone` (R5.12).
    An unexpected field is still `unknown_field`.
    """

    model_config = ConfigDict(extra="forbid")

    phone: Any = None
    password: Any = None


class ChangePasswordBody(BaseModel):
    """`POST /auth/change-password`.

    Lenient for the same reason: a missing or malformed current password is 401 `invalid_credentials`
    and is counted (R6.6), and a missing or malformed new password is 422 `invalid_password_format`
    (R6.4). The service decides, in the order of R6.4 then R6.6.
    """

    model_config = ConfigDict(extra="forbid")

    current_password: Any = None
    new_password: Any = None


def _token_response(outcome: signin.Success | signin.PasswordChanged) -> TokenResponse:
    return TokenResponse(
        access_token=outcome.access_token,
        expires_in=outcome.expires_in,
        must_change_password=outcome.must_change_password,
    )


# --------------------------------------------------------------------------- authentication (task 13.2)


@router.post("/auth/sign-in", response_model=TokenResponse)
def sign_in(body: SignInBody, session: ServiceSession, config: ServiceConfigDep) -> TokenResponse:
    """Sign a customer in. No token is read: a token sent with the request is ignored (R5.11)."""
    phone = body.phone if isinstance(body.phone, str) else None
    outcome = signin.sign_in(
        session, phone_raw=phone, password_raw=body.password, now=_now(), settings=config
    )
    # Commit whatever the outcome, so the failure row of a refused attempt survives the 401.
    session.commit()
    if isinstance(outcome, signin.Success):
        return _token_response(outcome)
    raise _failure(outcome)


@router.post("/auth/sign-out", status_code=HTTPStatus.NO_CONTENT)
def sign_out(principal: CustomerPrincipalUngatedDep, session: ServiceSession) -> Response:
    """Revoke every token issued to the customer, the presented one included (design: Revocation)."""
    session.execute(
        update(Customer)
        .where(Customer.id == principal.customer_id)
        .values(token_version=Customer.token_version + 1, updated_at=_now())
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.get("/auth/me", response_model=CustomerProfile)
def me(principal: CustomerPrincipalUngatedDep, session: ServiceSession) -> CustomerProfile:
    """The signed-in customer, with the current `must_change_password` (R6.8). Reads no hash."""
    profile = _load_profile(session, principal)
    if profile is None:  # deleted after the principal check; nothing deletes customers today
        raise _error(CODE_INVALID_TOKEN)
    return profile


def _load_profile(session: Session, principal: CustomerPrincipal) -> CustomerProfile | None:
    row = session.execute(
        select(Customer.full_name, Customer.city, Customer.apartment).where(
            Customer.id == principal.customer_id
        )
    ).one_or_none()
    if row is None:
        return None
    return CustomerProfile(
        full_name=row.full_name,
        phone=principal.phone,
        city=row.city,
        apartment=row.apartment,
        must_change_password=principal.must_change_password,
    )


@router.post("/auth/change-password", response_model=TokenResponse)
def change_password(
    body: ChangePasswordBody,
    principal: CustomerPrincipalUngatedDep,
    session: ServiceSession,
    config: ServiceConfigDep,
) -> TokenResponse:
    """Change the customer's own password. Works while `must_change_password` is set (R6.3)."""
    outcome = signin.change_password(
        session,
        customer_id=principal.customer_id,
        current_password_raw=body.current_password,
        new_password_raw=body.new_password,
        now=_now(),
        settings=config,
    )
    # Commit whatever the outcome: a wrong current password is a counted failure row (R6.6).
    session.commit()
    if isinstance(outcome, signin.PasswordChanged):
        return _token_response(outcome)
    raise _failure(outcome)


# --------------------------------------------------------------------------- tickets (task 13.3)
#
# Every endpoint here depends on `CustomerPrincipalDep`, the gated principal: while the customer owes a
# password change, each of them answers 403 `password_change_required` before its handler runs, so no
# data is read and nothing is changed (R6.2). The router owns the transaction: it commits on success
# and rolls back on a domain error. Reads never commit.


class _Owner:
    """What `create_ticket` reads from a customer: the id. The owner always comes from the token."""

    __slots__ = ("id",)

    def __init__(self, customer_id: uuid.UUID) -> None:
        self.id = customer_id


def get_ticket_allocator() -> Allocator | None:
    """The ticket-number allocator. `None` means `nextval('svc_ticket_number_seq')`.

    A dependency so that tests on SQLite, which has no sequences, can supply their own.
    """
    return None


TicketAllocatorDep = Annotated[Allocator | None, Depends(get_ticket_allocator)]


def _ticket_row(view: ticket_queries.CustomerTicketRowView) -> CustomerTicketRow:
    return CustomerTicketRow(
        ticket_number=view.ticket_number,
        category=CategoryRef(
            id=view.category.id, name_he=view.category.name_he, name_en=view.category.name_en
        ),
        created_date=view.created_date,
        status=view.status,  # type: ignore[arg-type]  # the column check constraint holds the four values
        urgent=view.urgent,
    )


def _ticket_detail(view: ticket_queries.CustomerTicketDetailView) -> CustomerTicketDetail:
    return CustomerTicketDetail(
        **_ticket_row(view).model_dump(),
        city=view.city,
        apartment=view.apartment,
        phone=view.phone,
        description=view.description,
        has_photo=view.has_photo,
    )


def _ticket_number_or_not_found(raw: str) -> int:
    """The ticket number of a raw path segment; anything else is the same 404 as a missing ticket."""
    number = parse_ticket_number_path(raw)
    if number is None:
        raise _error(CODE_TICKET_NOT_FOUND)
    return number


@router.get("/form-config", response_model=FormConfig)
def form_config(principal: CustomerPrincipalDep, session: ServiceSession) -> FormConfig:
    """The ticket form: active categories oldest first, the seven labels and the two options (R9.3)."""
    config = config_admin.get_form_config(session)
    return FormConfig(
        categories=[CategoryRef(id=c.id, name_he=c.name_he, name_en=c.name_en) for c in config.categories],
        labels={key: LabelTexts(he=label.he, en=label.en) for key, label in config.labels.items()},  # type: ignore[arg-type]
        settings=FormSettings(
            description_required=config.settings.description_required,
            photo_upload_enabled=config.settings.photo_upload_enabled,
        ),
    )


def _submission_values(form: FormData) -> dict[str, object]:
    """The form parts by name, for `validate_submission`.

    A name that appears twice cannot be a single value, so a repeated field is passed on as `None`,
    which fails its own field's validation (the same code a missing value gets). The `photo` part is
    not passed on: it is a known field that the photo handling below owns.
    """
    values: dict[str, object] = {}
    for name, value in form.multi_items():
        if name == FIELD_PHOTO:
            continue
        values[name] = None if name in values else value
    return values


def _store_ticket(
    session: Session, customer_id: uuid.UUID, values: dict[str, object], allocator: Allocator | None
) -> int:
    """Validate, allocate, insert and commit. Runs in a worker thread: it uses the blocking session."""
    form = config_admin.get_form_config(session)
    active_category_ids = {category.id for category in form.categories}
    try:
        clean = validate_submission(values, form.settings, active_category_ids)
        # HOOK (task 16.4): the photo is checked before this point and stored after the number is
        # allocated; `photo_key` below then becomes the stored object key. Until then no photo is kept.
        ticket = create_ticket(session, _Owner(customer_id), clean, None, allocator=allocator)
        number = ticket.ticket_number
        session.commit()
    except SubmissionValidationError as error:
        session.rollback()
        raise service_error(HTTPStatus.UNPROCESSABLE_ENTITY, fields=error.fields) from error
    except TicketNumbersExhausted as error:
        session.rollback()
        raise _error(error.code) from error
    return number


@router.post("/tickets", response_model=TicketCreated, status_code=HTTPStatus.CREATED)
async def submit_ticket(
    request: Request,
    principal: CustomerPrincipalDep,
    session: ServiceSession,
    allocator: TicketAllocatorDep,
) -> TicketCreated:
    """Open a ticket from a `multipart/form-data` form (R9).

    The handler reads the form itself, instead of declaring `Form(...)` parameters, so the order of the
    checks is ours and an unknown part is `unknown_field`, not something FastAPI decides. By the time
    it runs the customer is authenticated and past the password gate, so a refused customer never has
    their body parsed. The owner is `principal.customer_id`, never a form value.

    The handler is `async` only to await the form. Everything that touches the blocking session runs in
    a worker thread (`_store_ticket`), as the synchronous handlers of this router do.
    """
    try:
        form = await request.form()
    except StarletteHTTPException as error:  # a malformed or oversized multipart body
        raise service_error(HTTPStatus.UNPROCESSABLE_ENTITY, fields={"body": CODE_INVALID_REQUEST}) from error
    try:
        values = _submission_values(form)
        number = await run_in_threadpool(_store_ticket, session, principal.customer_id, values, allocator)
    finally:
        await form.close()
    return TicketCreated(ticket_number=number)


@router.get("/tickets", response_model=CustomerTicketPage)
def list_tickets(
    principal: CustomerPrincipalDep,
    session: ServiceSession,
    page: Annotated[str | None, Query()] = None,
) -> CustomerTicketPage:
    """One page (50) of the customer's own tickets, newest first (R12.1, R12.9).

    `page` is a raw string parsed by `parse_page`, so every failure is `invalid_page`.
    """
    try:
        number = parse_page(page)
    except GrammarError as error:
        raise _error(error.code) from error
    result = ticket_queries.list_customer_tickets(session, principal.customer_id, page=number)
    return CustomerTicketPage(
        items=[_ticket_row(item) for item in result.items],
        page=result.page,
        page_size=result.page_size,
        total=result.total,
    )


@router.get("/tickets/{ticket_number}", response_model=CustomerTicketDetail)
def get_ticket(
    ticket_number: str, principal: CustomerPrincipalDep, session: ServiceSession
) -> CustomerTicketDetail:
    """One own ticket. Missing, someone else's and malformed are the same 404 (R12.3)."""
    number = _ticket_number_or_not_found(ticket_number)
    try:
        view = ticket_queries.get_customer_ticket(session, principal.customer_id, number)
    except lifecycle.TicketNotFound as error:
        raise _error(error.code) from error
    return _ticket_detail(view)


@router.post("/tickets/{ticket_number}/cancel", response_model=CustomerTicketDetail)
def cancel_ticket(
    ticket_number: str,
    body: CancelTicketRequest,
    principal: CustomerPrincipalDep,
    session: ServiceSession,
) -> CustomerTicketDetail:
    """Cancel an own ticket that the customer last saw in `expected_status` (R13).

    The target is always `cancelled`: no route of this router takes a target, so a customer cannot ask
    for another status (R14.5). The response is built before the commit, from the row the lifecycle
    service returned, and the commit follows only a change that was applied.
    """
    number = _ticket_number_or_not_found(ticket_number)
    try:
        ticket = lifecycle.change_status(
            session,
            number,
            body.expected_status,
            "cancelled",
            CustomerActor(customer_id=principal.customer_id),
        )
        category = session.execute(select(Category).where(Category.id == ticket.category_id)).scalar_one()
        response = customer_ticket_detail(ticket, category)
    except lifecycle.LifecycleError as error:
        session.rollback()
        raise _error(error.code) from error
    session.commit()
    return response
