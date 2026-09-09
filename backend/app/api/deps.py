"""Shared router dependencies."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Annotated, Any, TypeVar

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import ColumnElement, Select
from sqlalchemy.orm import Session

from app.core import authz
from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.models.user import User, UserRole
from app.services import auth as auth_service
from app.services import authz as authz_service
from app.services.audit import AuditContext

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[Session, Depends(get_db)]

# `auto_error=False` so a missing header produces this module's error envelope rather than FastAPI's
# own `{"detail": "Not authenticated"}`, which the front end cannot translate.
bearer_scheme = HTTPBearer(auto_error=False, description="Access token issued by POST /api/auth/login")

BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


def get_audit_context(request: Request) -> AuditContext:
    """Audit context for an unauthenticated request.

    The actor is left unset: at login time there is no established identity, and the service fills it
    in the moment it resolves one. The request id is read from the header for now; the API-conventions
    task takes over generating it.
    """
    return AuditContext(request_id=request.headers.get("x-request-id"))


RequestContext = Annotated[AuditContext, Depends(get_audit_context)]


def get_current_user(session: DbSession, credentials: BearerCredentials) -> User:
    """Resolve the caller from the bearer access token.

    Rejects an expired or malformed token, a deactivated user, and a token whose `token_version` the
    user has moved past — which is what makes deactivation and logout take effect immediately rather
    than at the token's expiry (Requirement 20.8).
    """
    if credentials is None or not credentials.credentials:
        raise unauthorized("not_authenticated")
    try:
        return auth_service.resolve_access_token(session, credentials.credentials)
    except auth_service.AuthError as error:
        raise unauthorized(error.code) from error


CurrentUser = Annotated[User, Depends(get_current_user)]


#: The non-administrator caller owes a password change before they may use anything outside `/auth`.
#: A module constant here rather than an `AuthError` subclass because the obligation is enforced at
#: this boundary, not raised by the auth service; the front end translates it under `apiError`.
CODE_PASSWORD_CHANGE_REQUIRED = "password_change_required"


def get_enrolled_user(user: CurrentUser) -> User:
    """The caller, once their startup obligations are satisfied (Requirement 1.6).

    This is the dependency every router other than the auth router uses. `CurrentUser` answers "who is
    calling"; this answers "may they call anything yet", and the two are separate because an
    administrator who has not enrolled has to be able to reach the endpoints that let them enrol, and
    a non-administrator who owes a password change has to be able to reach the one that lets them
    change it.

    Two obligations gate here, both surfaced on `GET /auth/me` so the front end can route straight to
    the right screen: the mandatory-2FA enrolment (admins), and the first-use password change
    (non-admins). Either one answered 403 blocks everything outside `/auth`.

    A distinct dependency rather than a flag on `CurrentUser` for one reason: a flag is something each
    router has to remember to read, and the endpoint that forgets is unguarded with nothing to show
    that it is. Here the type a router asks for *is* the guarantee — `EnrolledUser` cannot be obtained
    without the obligation being met — so an unguarded endpoint is visible in its signature.

    The exceptions are exactly the endpoints an unenrolled administrator needs and no more:
    `POST /auth/2fa/setup` and `POST /auth/2fa/verify` to enrol, `GET /auth/me` so the front end can
    see why it is blocked, and `POST /auth/logout` so a wrong account can be got out of.
    """
    if user.is_2fa_enrolment_required:
        raise forbidden(auth_service.TotpEnrolmentRequired.code)
    # A non-administrator who was created with an administrator-chosen password (or predates the
    # obligation and was backfilled) must set their own before using anything. Enforced exactly like
    # the 2FA gate above: the auth router uses `CurrentUser`, so login/refresh/logout/me and the
    # change-password endpoint stay reachable while everything else answers 403. The admin role is
    # never blocked — its `must_change_password` is false anyway, and the explicit role check is
    # belt-and-suspenders so an admin cannot be locked out even if the column were somehow set.
    if user.must_change_password and user.role != UserRole.ADMIN:
        raise forbidden(CODE_PASSWORD_CHANGE_REQUIRED)
    return user


EnrolledUser = Annotated[User, Depends(get_enrolled_user)]


def get_audit_context_for_user(request: Request, user: CurrentUser) -> AuditContext:
    """Audit context for an authenticated request, with the caller as the actor."""
    return AuditContext(actor_user_id=user.id, request_id=request.headers.get("x-request-id"))


AuthenticatedContext = Annotated[AuditContext, Depends(get_audit_context_for_user)]


def api_error(status: HTTPStatus, code: str, headers: dict[str, str] | None = None) -> HTTPException:
    """An HTTP failure carrying a machine code the front end can translate.

    The shape is the error envelope of the design, nested under FastAPI's `detail` for now: the
    API-conventions task installs the exception handlers that lift it to the top level, and doing it
    here would leave two competing envelopes to reconcile. One helper so that when the lift happens
    there is one place to change.
    """
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code}},
        headers=headers,
    )


def unauthorized(code: str) -> HTTPException:
    """A 401: the caller is not authenticated, or their credentials no longer hold."""
    return api_error(HTTPStatus.UNAUTHORIZED, code, headers={"WWW-Authenticate": "Bearer"})


def forbidden(code: str) -> HTTPException:
    """A 403: the caller is authenticated, and still may not do this.

    Distinct from 401 because the distinction is the whole message. A 401 tells a client to send
    credentials again; retrying with fresh credentials is exactly what does not help an administrator
    who has not enrolled in 2FA, and a front end that treats the two alike would sign them out in a
    loop instead of showing them the enrolment screen.
    """
    return api_error(HTTPStatus.FORBIDDEN, code)


# --------------------------------------------------------------------------- authorization
# Requirement 2, at the HTTP boundary. The policy is in `app.core.authz` and the session work is in
# `app.services.authz`; what is left here is turning a refusal into a 403 and recording the attempt.
#
# Requirement 2.9 asks that authorization be enforced server-side on every endpoint, and the shape
# below is what makes that checkable by reading a signature rather than a body. An endpoint states its
# requirement in the type it asks for — `AdminCaller`, or `CurrentCaller` plus a `require_site` call —
# so an endpoint that enforces nothing is visible as one that asked for nothing.

#: The caller's role does not permit this endpoint at all.
CODE_INSUFFICIENT_ROLE = "insufficient_role"
#: The endpoint is open to the role, but the site named in the request is outside their scope.
CODE_SITE_OUT_OF_SCOPE = "site_out_of_scope"
#: An employee asked about somebody else (Requirement 2.7).
CODE_NOT_OWN_RECORD = "not_own_record"

_Statement = TypeVar("_Statement", bound=Select)


def _attempted_action(request: Request) -> str:
    """What the caller tried, as the audit log records it: `GET /api/employees/{id}`.

    The route's template rather than the resolved path, so denials against different resources on the
    same endpoint group together; the resource itself is passed separately as `detail` where naming it
    helps.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    return f"{request.method} {path}"


