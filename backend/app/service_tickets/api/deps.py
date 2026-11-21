"""Principal dependencies of the service ticket API (design.md, "Staff dependencies"; R1.2 to R1.8, R1.14).

Each dependency answers "who is calling, and may they be here" and nothing else. They read one row
(`users` for staff, `svc_customers` for a customer) and write nothing: no audit row, no counter, no
session `add`. The check order is the one of R1.14, and it is the same for every dependency:

    bearer present -> token valid for this endpoint class -> the other kind of token (403)
    -> role -> admin 2FA enrolment owed -> forced password change

* `staff_principal`: any of `admin`, `operations_admin`, `technician`. A role from Hours that the
  ticket system does not serve (`site_manager`, `accounting`, `employee`) is `insufficient_role`.
  A valid customer token is 403 `customer_token_not_accepted`.
* `config_staff_principal`: `staff_principal`, then `admin` or `operations_admin` only.
* `customer_principal`: a customer token, a live and current `svc_customers` row, then the
  forced-password gate. `customer_principal_ungated` is the same without the last gate, for sign-out,
  `me` and change-password. A valid staff access token is 403 `staff_token_not_accepted`.
* `photo_principal`: the one shared route. Staff first, then customer. Whichever kind the token is,
  that kind's own checks apply; a token that is neither is 401.

Rate limiting (task 17.5; R22; design.md, "Request check order")
-----------------------------------------------------------------
The per-customer request window (120 per 60 s, `svc:rl:req:<customer id>`) is taken by every customer
dependency (`customer_principal`, `customer_principal_ungated`) and by the customer branch of
`photo_principal`, at one fixed place:

    bearer present -> token verifies as a customer token (signature, expiry, kind)
    -> REQUEST WINDOW (429 `rate_limited`) -> customer row read (401 revoked or inactive)
    -> forced password change (403)

* The customer id has to come from a token whose signature verified, so the window cannot sit before
  that step. It sits before the first database read, so a request over the limit costs no query and
  says nothing about the customer's row.
* A token that does not verify (missing, expired, bad signature, a staff token, any other kind) is
  answered 401 or 403 as before and counts against nothing.
* Rate limiting therefore precedes the 401 of a token that verifies but whose customer is revoked or
  inactive: over the limit, such a token gets 429 (the same answer as for a live one, so it reveals no
  account state); under the limit it gets its 401 as before. That one case is where the order differs from
  a literal reading of R1.14, as the design's flowchart says (rate limits first). Every request that
  reaches the row read has been counted, revoked or inactive ones included.
* Staff tokens are never limited by this feature: the staff dependencies take no slot, and a staff token
  on a customer endpoint is 403 before the window.
* Customer sign-in is not behind a customer dependency, so it is never counted here (R22.3). Its own
  per-address limit is `signin_address_limit` below.

All of these calls are synchronous Redis calls. The dependencies are plain `def`, so FastAPI runs them in
its thread pool and they do not block the event loop.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import BearerCredentials, DbSession, SettingsDep
from app.core.security import TokenError, TokenType, decode_token
from app.models.user import UserRole
from app.service_tickets import customer_token
from app.service_tickets.api import limits
from app.service_tickets.api.errors import service_error
from app.service_tickets.api.limits import ClockDep
from app.service_tickets.config import ServiceConfig
from app.service_tickets.error_codes import (
    CODE_CUSTOMER_TOKEN_NOT_ACCEPTED,
    CODE_INSUFFICIENT_ROLE,
    CODE_INVALID_TOKEN,
    CODE_NOT_AUTHENTICATED,
    CODE_PASSWORD_CHANGE_REQUIRED,
    CODE_STAFF_TOKEN_NOT_ACCEPTED,
    CODE_TOTP_ENROLMENT_REQUIRED,
)
from app.service_tickets.models import Customer
from app.service_tickets.services.staff_identity import StaffPrincipal, resolve_staff
from app.services import auth as auth_service

#: Who may use the ticket staff endpoints.
TICKET_STAFF_ROLES = frozenset({UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.TECHNICIAN})

#: Who may use the configuration endpoints and the customer management endpoints.
CONFIG_STAFF_ROLES = frozenset({UserRole.ADMIN, UserRole.OPERATIONS_ADMIN})


@dataclass(frozen=True, slots=True)
class CustomerPrincipal:
    """The signed-in customer, as the ticket routes need to know them.

    `must_change_password` is carried because `me` and the sign-out and change-password routes
    (the ungated dependency) already hold the row and need not read it a second time.
    """

    customer_id: uuid.UUID
    phone: str
    must_change_password: bool


def get_service_session(request: Request, session: DbSession) -> Session:
    """The request's database session, remembered so `ServiceRoute` can roll it back on a DB failure.

    Wraps `get_db` through `DbSession`, so a test that overrides `get_db` is honoured.
    """
    request.state.service_session = session
    return session


ServiceSession = Annotated[Session, Depends(get_service_session)]


def get_service_config(settings: SettingsDep) -> ServiceConfig:
    """The typed service-ticket configuration. Tests override this dependency to pick their own.

    Defined here (and re-exported by `customer_router`) because the principals below need the limits,
    and the router imports this module.
    """
    return ServiceConfig.from_settings(settings)


ServiceConfigDep = Annotated[ServiceConfig, Depends(get_service_config)]


def signin_address_limit(request: Request, config: ServiceConfigDep, clock: ClockDep) -> None:
    """Route dependency of customer sign-in: the per-address window (R22.1, R22.7).

    FastAPI resolves route dependencies before it validates the body model, so a request rejected here
    has had its phone and password neither read nor validated. See `limits.check_signin_address`.
    """
    limits.check_signin_address(request, config, clock())


def _unauthorized(code: str) -> HTTPException:
    return service_error(HTTPStatus.UNAUTHORIZED, code, headers={"WWW-Authenticate": "Bearer"})


def _forbidden(code: str) -> HTTPException:
    return service_error(HTTPStatus.FORBIDDEN, code)


def _bearer_token(credentials: BearerCredentials) -> str:
    if credentials is None or not credentials.credentials:
        raise _unauthorized(CODE_NOT_AUTHENTICATED)
    return credentials.credentials


def _is_valid_staff_token(token: str) -> bool:
    """Whether `token` is a staff access token with a good signature and expiry. Reads no database."""
    try:
        decode_token(token, expected_type=TokenType.ACCESS)
    except TokenError:
        return False
    return True


def _admit_staff(principal: StaffPrincipal) -> StaffPrincipal:
    """The role, 2FA and forced-password gates, in that order."""
    if principal.role not in TICKET_STAFF_ROLES:
        raise _forbidden(CODE_INSUFFICIENT_ROLE)
    if principal.is_2fa_enrolment_required:
        raise _forbidden(CODE_TOTP_ENROLMENT_REQUIRED)
    if principal.must_change_password and principal.role is not UserRole.ADMIN:
        raise _forbidden(CODE_PASSWORD_CHANGE_REQUIRED)
    return principal


def _customer_from_claims(
    session: Session, claims: customer_token.CustomerTokenClaims, *, gated: bool
) -> CustomerPrincipal:
    """One primary-key read of the customer. Never selects `password_hash`."""
    statement = select(
        Customer.phone, Customer.is_active, Customer.token_version, Customer.must_change_password
    ).where(Customer.id == claims.customer_id)
    row = session.execute(statement).one_or_none()
    if row is None or not row.is_active or row.token_version != claims.version:
        raise _unauthorized(CODE_INVALID_TOKEN)
    if gated and row.must_change_password:
        raise _forbidden(CODE_PASSWORD_CHANGE_REQUIRED)
    return CustomerPrincipal(
        customer_id=claims.customer_id, phone=row.phone, must_change_password=row.must_change_password
    )


def _resolve_customer(
    session: Session, token: str, *, gated: bool, config: ServiceConfig, clock: limits.Clock
) -> CustomerPrincipal:
    try:
        claims = customer_token.decode_customer_token(token)
    except customer_token.CustomerTokenError as error:
        if _is_valid_staff_token(token):
            raise _forbidden(CODE_STAFF_TOKEN_NOT_ACCEPTED) from error
        raise _unauthorized(CODE_INVALID_TOKEN) from error
    # The token verified, so its customer id can be trusted as a key. No database read has happened.
    limits.take_request_slot(claims.customer_id, config, clock())
    return _customer_from_claims(session, claims, gated=gated)


def staff_principal(session: ServiceSession, credentials: BearerCredentials) -> StaffPrincipal:
    token = _bearer_token(credentials)
    try:
        principal = resolve_staff(session, token, datetime.now(UTC))
    except auth_service.AuthError as error:
        if customer_token.is_valid_customer_token(token):
            raise _forbidden(CODE_CUSTOMER_TOKEN_NOT_ACCEPTED) from error
        raise _unauthorized(error.code) from error
    return _admit_staff(principal)


StaffPrincipalDep = Annotated[StaffPrincipal, Depends(staff_principal)]


def config_staff_principal(principal: StaffPrincipalDep) -> StaffPrincipal:
    if principal.role not in CONFIG_STAFF_ROLES:
        raise _forbidden(CODE_INSUFFICIENT_ROLE)
    return principal


def customer_principal(
    session: ServiceSession, credentials: BearerCredentials, config: ServiceConfigDep, clock: ClockDep
) -> CustomerPrincipal:
    return _resolve_customer(session, _bearer_token(credentials), gated=True, config=config, clock=clock)


def customer_principal_ungated(
    session: ServiceSession, credentials: BearerCredentials, config: ServiceConfigDep, clock: ClockDep
) -> CustomerPrincipal:
    return _resolve_customer(session, _bearer_token(credentials), gated=False, config=config, clock=clock)


def photo_principal(
    session: ServiceSession, credentials: BearerCredentials, config: ServiceConfigDep, clock: ClockDep
) -> StaffPrincipal | CustomerPrincipal:
    """Staff resolution first, then customer resolution, each with its own checks.

    A customer token fails staff resolution at the signature, before any database read, so at most
    one row is read. A staff token that is no longer good (deactivated, revoked) is 401 here, never
    `staff_token_not_accepted`: the route accepts staff. Staff are not rate limited; a customer is,
    after the token verifies and before the customer row is read (see the module docstring).
    """
    token = _bearer_token(credentials)
    try:
        principal = resolve_staff(session, token, datetime.now(UTC))
    except auth_service.AuthError:
        try:
            claims = customer_token.decode_customer_token(token)
        except customer_token.CustomerTokenError as error:
            raise _unauthorized(CODE_INVALID_TOKEN) from error
        limits.take_request_slot(claims.customer_id, config, clock())
        return _customer_from_claims(session, claims, gated=True)
    return _admit_staff(principal)


CustomerPrincipalDep = Annotated[CustomerPrincipal, Depends(customer_principal)]
CustomerPrincipalUngatedDep = Annotated[CustomerPrincipal, Depends(customer_principal_ungated)]
ConfigStaffPrincipalDep = Annotated[StaffPrincipal, Depends(config_staff_principal)]
PhotoPrincipalDep = Annotated[StaffPrincipal | CustomerPrincipal, Depends(photo_principal)]