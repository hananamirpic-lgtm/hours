"""Staff identity for the service ticket system (design.md, "Staff identity", decision 5).

Hours already authenticates its staff, and the ticket system reuses those logins. What it does not
reuse is `app.services.auth.resolve_access_token`, because that function loads the whole `users` row:
the bcrypt hash and the two encrypted TOTP secrets come with it, and the TOTP columns are decrypted on
load. A ticket route needs none of those, so this module reads only the columns it checks.

`resolve_staff` reproduces the four checks of `resolve_access_token` and raises the same
`InvalidToken`: a valid access token, an active user, a token `ver` equal to the user's
`token_version`, and a user who is not login-locked. A deactivated technician therefore gets the
401 `invalid_token` that Hours gives (Requirement 2.5). `tests/test_service_staff_identity.py` runs
both functions over the whole matrix and fails if they ever disagree.

The role is read from the row on every call and never from the token's `role` claim, which is advisory
(see `create_access_token`). A role change therefore applies on the very next request, even to a token
minted before the change (Requirement 2.7).

The functions read exactly one `users` row by primary key and write nothing. No audit row is added:
this package may not import `app.services.audit`, and a read has nothing to record.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session, load_only

from app.core.security import TokenError, TokenType, decode_token
from app.models.user import User, UserRole
from app.services import auth as auth_service


@dataclass(frozen=True, slots=True)
class StaffPrincipal:
    """The acting staff member, as the ticket routes need to know them.

    `display_name` is the username. The `users` table holds no personal name for a staff member
    (design finding F1), so this property is the single place that decision is made; if a name ever
    becomes available, only this property changes (Requirements 14.6, 16.4).
    """

    user_id: uuid.UUID
    username: str
    role: UserRole
    must_change_password: bool
    is_2fa_enrolment_required: bool

    @property
    def display_name(self) -> str:
        return self.username


def load_staff_identity(session: Session, user_id: uuid.UUID) -> User | None:
    """Read the one `users` row a staff request needs, or `None` when there is no such user.

    Only the columns the four checks and the principal use are selected, so `password_hash` and the
    TOTP secret columns are never read from the database, never decrypted, and stay deferred on the
    returned object. The object is expunged, so it is detached: touching a deferred column raises
    `DetachedInstanceError` instead of issuing a second query, and a stray assignment cannot flush.

    Meant for a request-scoped session. `Session.get` returns an instance already in the identity map
    without a query, and expunging would then detach the caller's own object.
    """
    user = session.get(
        User,
        user_id,
        options=[
            load_only(
                User.id,
                User.username,
                User.role,
                User.is_active,
                User.token_version,
                User.must_change_password,
                User.is_2fa_enabled,
                User.failed_login_count,
                User.locked_until,
            )
        ],
    )
    if user is not None:
        session.expunge(user)
    return user


def resolve_staff(session: Session, token: str, now: datetime | None = None) -> StaffPrincipal:
    """Return the staff member an access token belongs to, or raise `auth_service.InvalidToken`.

    The checks and their order match `auth_service.resolve_access_token`. `now` is the instant used
    for the login-lock test; token expiry is checked against the real clock by `decode_token`.
    """
    try:
        claims = decode_token(token, expected_type=TokenType.ACCESS)
    except TokenError as error:
        raise auth_service.InvalidToken from error

    user = load_staff_identity(session, claims.subject)
    if user is None or not user.is_active or claims.token_version != user.token_version:
        raise auth_service.InvalidToken
    if auth_service.is_locked(user, now):
        raise auth_service.InvalidToken

    return StaffPrincipal(
        user_id=user.id,
        username=user.username,
        role=user.role,
        must_change_password=user.must_change_password,
        is_2fa_enrolment_required=user.is_2fa_enrolment_required,
    )
