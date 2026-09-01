"""Authentication router. HTTP only: validate, call the service, map a refusal onto a status code.

Sign-in and refresh carry a per-client rate limit (Requirement 20.4) on top of the failed-attempt
lock in the service: the lock is per account and this is the blunt per-IP ceiling underneath it, so a
caller cannot spray guesses across many usernames from one address faster than the window allows.
"""

from __future__ import annotations

from dataclasses import asdict
from http import HTTPStatus

from fastapi import APIRouter, Depends, Response

from app.api.deps import (
    AuthenticatedContext,
    CurrentUser,
    DbSession,
    RequestContext,
    api_error,
    unauthorized,
)
from app.core.rate_limit import rate_limit_auth
from app.schemas.auth import (
    ChangePasswordRequest,
    CurrentUserResponse,
    LanguagePreferenceRequest,
    LoginRequest,
    RefreshRequest,
    TokenResponse,
    TotpSetupResponse,
    TotpVerifyRequest,
)
from app.services import auth as auth_service

# Every endpoint in this router takes `CurrentUser` rather than `EnrolledUser`, so a caller still owing
# a startup obligation — an administrator who has not completed 2FA enrolment, or a non-administrator
# who has not made a first-use password change — can still reach all of them. That is deliberate and
# it is the whole exception list: enrol, change your password, see why you are blocked, or sign out.
# Any new endpoint added here has to earn its place on that list; anything else belongs on
# `EnrolledUser`.
router = APIRouter(prefix="/auth", tags=["auth"])

_UNAUTHORIZED_RESPONSE = {
    HTTPStatus.UNAUTHORIZED: {"description": "Authentication failed; the body carries a machine code"}
}

_RATE_LIMITED_RESPONSE = {
    HTTPStatus.TOO_MANY_REQUESTS: {
        "description": "Too many requests from this client; retry after the window (Requirement 20.4)"
    }
}

_VERIFY_FAILURES = {
    HTTPStatus.BAD_REQUEST: {"description": "The submitted code does not match the pending secret"},
    HTTPStatus.CONFLICT: {"description": "No enrolment is in progress"},
}

#: Which status each enrolment refusal maps onto. The wrong code is a bad request — the submitted
#: value is wrong and a different value would work. A missing pending secret is a conflict — nothing
#: the client can put in this request would help, because the resource is not in a state to accept it.
_ENROLMENT_ERROR_STATUS = {
    auth_service.InvalidTotpCode.code: HTTPStatus.BAD_REQUEST,
    auth_service.TotpEnrolmentNotStarted.code: HTTPStatus.CONFLICT,
}

#: Which status each change-password refusal maps onto. A wrong current password and an identical new
#: one are both bad requests — the submitted values are wrong and different values would work; the
#: too-short case is a 422 to match the schema's own validation of the same bound.
_CHANGE_PASSWORD_ERROR_STATUS = {
    auth_service.CurrentPasswordIncorrect.code: HTTPStatus.BAD_REQUEST,
    auth_service.NewPasswordMustDiffer.code: HTTPStatus.BAD_REQUEST,
    auth_service.NewPasswordTooShort.code: HTTPStatus.UNPROCESSABLE_ENTITY,
}


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Sign in",
    description=(
        "Returns an access token valid for 15 minutes and a refresh token valid for 7 days. "
        "Every rejection returns the same `invalid_credentials` code, so the response does not "
        "disclose whether the username exists. When the account has 2FA enabled and no code was "
        "supplied, the code is `totp_required`."
    ),
    responses=_UNAUTHORIZED_RESPONSE | _RATE_LIMITED_RESPONSE,
    dependencies=[Depends(rate_limit_auth)],
)
def login(
    payload: LoginRequest,
    session: DbSession,
    context: RequestContext,
) -> TokenResponse:
    try:
        pair = auth_service.login(
            session,
            username=payload.username,
            password=payload.password,
            totp_code=payload.totp_code,
            context=context,
        )
    except auth_service.AuthError as error:
        raise unauthorized(error.code) from error
    return TokenResponse(**asdict(pair))


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Renew an expired access token",
    description=(
        "Exchanges a refresh token for a new pair. The presented token is rotated away, so "
        "presenting it a second time is refused and revokes the user's outstanding refresh tokens."
    ),
    responses=_UNAUTHORIZED_RESPONSE | _RATE_LIMITED_RESPONSE,
    dependencies=[Depends(rate_limit_auth)],
)
def refresh(
    payload: RefreshRequest,
    session: DbSession,
    context: RequestContext,
) -> TokenResponse:
    try:
        pair = auth_service.refresh(session, refresh_token=payload.refresh_token, context=context)
    except auth_service.AuthError as error:
        raise unauthorized(error.code) from error
    return TokenResponse(**asdict(pair))


