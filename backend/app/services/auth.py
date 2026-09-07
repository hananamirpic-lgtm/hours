"""Authentication: login, TOTP enrolment, refresh rotation, logout, and access-token resolution.

Requirement 1 in one module. Five decisions in here are worth reading before changing anything.

**One failure message.** Requirement 1.2 asks that a rejection not disclose whether a username
exists. Every rejected login therefore raises the same `InvalidCredentials` with the same code —
unknown username, wrong password, deactivated account and locked account alike. The one exception is
`TotpRequired`, which is only ever raised *after* the password has been verified, so it tells the
caller nothing they did not already prove they knew.

**The lock needs no extra column.** Requirement 1.7 is "more than 5 failures within 15 minutes", and
the table gives us `failed_login_count` and `locked_until` and nothing else. So `locked_until` does
double duty: while the count is below the threshold it marks when the current counting window closes,
and once the count reaches the threshold it is extended to mark when the lock lifts. The two states
are told apart by the count, which is why `is_locked` tests both fields. A first failure after the
window has closed starts a fresh window rather than adding to a stale one, which is what makes it a
window rather than a lifetime tally.

**Rotation rides on `token_version`.** Reuse detection is not stateless — something has to remember
which refresh token is the current one. The schema offers exactly one place to remember it without
adding a table, so `token_version` is the rotation counter: every refresh bumps it, and a refresh
token carrying a stale version is therefore either a replay of an already-rotated token or a token
from before a deactivation. Both are refused, and the reuse case bumps the version again so every
outstanding refresh token for that user dies with it.

The cost of that choice, stated plainly: a user has one active refresh chain. Refreshing on a phone
invalidates the desktop's refresh token, and the desktop's next refresh is reported as a reuse event.
For this system — a login per person, largely one device each — that is an acceptable trade and
arguably a security gain. The fix, if concurrent sessions are ever needed, is a `refresh_tokens` table
holding one row per issued token; that is a schema change and is deliberately not made here.

**An unproven secret is not a credential.** Enrolment is two calls and two columns, and both halves
of that exist for the same reason: a secret nobody has demonstrated an authenticator can generate
codes from must never be the thing an account depends on. So issuing a secret changes nothing about
how the user signs in, `is_2fa_enabled` is set in exactly one place, and the secret in use survives
until its replacement is proven. The section below says more; `models.user.totp_pending_secret` says
why the column is separate.

**The service owns its transaction.** Unlike the rest of the service layer, these functions commit.
A failed login has to persist its counter increment and its audit row *while still raising*, so the
caller cannot be the one to decide whether the work lands — a router that forgot to commit on the
error path would silently disable the lockout.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pyotp
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import (
    ACCESS_TOKEN_TTL,
    TokenError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    dummy_password_hash,
    hash_password,
    verify_password,
)
from app.models.user import AppLanguage, User, UserRole
from app.services.audit import AuditContext, record_change, record_model_changes, snapshot

logger = logging.getLogger(__name__)

#: Requirement 1.7. The fifth failure is the one that locks: "more than 5 failed attempts" read
#: strictly would allow a sixth, and no attacker benefits from our generosity.
MAX_FAILED_ATTEMPTS = 5
#: How long failures keep accumulating before the tally resets.
FAILED_ATTEMPT_WINDOW = timedelta(minutes=15)
#: How long authentication stays locked once the threshold is reached.
LOCK_DURATION = timedelta(minutes=15)

#: Minimum length of a new password set through the self-service change. Mirrors the create/reset
#: path (`app.schemas.user._PASSWORD_MIN`); stated here too because the service is the last line of
#: defence and must not depend on a schema having validated first. The bcrypt 72-byte ceiling is
#: enforced by `hash_password`, which this maps rather than duplicates.
MIN_PASSWORD_LENGTH = 8

#: The employee login uses a short numeric PIN rather than the console roles' longer password: an
#: employee signs in on a phone with a 4-digit number as username, and a matching 4-8 digit PIN is the
#: usable second half. Enforced only for the employee role; every other role keeps `MIN_PASSWORD_LENGTH`
#: and the schema's 72-byte ceiling. "Digits" means 0-9 only, so the PIN is numeric like the username.
EMPLOYEE_PASSWORD_MIN_DIGITS = 4
EMPLOYEE_PASSWORD_MAX_DIGITS = 8

#: Codes either side of the current 30-second step that are still accepted, absorbing clock skew
#: between the server and the user's phone. One step each way is the usual setting; more turns a
#: 6-digit code into a materially smaller search space.
TOTP_VALID_WINDOW = 1

#: All authentication events land on this field of the `users` entity, so the audit view can render
#: them as one stream per user. The event name is the new value; there is no old value, because an
#: event is not a field that changed.
AUDIT_FIELD = "authentication"

EVENT_LOGIN_SUCCEEDED = "login_succeeded"
EVENT_LOGIN_FAILED = "login_failed"
EVENT_ACCOUNT_LOCKED = "account_locked"
EVENT_REFRESH_ROTATED = "refresh_rotated"
EVENT_REFRESH_REUSE_DETECTED = "refresh_reuse_detected"
EVENT_LOGOUT = "logout"
EVENT_TOTP_SECRET_ISSUED = "totp_secret_issued"
EVENT_2FA_ENABLED = "2fa_enabled"
EVENT_2FA_REENROLLED = "2fa_re_enrolled"
EVENT_PASSWORD_CHANGED = "password_changed"

REASON_BAD_PASSWORD = "incorrect_password"
REASON_BAD_TOTP = "incorrect_totp_code"
REASON_INACTIVE = "account_deactivated"
REASON_LOCKED = "account_locked"
REASON_FIRST_ENROLMENT = "first enrolment"
REASON_RE_ENROLMENT = "replacing a secret that was already in use"
REASON_SELF_SERVICE_CHANGE = "self_service_change"


class AuthError(Exception):
    """Base for every authentication refusal. `code` is what the API returns."""

    code = "authentication_failed"


class InvalidCredentials(AuthError):
    """The single generic login failure of Requirement 1.2."""

    code = "invalid_credentials"


class CurrentPasswordIncorrect(AuthError):
    """The current password supplied to a self-service change does not match the stored hash.

    Distinct from `InvalidCredentials` on purpose: the caller is already authenticated, so there is
    no username to protect and nothing to be gained from the generic message. A specific code lets
    the change-password screen say "your current password is wrong" rather than the login screen's
    ambiguous "username or password".
    """

    code = "current_password_incorrect"


class NewPasswordMustDiffer(AuthError):
    """The new password submitted to a change is the one already in force.

    A change that changes nothing is almost always a mistake, and accepting it would let a forced
    first-use change be satisfied without the user ever picking a new secret. Refused so the
    obligation is only cleared by an actual change.
    """

    code = "new_password_must_differ"


class NewPasswordTooShort(AuthError):
    """The new password is shorter than the minimum the create/reset path enforces.

    The schema validates this first, so a request through the API normally never reaches it; the
    check lives here too because the service is called directly in tests and must not trust a caller
    to have validated. Reuses the `validation_error` code the front end already translates rather
    than adding a message for a case the form prevents.
    """

    code = "validation_error"


class NewPasswordInvalid(AuthError):
    """The new password does not meet the rule for the caller's role.

    For an employee the password must be 4-8 digits (0-9 only); the check lives here as well as in the
    schema because the service is called directly in tests and must not trust a caller to have
    validated. Reuses the `validation_error` code the front end already translates.
    """

    code = "validation_error"


class TotpRequired(AuthError):
    """The password was correct and 2FA is enabled, but no code was supplied."""

    code = "totp_required"


class TotpNotEnrolled(AuthError):
    """2FA is enabled on the account with no secret stored, so no code can ever be correct.

    A configuration fault rather than a credential fault, and reported as such: the generic message
    would send the user round the loop of retyping a password that was right.
    """

    code = "totp_not_enrolled"


class InvalidToken(AuthError):
    """A presented token is not usable: malformed, expired, revoked, or replayed."""

    code = "invalid_token"


class TotpEnrolmentNotStarted(AuthError):
    """A code was submitted with no secret waiting to be proven.

    Either enrolment was never begun, or it was already completed and the secret promoted. Reported
    distinctly because the client's next move is `POST /auth/2fa/setup`, not "try another code".
    """

    code = "totp_enrolment_not_started"


class InvalidTotpCode(AuthError):
    """The submitted code does not match the pending secret.

    Distinct from `InvalidCredentials` on purpose: nothing here is a credential test in the sense
    Requirement 1.2 is about. The caller is already authenticated, and the secret being tested is one
    the server handed them seconds ago, so naming the failure discloses nothing and saves the user
    from guessing whether their clock or their typing was the problem.
    """

    code = "invalid_totp_code"


class TotpEnrolmentRequired(AuthError):
    """The caller's role makes 2FA mandatory and they have not enrolled (Requirement 1.6).

    Raised by the authorization layer rather than by anything in this module — the enrolment
    endpoints, logout and `/auth/me` stay reachable, and everything else does not.
    """

    code = "totp_enrolment_required"


@dataclass(frozen=True, slots=True)
class TokenPair:
    """What a successful login or refresh returns."""

    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "bearer"


@dataclass(frozen=True, slots=True)
class TotpEnrolment:
    """A freshly issued secret and the URI an authenticator app can be pointed at.

    Both describe the same secret. The URI exists because nobody wants to type 32 base32 characters
    into a phone, and the bare secret exists because a QR code is unreadable on the device that is
    displaying it — a user enrolling on their laptop with the authenticator on that same laptop needs
    something to copy.
    """

    secret: str
    provisioning_uri: str


# --------------------------------------------------------------------------- login


def login(
    session: Session,
    *,
    username: str,
    password: str,
    totp_code: str | None = None,
    context: AuditContext,
    now: datetime | None = None,
) -> TokenPair:
    """Verify credentials and issue a token pair.

    `now` is injectable so the lock window and TOTP step can be tested without sleeping. Raises
    `InvalidCredentials` for every rejection except the two TOTP configuration cases.
    """
    moment = now or _utcnow()
    user = session.scalar(select(User).where(User.username == username))

    if user is None:
        # Spend the same time as a real verification would. See `dummy_password_hash`.
        verify_password(password, dummy_password_hash())
        # No audit row: `change_logs.entity_id` is not nullable and there is no entity to attribute
        # this to. The application log is where a campaign against unknown usernames shows up.
        logger.info("login failed: unknown username")
        raise InvalidCredentials

    context = replace(context, actor_user_id=user.id)

    if is_locked(user, moment):
        # Deliberately not counted as another failure: extending the lock on every attempt would let
        # anyone keep a real user locked out indefinitely.
        _record_event(session, user, EVENT_LOGIN_FAILED, context, reason=REASON_LOCKED)
        session.commit()
        raise InvalidCredentials

    if not verify_password(password, user.password_hash):
        _register_failure(session, user, context, moment, reason=REASON_BAD_PASSWORD)
        raise InvalidCredentials

    # Checked after the password so the counter is only ever moved by someone who got the password
    # wrong. The message is the same either way, so this ordering discloses nothing.
    if not user.is_active:
        _record_event(session, user, EVENT_LOGIN_FAILED, context, reason=REASON_INACTIVE)
        session.commit()
        raise InvalidCredentials

    if user.is_2fa_enabled:
        if not user.totp_secret:
            raise TotpNotEnrolled
        if totp_code is None:
            raise TotpRequired
        if not verify_totp(user.totp_secret, totp_code, now=moment):
            _register_failure(session, user, context, moment, reason=REASON_BAD_TOTP)
            raise InvalidCredentials

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = moment
    _record_event(session, user, EVENT_LOGIN_SUCCEEDED, context)
    pair = _issue(user, moment)
    session.commit()
    return pair


def is_locked(user: User, now: datetime | None = None) -> bool:
    """Whether authentication is currently locked for this user.

    Both fields matter: the count says the threshold was reached, the timestamp says the lock has not
    yet lifted. Below the threshold the timestamp is only a counting window and must not lock anyone.
    """
    if user.failed_login_count < MAX_FAILED_ATTEMPTS or user.locked_until is None:
        return False
    return _as_utc(user.locked_until) > (now or _utcnow())


def verify_totp(secret: str, code: str, *, now: datetime | None = None) -> bool:
    """Whether `code` is valid for `secret` at `now`, within `TOTP_VALID_WINDOW` steps either side."""
    if not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(code, for_time=now or _utcnow(), valid_window=TOTP_VALID_WINDOW)
    except (ValueError, TypeError):
        # A secret that is not valid base32, or a code that is not digits. Neither authenticates.
        return False


def _register_failure(
    session: Session,
    user: User,
    context: AuditContext,
    now: datetime,
    *,
    reason: str,
) -> None:
    """Count one failed attempt, locking the account if that reaches the threshold."""
    window_open = user.locked_until is not None and _as_utc(user.locked_until) > now
    if window_open:
        user.failed_login_count += 1
    else:
        user.failed_login_count = 1
        user.locked_until = now + FAILED_ATTEMPT_WINDOW

    _record_event(session, user, EVENT_LOGIN_FAILED, context, reason=reason)
    if user.failed_login_count >= MAX_FAILED_ATTEMPTS:
        user.locked_until = now + LOCK_DURATION
        _record_event(
            session,
            user,
            EVENT_ACCOUNT_LOCKED,
            context,
            reason=f"{user.failed_login_count} failed attempts",
        )
    session.commit()


# --------------------------------------------------------------------------- two-factor enrolment
# Enrolment is deliberately two calls, because a secret that has been generated is not yet a secret
# that works: until a code proves the authenticator holds it, enabling 2FA on the strength of it would
# lock the user out of an account they can no longer authenticate to. So `begin_totp_enrolment` issues
# and stores the secret and changes nothing about how the user signs in, and `complete_totp_enrolment`
# is the only place `is_2fa_enabled` is ever set.
#
# The pending secret lives in its own column for the same reason read from the other end: a user who
# already has 2FA enabled and asks for a new secret must keep the old one working until the new one is
# proven, or an enrolment abandoned before the QR code was scanned leaves them with nothing.


def begin_totp_enrolment(session: Session, *, user: User, context: AuditContext) -> TotpEnrolment:
    """Issue a fresh secret for `user` and return it with its provisioning URI.

    Does not enable 2FA, and does not touch the secret the user currently signs in with. Calling this
    twice simply replaces one unproven secret with another, which costs nothing: the client that asked
    is the only party that ever held the first one.
    """
    secret = pyotp.random_base32()
    user.totp_pending_secret = secret

    context = replace(context, actor_user_id=user.id)
    _record_event(
        session,
        user,
        EVENT_TOTP_SECRET_ISSUED,
        context,
        reason=REASON_RE_ENROLMENT if user.is_2fa_enabled else REASON_FIRST_ENROLMENT,
    )
    session.commit()
    return TotpEnrolment(secret=secret, provisioning_uri=totp_provisioning_uri(user, secret))


def complete_totp_enrolment(
    session: Session,
    *,
    user: User,
    totp_code: str,
    context: AuditContext,
    now: datetime | None = None,
) -> None:
    """Prove a pending secret with a code, then make it the one that authenticates.

    Raises `TotpEnrolmentNotStarted` when there is nothing pending and `InvalidTotpCode` when the code
    does not match. Neither failure changes anything, so a mistyped code leaves the user exactly where
    they were — still enrolling, or still using their previous secret.

    A wrong code here is not counted towards the login lock of Requirement 1.7. The lock exists to
    stop an attacker guessing a credential they do not hold; the secret being tested here was handed
    to this caller moments ago over their own authenticated session, so there is nothing to guess and
    the only person a counter could lock out is the legitimate user fumbling a six-digit code.
    """
    pending = user.totp_pending_secret
    if not pending:
        raise TotpEnrolmentNotStarted
    if not verify_totp(pending, totp_code, now=now or _utcnow()):
        raise InvalidTotpCode

    was_enabled = user.is_2fa_enabled
    user.totp_secret = pending
    user.totp_pending_secret = None
    user.is_2fa_enabled = True

    _record_event(
        session,
        user,
        EVENT_2FA_REENROLLED if was_enabled else EVENT_2FA_ENABLED,
        replace(context, actor_user_id=user.id),
    )
    session.commit()


def totp_provisioning_uri(user: User, secret: str) -> str:
    """The `otpauth://` URI for a secret, as an authenticator app expects it.

    The issuer names the deployment and the account name is the username, which is what the app shows
    beside the code. Both matter: an entry labelled only with a six-digit number is one a user deletes
    a year later without knowing what it was for.
    """
    return pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name=get_settings().totp_issuer)


# --------------------------------------------------------------------------- refresh and logout


def refresh(
    session: Session,
    *,
    refresh_token: str,
    context: AuditContext,
    now: datetime | None = None,
) -> TokenPair:
    """Rotate a refresh token, or refuse it and revoke the user's outstanding tokens.

    A token whose `ver` no longer matches the user has already been rotated away — the only way to
    hold one is to have replayed it, or to be using a token issued before a deactivation or logout.
    Either way the safe response is the same: refuse, and invalidate whatever else is outstanding, on
    the assumption that a replay means the token leaked.
    """
    moment = now or _utcnow()
    try:
        claims = decode_token(refresh_token, expected_type=TokenType.REFRESH)
    except TokenError as error:
        raise InvalidToken from error

    user = session.get(User, claims.subject)
    if user is None:
        raise InvalidToken

    context = replace(context, actor_user_id=user.id)

    if not user.is_active:
        _record_event(session, user, EVENT_LOGIN_FAILED, context, reason=REASON_INACTIVE)
        session.commit()
        raise InvalidToken

    if claims.token_version != user.token_version:
        user.token_version += 1
        _record_event(
            session,
            user,
            EVENT_REFRESH_REUSE_DETECTED,
            context,
            reason=f"rotation {claims.rotation_id} was already rotated; outstanding tokens revoked",
        )
        session.commit()
        raise InvalidToken

    user.token_version += 1
    pair = _issue(user, moment)
    _record_event(session, user, EVENT_REFRESH_ROTATED, context, reason=f"rotation {claims.rotation_id}")
    session.commit()
    return pair


def logout(session: Session, *, user: User, context: AuditContext) -> None:
    """End the session by bumping the token version, which invalidates every issued token."""
    user.token_version += 1
    _record_event(session, user, EVENT_LOGOUT, replace(context, actor_user_id=user.id))
    session.commit()


def set_language(session: Session, *, user: User, language: AppLanguage, context: AuditContext) -> User:
    """Persist the caller's interface language (Requirement 21.2).

    A field change like any other, so it goes through the diff writer and lands one audit row — but
    only when the value actually moves, so toggling to the language already stored writes nothing.
    """
    before = snapshot(user)
    user.language = language
    record_model_changes(session, user, before, context=replace(context, actor_user_id=user.id))
    session.commit()
    return user


def _validate_new_password(user: User, new_password: str) -> None:
    """Enforce the password rule for the caller's role, raising a distinct refusal on failure.

    An employee login uses a numeric PIN of `EMPLOYEE_PASSWORD_MIN_DIGITS`-`EMPLOYEE_PASSWORD_MAX_DIGITS`
    digits (0-9 only), matching its numeric username; every other role keeps the console minimum of
    `MIN_PASSWORD_LENGTH` (the bcrypt 72-byte ceiling is the schema's and `hash_password`'s concern).
    """
    if user.role is UserRole.EMPLOYEE:
        if not new_password.isdigit():
            raise NewPasswordInvalid
        if not (EMPLOYEE_PASSWORD_MIN_DIGITS <= len(new_password) <= EMPLOYEE_PASSWORD_MAX_DIGITS):
            raise NewPasswordInvalid
        return
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise NewPasswordTooShort


def change_password(
    session: Session,
    *,
    user: User,
    current_password: str,
    new_password: str,
    context: AuditContext,
) -> User:
    """Change the caller's own password, clearing any first-use obligation.

    Self-service, so the caller proves the current password rather than an administrator resetting it
    on their behalf. Three rules, each raising a distinct refusal that changes nothing:

    * The current password must verify, or `CurrentPasswordIncorrect`.
    * The new password must not equal the current one, or `NewPasswordMustDiffer` — a change that
      changes nothing would let a forced first-use change be satisfied without picking a new secret.
    * The new password must meet the minimum length, or `NewPasswordTooShort`; `hash_password` is the
      backstop for the bcrypt byte ceiling.

    On success the hash is replaced, `must_change_password` is cleared, and `token_version` is bumped
    so every session established under the old password ends at once — the same immediacy the admin
    reset path gives (Requirement 20.8, applied to a self-service change). Two audit rows are written,
    matching the reset path: the hash change through the diff writer, and one row naming *why* the
    token version moved. Like the rest of this module, the function owns its transaction and commits.
    """
    if not verify_password(current_password, user.password_hash):
        raise CurrentPasswordIncorrect
    _validate_new_password(user, new_password)
    if verify_password(new_password, user.password_hash):
        raise NewPasswordMustDiffer

    context = replace(context, actor_user_id=user.id)
    before = snapshot(user)
    # `hash_password` raises `ValueError` for a password bcrypt cannot read (over 72 bytes); the schema
    # bounds the field so a request never reaches it, and a direct caller passing an over-long value is
    # a programming error, so it is left to surface rather than mapped to a refusal.
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.token_version += 1

    record_model_changes(session, user, before, context=context)
    # The hash change is audited by the diff above; this row names why the token version moved, so a
    # reader sees a deliberate self-service change rather than an unexplained version bump.
    record_change(
        session,
        entity_type="users",
        entity_id=user.id,
        field="password",
        old_value=None,
        new_value="changed",
        context=context,
        reason=REASON_SELF_SERVICE_CHANGE,
    )
    session.commit()
    return user


def resolve_access_token(session: Session, token: str, *, now: datetime | None = None) -> User:
    """Return the user an access token belongs to, or raise `InvalidToken`.

    The version check is what makes Requirement 1.4 immediate: a deactivation, a logout or a password
    change bumps `token_version`, and every token minted under the old version stops working on its
    next use rather than at its expiry.
    """
    try:
        claims = decode_token(token, expected_type=TokenType.ACCESS)
    except TokenError as error:
        raise InvalidToken from error

    user = session.get(User, claims.subject)
    if user is None or not user.is_active or claims.token_version != user.token_version:
        raise InvalidToken
    if is_locked(user, now or _utcnow()):
        raise InvalidToken
    return user


# --------------------------------------------------------------------------- helpers


def _issue(user: User, now: datetime) -> TokenPair:
    access_token = create_access_token(
        user_id=user.id, token_version=user.token_version, role=user.role.value, now=now
    )
    refresh_token, _ = create_refresh_token(user_id=user.id, token_version=user.token_version, now=now)
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
    )


def _record_event(
    session: Session,
    user: User,
    event: str,
    context: AuditContext,
    *,
    reason: str | None = None,
) -> None:
    """Write one authentication event to the audit log (Requirement 13.5).

    The counter columns this module moves are not audited as field changes: a brute-force attempt
    would then write ten rows saying `failed_login_count` went from 3 to 4, burying the one row that
    says what happened.
    """
    record_change(
        session,
        entity_type="users",
        entity_id=user.id,
        field=AUDIT_FIELD,
        old_value=None,
        new_value=event,
        context=context,
        reason=reason,
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive timestamp.

    PostgreSQL's `timestamptz` always reads back aware, but SQLite — which the unit tests run on —
    drops the offset, and comparing a naive timestamp with an aware one raises. Normalising on read
    keeps the comparison honest on both.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
