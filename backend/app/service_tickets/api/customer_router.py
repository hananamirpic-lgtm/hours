"""Customer endpoints of the service ticket API (design.md, "API surface"; Requirements 5, 6, 7.11).

One `APIRouter` with `route_class=ServiceRoute`, mounted under `{api_prefix}/service` by task 13.7. It
holds the customer authentication endpoints here (task 13.2) and gets the form, ticket and photo
endpoints from the later tasks, each in its own marked section below.

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

from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import SettingsDep
from app.service_tickets.api.deps import (
    CustomerPrincipal,
    CustomerPrincipalUngatedDep,
    ServiceSession,
)
from app.service_tickets.api.errors import service_error
from app.service_tickets.api.route import ServiceRoute
from app.service_tickets.config import ServiceConfig
from app.service_tickets.error_codes import (
    CODE_INVALID_CREDENTIALS,
    CODE_INVALID_PASSWORD_FORMAT,
    CODE_INVALID_PHONE,
    CODE_INVALID_TOKEN,
    CODE_PASSWORD_NOT_CHANGED,
    CODE_TOO_MANY_ATTEMPTS,
)
from app.service_tickets.models import Customer
from app.service_tickets.schemas import CustomerProfile, TokenResponse
from app.service_tickets.services import signin

router = APIRouter(route_class=ServiceRoute, tags=["service-customer"])

#: HTTP status of each customer authentication error code (design.md, "Customer and shared endpoints").
_ERROR_STATUS: dict[str, HTTPStatus] = {
    CODE_INVALID_PHONE: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_CREDENTIALS: HTTPStatus.UNAUTHORIZED,
    CODE_TOO_MANY_ATTEMPTS: HTTPStatus.TOO_MANY_REQUESTS,
    CODE_INVALID_PASSWORD_FORMAT: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_PASSWORD_NOT_CHANGED: HTTPStatus.UNPROCESSABLE_ENTITY,
    CODE_INVALID_TOKEN: HTTPStatus.UNAUTHORIZED,
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
# `GET /form-config`, `POST /tickets`, `GET /tickets`, `GET /tickets/{ticket_number}` and
# `POST /tickets/{ticket_number}/cancel` are added here by task 13.3.