@router.post(
    "/logout",
    status_code=HTTPStatus.NO_CONTENT,
    summary="Sign out",
    description="Invalidates every token issued to the caller, including refresh tokens.",
    responses=_UNAUTHORIZED_RESPONSE,
)
def logout(user: CurrentUser, session: DbSession, context: AuthenticatedContext) -> Response:
    auth_service.logout(session, user=user, context=context)
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.post(
    "/2fa/setup",
    response_model=TotpSetupResponse,
    summary="Begin two-factor enrolment",
    description=(
        "Issues a fresh TOTP secret for the caller and returns it with an `otpauth://` provisioning "
        "URI to render as a QR code. This does **not** enable 2FA and does not affect how the caller "
        "signs in: `POST /auth/2fa/verify` does that, once a code proves the secret reached an "
        "authenticator. A caller who already has 2FA enabled keeps their existing secret working "
        "until the new one is verified, so an abandoned enrolment costs nothing. "
        "The response carries a credential and is served `no-store`."
    ),
    responses=_UNAUTHORIZED_RESPONSE,
)
def setup_two_factor(
    user: CurrentUser,
    session: DbSession,
    context: AuthenticatedContext,
    response: Response,
) -> TotpSetupResponse:
    enrolment = auth_service.begin_totp_enrolment(session, user=user, context=context)
    # A secret in a shared cache or a browser's back-forward cache is a secret in a place nobody is
    # thinking about. The header costs nothing and the alternative is unbounded.
    response.headers["Cache-Control"] = "no-store"
    return TotpSetupResponse(secret=enrolment.secret, provisioning_uri=enrolment.provisioning_uri)


@router.post(
    "/2fa/verify",
    response_model=CurrentUserResponse,
    summary="Complete two-factor enrolment",
    description=(
        "Checks the code against the secret issued by `POST /auth/2fa/setup` and, only if it matches, "
        "makes that secret the one the caller signs in with and enables 2FA. Returns the caller's "
        "updated state, which is what a front end needs to drop its enrolment prompt. Subsequent "
        "logins require a code."
    ),
    responses=_UNAUTHORIZED_RESPONSE | _VERIFY_FAILURES,
)
def verify_two_factor(
    payload: TotpVerifyRequest,
    user: CurrentUser,
    session: DbSession,
    context: AuthenticatedContext,
) -> CurrentUserResponse:
    try:
        auth_service.complete_totp_enrolment(session, user=user, totp_code=payload.totp_code, context=context)
    except auth_service.AuthError as error:
        status = _ENROLMENT_ERROR_STATUS.get(error.code, HTTPStatus.BAD_REQUEST)
        raise api_error(status, error.code) from error
    return CurrentUserResponse.model_validate(user)


@router.post(
    "/change-password",
    response_model=CurrentUserResponse,
    summary="Change your own password",
    description=(
        "Sets a new password for the caller, proving the current one first. Clears the first-use "
        "obligation that blocks a non-administrator from the rest of the API (Requirement 1), and "
        "ends every other session by rotating the caller's token version — so the access token used "
        "to make this call keeps working, but any other session on the old password does not. "
        "Reachable while the caller is otherwise blocked, like the rest of this router. `400` with "
        "`current_password_incorrect` when the current password is wrong, `400` with "
        "`new_password_must_differ` when the new password equals the current one."
    ),
    responses=_UNAUTHORIZED_RESPONSE
    | {HTTPStatus.BAD_REQUEST: {"description": "The current password is wrong or the new one is unchanged"}},
)
def change_password(
    payload: ChangePasswordRequest,
    user: CurrentUser,
    session: DbSession,
    context: AuthenticatedContext,
) -> CurrentUserResponse:
    try:
        auth_service.change_password(
            session,
            user=user,
            current_password=payload.current_password,
            new_password=payload.new_password,
            context=context,
        )
    except auth_service.AuthError as error:
        status = _CHANGE_PASSWORD_ERROR_STATUS.get(error.code, HTTPStatus.BAD_REQUEST)
        raise api_error(status, error.code) from error
    return CurrentUserResponse.model_validate(user)


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="The signed-in user",
    description=(
        "Identity, role, language preference and 2FA enrolment state of the caller. Carries no "
        "credential fields. `is_2fa_enrolment_required` is true for a role where 2FA is mandatory and "
        "enrolment is outstanding, in which case every endpoint outside this router answers 403 until "
        "it is done; `is_2fa_enrolment_prompted` is the softer signal for roles that are asked but not "
        "blocked."
    ),
    responses=_UNAUTHORIZED_RESPONSE,
)
def read_me(user: CurrentUser) -> CurrentUserResponse:
    return CurrentUserResponse.model_validate(user)


@router.patch(
    "/me/language",
    response_model=CurrentUserResponse,
    summary="Set the interface language",
    description=(
        "Persists the caller's language preference against their account, so the choice follows the "
        "user across devices rather than living only in one browser (Requirement 21.2). Returns the "
        "caller's updated state. Reachable before 2FA enrolment, like the rest of this router, so the "
        "enrolment screen itself can be read in the chosen language."
    ),
    responses=_UNAUTHORIZED_RESPONSE,
)
def set_language(
    payload: LanguagePreferenceRequest,
    user: CurrentUser,
    session: DbSession,
    context: AuthenticatedContext,
) -> CurrentUserResponse:
    auth_service.set_language(session, user=user, language=payload.language, context=context)
    return CurrentUserResponse.model_validate(user)