@dataclass(frozen=True, slots=True)
class Caller:
    """The authenticated caller, their site scope, and the checks that depend on both.

    One object rather than four dependencies per endpoint. The plumbing a permission check needs — the
    session to audit into, the request to name the attempt, the audit context to attribute it — is
    identical everywhere, and an endpoint that has to assemble it by hand is an endpoint that will
    eventually assemble it wrong or skip the check. Here the checks read as one call:

        def read_site(site_id: uuid.UUID, caller: CurrentCaller) -> SiteResponse:
            caller.require_site(site_id)

    Every refusal audits before it raises, which is Requirement 2.8.
    """

    user: User
    scope: authz.SiteScope
    session: Session
    context: AuditContext
    action: str

    @property
    def role(self) -> UserRole:
        return self.user.role

    @property
    def may_read_money_fields(self) -> bool:
        """Whether wage, payroll and billing figures may be served to this caller (Requirement 2.5)."""
        return authz.may_read_money_fields(self.role)

    def redact(self, payload: Any) -> Any:
        """Strip the fields this caller may not see from a serialised response.

        Safe to call unconditionally: a finance role gets the payload back unchanged. That is the
        point — a redaction applied only where someone remembered a site manager might be reading is
        a redaction that will be missed somewhere.
        """
        return authz.redact(payload, self.role)

    def scope_query(self, statement: _Statement, site_column: ColumnElement[uuid.UUID]) -> _Statement:
        """Narrow a list query to the sites this caller may see (Requirement 2.3).

        In the `WHERE` clause, not in a comprehension over the results — see `app.services.authz`.
        """
        return authz_service.apply_site_scope(statement, site_column, self.scope)

    def require_site(self, site_id: uuid.UUID | None) -> None:
        """Refuse unless `site_id` is inside this caller's scope.

        Used on reads and writes of a single site's data. A missing id is a refusal, not a pass: at a
        permission check it means the caller could not say what they were asking for.
        """
        if self.scope.allows(site_id):
            return
        raise self.deny(
            code=CODE_SITE_OUT_OF_SCOPE,
            reason=authz_service.REASON_SITE_OUT_OF_SCOPE,
            detail=f"site={site_id}" if site_id is not None else "site=unspecified",
        )

    def require_own_employee_record(self, employee_id: uuid.UUID) -> None:
        """Refuse unless an employee-role caller is asking about themselves (Requirement 2.7).

        A no-op for the other three roles: what limits them is their site scope, checked separately.
        """
        if authz.is_own_record(
            role=self.role, caller_employee_id=self.user.employee_id, employee_id=employee_id
        ):
            return
        raise self.deny(
            code=CODE_NOT_OWN_RECORD,
            reason=authz_service.REASON_NOT_OWN_RECORD,
            detail=f"employee={employee_id}",
        )

    def deny(self, *, code: str, reason: str, detail: str | None = None) -> HTTPException:
        """Record the refused attempt and return the error to raise (Requirement 2.8).

        Returns rather than raises so a call site reads `raise caller.deny(...)`, which keeps the
        control flow visible at the point it happens instead of hiding a raise inside a helper.
        """
        authz_service.record_denial(
            self.session,
            user=self.user,
            action=self.action,
            reason=reason,
            context=self.context,
            detail=detail,
        )
        return forbidden(code)


