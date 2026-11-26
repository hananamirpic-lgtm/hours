"""Customer sign-in and change-password (Requirements 5 and 6; design.md, "Sign-in, lock and password rules").

Both functions return a value for every outcome that a caller can cause with its input, credential
failures and locks included. They raise only for programming errors and database failures. The router
owns the transaction and must **commit** whatever outcome comes back, because a failed attempt is a row
(`svc_signin_failures`) that has to survive the 401 that follows it. A router that rolled back on
`InvalidCredentials` would make the lock impossible to reach.

The lock is derived, not stored (R5.6, R5.8, R5.9). A phone is locked while `now` is before
`newest + signin_lock_seconds` and `svc_signin_failures` holds at least `signin_lock_attempts` rows for it
in the window that ends at the newest row (`failed_at > newest - signin_lock_seconds`). A request answered
as locked records nothing (R5.13), so no new row appears while the lock holds and it ends exactly
`signin_lock_seconds` after the newest row, the failure that started it. After that every older row is
outside the window of any later failure, so counting starts again from zero, with no state to clear.

The window is anchored at the newest row rather than at `now` (a deliberate departure from the design's
"`failed_at > now - LOCK_SECONDS`" sketch). With failures a few seconds apart, counting back from `now`
drops the older failures out of the window one by one and unlocks the phone before the `Retry-After` the
caller was given has passed.

Order of work in `sign_in`:

1. Normalize the phone. A malformed phone is `InvalidPhoneInput`: no verification, nothing recorded (R5.12).
2. Take the per-phone advisory lock (PostgreSQL only), so parallel guesses queue up behind each other and
   the count they read includes the failures of the ones before them.
3. Count recent failures. At or above the limit: `Locked`. No verification, nothing recorded (R5.13).
4. Load the customer by phone, then make exactly one `verify_password` call: against the stored hash when
   the customer exists, is active and the password is four ASCII digits, otherwise against the dummy hash,
   so the four failure classes cost the same time (R5.5). The four failures return the same value (R5.2 to
   R5.4).
5. A failure inserts one row and purges rows older than the retention horizon. A success deletes the
   phone's rows (R5.8).

`change_password` follows R6.3 to R6.6 and R6.11 in this order: lock check (no password is looked at), new
password format (not counted), current password (counted), new password differs from the initial password
and from the current one, then one `UPDATE` of hash, flag and token version.

Logging: this module logs nothing. No phone number, password or hash may reach a log line.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.security import dummy_password_hash, hash_password, verify_password
from app.service_tickets.config import ServiceConfig
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.grammars import is_valid_customer_password
from app.service_tickets.models import Customer, SigninFailure
from app.service_tickets.phone import InvalidPhone, normalize_phone
from app.service_tickets.services.customers import INITIAL_PASSWORD

#: Failure rows are kept at least this long, whatever the lock length is configured to (design: "Cleanup").
RETENTION_SECONDS = 3600

#: Verified against the dummy hash when there is no usable stored hash or the submitted password cannot be a
#: customer password, so the attempt still costs one bcrypt round. The dummy hash matches nothing.
_PLACEHOLDER_PASSWORD = "0000"


# --------------------------------------------------------------------------- outcomes


@dataclass(frozen=True, slots=True)
class Success:
    """A successful sign-in. `access_token` is the Customer_Token, valid `expires_in` seconds."""

    customer_id: uuid.UUID
    access_token: str
    expires_in: int
    must_change_password: bool


@dataclass(frozen=True, slots=True)
class InvalidCredentials:
    """Unregistered phone, inactive customer, wrong password or malformed password. One value for all four."""


@dataclass(frozen=True, slots=True)
class Locked:
    """The phone is locked. `retry_after` is whole seconds, rounded up, and at least 1 (R5.6)."""

    retry_after: int


@dataclass(frozen=True, slots=True)
class InvalidPhoneInput:
    """The phone failed the Requirement 4 grammar (HTTP 422 `invalid_phone` at the router)."""


@dataclass(frozen=True, slots=True)
class InvalidPasswordFormat:
    """The new password is not four ASCII digits (HTTP 422 `invalid_password_format`; not counted, R6.4)."""


@dataclass(frozen=True, slots=True)
class PasswordNotChanged:
    """The new password equals the initial or the current password (HTTP 422 `password_not_changed`, R6.5)."""


@dataclass(frozen=True, slots=True)
class PasswordChanged:
    """The password was changed. `access_token` replaces every token issued before (R6.3)."""

    customer_id: uuid.UUID
    access_token: str
    expires_in: int
    must_change_password: bool


SignInOutcome = Success | InvalidCredentials | Locked | InvalidPhoneInput
ChangePasswordOutcome = (
    PasswordChanged | InvalidCredentials | Locked | InvalidPasswordFormat | PasswordNotChanged
)


# --------------------------------------------------------------------------- helpers


def _utc(moment: datetime) -> datetime:
    """`moment` as an aware UTC datetime. SQLite returns `timestamptz` values without a zone."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _lock_phone(session: Session, phone: str) -> None:
    """Serialize sign-in work on one phone for the rest of the transaction (PostgreSQL only).

    Without this, many parallel guesses could all read "not locked" before any of them has written its
    failure row. SQLite, used by the unit tests, has no advisory locks and a single writer anyway.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(select(func.pg_advisory_xact_lock(func.hashtext(phone))))


def _retry_after(session: Session, phone: str, now: datetime, settings: ServiceConfig) -> int | None:
    """Seconds until the lock ends, or `None` when the phone is not locked."""
    newest = session.execute(
        select(func.max(SigninFailure.failed_at)).where(SigninFailure.phone == phone)
    ).scalar_one()
    if newest is None:
        return None
    newest = _utc(newest)
    window = timedelta(seconds=settings.signin_lock_seconds)
    ends_at = newest + window
    if now >= ends_at:
        return None
    # The window is anchored at the newest failure, not at `now`. A request answered as locked records
    # nothing, so the newest row is the failure that started the lock, and the lock must last until
    # `window` after it (R5.6). Counting back from `now` instead would let the lock end early, as the
    # earlier failures of the burst left the window one by one.
    count = session.execute(
        select(func.count(SigninFailure.id)).where(
            SigninFailure.phone == phone, SigninFailure.failed_at > newest - window
        )
    ).scalar_one()
    if count < settings.signin_lock_attempts:
        return None
    return max(1, math.ceil((ends_at - now).total_seconds()))


def _record_failure(session: Session, phone: str, now: datetime, settings: ServiceConfig) -> None:
    """Insert one failure row and purge the rows no window can need any more."""
    horizon = now - timedelta(seconds=max(RETENTION_SECONDS, settings.signin_lock_seconds))
    session.add(SigninFailure(phone=phone, failed_at=now))
    session.execute(delete(SigninFailure).where(SigninFailure.failed_at < horizon))
    session.flush()


def _clear_failures(session: Session, phone: str) -> None:
    session.execute(delete(SigninFailure).where(SigninFailure.phone == phone))


def _verify(password: object, password_hash: str | None) -> bool:
    """Exactly one `verify_password` call. `None` for the hash means "spend the time and fail".

    A password that is not four ASCII digits is never compared with a stored hash, whatever it is.
    """
    if password_hash is not None and is_valid_customer_password(password):
        return verify_password(str(password), password_hash)
    verify_password(_PLACEHOLDER_PASSWORD, dummy_password_hash())
    return False


# --------------------------------------------------------------------------- sign in


def sign_in(
    session: Session,
    *,
    phone_raw: str | None,
    password_raw: object,
    now: datetime,
    settings: ServiceConfig,
) -> SignInOutcome:
    """Check a phone and password and return the outcome. The caller commits (see the module docstring)."""
    try:
        phone = normalize_phone(phone_raw)
    except InvalidPhone:
        return InvalidPhoneInput()

    now = _utc(now)
    _lock_phone(session, phone)
    retry_after = _retry_after(session, phone, now, settings)
    if retry_after is not None:
        return Locked(retry_after)

    customer = session.execute(select(Customer).where(Customer.phone == phone)).scalar_one_or_none()
    usable = customer is not None and customer.is_active
    matched = _verify(password_raw, customer.password_hash if usable and customer is not None else None)
    if not (usable and matched and customer is not None):
        _record_failure(session, phone, now, settings)
        return InvalidCredentials()

    _clear_failures(session, phone)
    return Success(
        customer_id=customer.id,
        access_token=mint_customer_token(
            customer.id, customer.token_version, now, settings.token_ttl_seconds
        ),
        expires_in=settings.token_ttl_seconds,
        must_change_password=customer.must_change_password,
    )


# --------------------------------------------------------------------------- change password


def change_password(
    session: Session,
    *,
    customer_id: uuid.UUID,
    current_password_raw: object,
    new_password_raw: object,
    now: datetime,
    settings: ServiceConfig,
) -> ChangePasswordOutcome:
    """Change a customer's own password and return the outcome. The caller commits.

    `customer_id` comes from a verified Customer_Token, so a missing or inactive customer is not an
    expected input. It is answered as `InvalidCredentials` after one dummy verification rather than raised,
    so that the response gives nothing away.
    """
    now = _utc(now)
    customer = session.execute(
        select(Customer).where(Customer.id == customer_id).with_for_update()
    ).scalar_one_or_none()
    if customer is None:
        _verify(current_password_raw, None)
        return InvalidCredentials()

    phone = customer.phone
    _lock_phone(session, phone)

    retry_after = _retry_after(session, phone, now, settings)
    if retry_after is not None:
        return Locked(retry_after)

    if not is_valid_customer_password(new_password_raw):
        return InvalidPasswordFormat()

    matched = _verify(current_password_raw, customer.password_hash if customer.is_active else None)
    if not matched:
        _record_failure(session, phone, now, settings)
        return InvalidCredentials()

    # A matching current password ends the count even when the new password is then refused (R6.11).
    _clear_failures(session, phone)

    if new_password_raw in (INITIAL_PASSWORD, current_password_raw):
        return PasswordNotChanged()

    new_hash = hash_password(str(new_password_raw))
    session.execute(
        update(Customer)
        .where(Customer.id == customer_id)
        .values(
            password_hash=new_hash,
            must_change_password=False,
            token_version=Customer.token_version + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    refreshed = session.get(Customer, customer_id, populate_existing=True)
    if refreshed is None:  # deleted between the update and the read; nothing deletes customers today
        return InvalidCredentials()
    return PasswordChanged(
        customer_id=refreshed.id,
        access_token=mint_customer_token(
            refreshed.id, refreshed.token_version, now, settings.token_ttl_seconds
        ),
        expires_in=settings.token_ttl_seconds,
        must_change_password=False,
    )