def get_site_scope(session: DbSession, user: EnrolledUser) -> authz.SiteScope:
    """The set of sites the caller may read and write (Requirement 2.3)."""
    return authz_service.resolve_site_scope(session, user)


CallerSiteScope = Annotated[authz.SiteScope, Depends(get_site_scope)]


def get_caller(
    request: Request,
    session: DbSession,
    user: EnrolledUser,
    scope: CallerSiteScope,
    context: AuthenticatedContext,
) -> Caller:
    """Assemble the caller and everything a permission check on them needs."""
    return Caller(
        user=user,
        scope=scope,
        session=session,
        context=context,
        action=_attempted_action(request),
    )


CurrentCaller = Annotated[Caller, Depends(get_caller)]


def require_roles(*roles: UserRole) -> Callable[..., Caller]:
    """A dependency admitting only `roles`, and the administrator.

    The administrator is added to every guard rather than listed on each one, because Requirement 2.2
    is unconditional: an endpoint whose guard omitted `ADMIN` would be a bug that reads like a policy.
    Where an endpoint genuinely serves the caller's own record — an employee's scans — the role guard
    is only half the check and `require_own_employee_record` is the other half.

    Returns a `Caller` rather than a `User` so a guarded endpoint still has the site scope and the
    redaction helper to hand; a guard that stripped those away would push every endpoint into asking
    for two dependencies and getting the pairing wrong somewhere.
    """
    permitted = frozenset(roles) | authz.ROLES_WITH_FULL_ACCESS

    def dependency(caller: CurrentCaller) -> Caller:
        if caller.role not in permitted:
            raise caller.deny(
                code=CODE_INSUFFICIENT_ROLE,
                reason=authz_service.REASON_ROLE_NOT_PERMITTED,
            )
        return caller

    return dependency


#: Guards for the four roles of Requirement 2.1. Each admits its role and the administrator.
AdminCaller = Annotated[Caller, Depends(require_roles(UserRole.ADMIN))]
SiteManagerCaller = Annotated[Caller, Depends(require_roles(UserRole.SITE_MANAGER))]
AccountingCaller = Annotated[Caller, Depends(require_roles(UserRole.ACCOUNTING))]
EmployeeCaller = Annotated[Caller, Depends(require_roles(UserRole.EMPLOYEE))]

#: Operational administration: an administrator and the operations administrator, who has full
#: operational access but no financial visibility. This guards every non-finance endpoint that was
#: previously administrator-only. It is emphatically NOT used for payroll or billing, which stay on
#: `FinanceCaller`; `operations_admin` is absent from `FINANCE_ROLES`, so it is refused there.
OperationsCaller = Annotated[
    Caller, Depends(require_roles(UserRole.ADMIN, UserRole.OPERATIONS_ADMIN))
]

#: Guards for the role *groups* the requirements describe, so an endpoint names the policy it
#: implements rather than restating a list of roles that then drifts between endpoints.
FinanceCaller = Annotated[Caller, Depends(require_roles(*authz.FINANCE_ROLES))]

#: Who may read a client record: the operational administrators (admin, operations_admin) and
#: accounting. A client is a billing entity accounting works with, and the operations administrator
#: manages clients — but the record carries no money figure, so there is nothing to redact and no
#: reason to withhold it from the operations admin. A site manager still has no client endpoint
#: (Requirement 2.5). Client writes stay on OperationsCaller (admin + operations_admin).
ClientReaderCaller = Annotated[
    Caller,
    Depends(require_roles(UserRole.OPERATIONS_ADMIN, UserRole.ACCOUNTING)),
]
AttendanceWriterCaller = Annotated[Caller, Depends(require_roles(*authz.ATTENDANCE_WRITE_ROLES))]
HoursReaderCaller = Annotated[Caller, Depends(require_roles(*authz.HOURS_READ_ROLES))]
PersonnelReaderCaller = Annotated[Caller, Depends(require_roles(*authz.PERSONNEL_READ_ROLES))]
