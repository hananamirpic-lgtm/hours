"""User request and response schemas (Requirement 1, 2, 20.8).

A *user* is a login, distinct from an *employee* (the person). This is the administrative surface for
creating and maintaining logins, assigning roles, and — for a site manager — assigning the sites that
define their scope (Requirement 2.3).

Locale-neutral like every other schema: no message text, only values the front end formats. No
credential ever leaves on a response: `password` is write-only, arriving on create and (optionally) on
update, and never appearing on any response model. `totp_secret` is not modelled here at all — the
2FA enrolment endpoints under `/auth` own it, and it must never be served to an administrator managing
another account.

`extra="forbid"` on every input model means a request carrying an unexpected field is rejected rather
than silently ignored, which is the same posture the rest of the write surface takes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.user import AppLanguage, UserRole

#: bcrypt reads at most 72 bytes; `app.core.security.hash_password` refuses anything longer rather
#: than truncating. Bounding it here turns that into a field-level validation error on the create
#: form instead of a 500 from the hasher. The floor matches the login schema's minimum.
_PASSWORD_MIN = 8
_PASSWORD_MAX = 72

#: A username is an identifier, not free text. Kept modest and non-blank; uniqueness is the database's
#: `uq_users_username`, reported by the service as a conflict.
_USERNAME = Field(min_length=1, max_length=150)


def _reject_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


# --------------------------------------------------------------------------- requests


class UserCreate(BaseModel):
    """Create a login (Requirement 1, 2.1).

    `employee_id` links a login to a person and is meaningful only for the employee role; the service
    enforces that pairing. `site_ids` may be supplied for a site manager to set their scope in the same
    call, and is ignored for other roles because scope is a site-manager concept (Requirement 2.3).
    """

    model_config = ConfigDict(extra="forbid")

    username: str = _USERNAME
    password: str = Field(min_length=_PASSWORD_MIN, max_length=_PASSWORD_MAX)
    role: UserRole
    employee_id: uuid.UUID | None = None
    language: AppLanguage = AppLanguage.HEBREW
    #: The sites a site manager may see and write. Applied only when the role is site manager.
    site_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("username")
    @classmethod
    def _username_not_blank(cls, value: str) -> str:
        return _reject_blank(value)


class UserUpdate(BaseModel):
    """Edit a login (Requirement 1, 2.1).

    Every field is optional; an omitted field is left unchanged. A `password` sent here is a reset,
    which bumps the token version so sessions on the old password end at once. `is_active` is *not*
    here — deactivation is its own endpoint, because it carries the session-ending side effect of
    Requirement 20.8 and should read as the deliberate act it is, not as one field among many.
    """

    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(default=None, min_length=1, max_length=150)
    password: str | None = Field(default=None, min_length=_PASSWORD_MIN, max_length=_PASSWORD_MAX)
    role: UserRole | None = None
    employee_id: uuid.UUID | None = None
    language: AppLanguage | None = None

    @field_validator("username")
    @classmethod
    def _username_not_blank_when_present(cls, value: str | None) -> str | None:
        return value if value is None else _reject_blank(value)


class UserSitesUpdate(BaseModel):
    """The body of `PUT /users/{id}/sites`: the set of sites a site manager may see (Requirement 2.3).

    Replaces the whole set rather than appending, so the request states the intended scope. Applied
    only to a site manager; the service rejects it for any other role, since scope is not a concept
    that applies to them.
    """

    model_config = ConfigDict(extra="forbid")

    site_ids: list[uuid.UUID] = Field(default_factory=list)


# --------------------------------------------------------------------------- responses


class UserListItem(BaseModel):
    """A row in the user list: identity, role and active state, no credential."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    role: UserRole
    is_active: bool
    is_2fa_enabled: bool


class UserListResponse(BaseModel):
    """A stable-sorted page of users (Requirement 22.5)."""

    items: list[UserListItem]
    total: int
    limit: int
    offset: int


class UserResponse(BaseModel):
    """A user card as served. No `password_hash`, no `totp_secret`, no lockout counters — the fields a
    browser does not need are fields that cannot leak.

    `is_2fa_enrolment_required` and `is_2fa_enrolment_prompted` are surfaced so the administrator sees
    at a glance whether a manager or accounting user still owes an enrolment (Requirement 1.6), and
    `site_ids` carries a site manager's assigned scope for the card (Requirement 2.3).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    role: UserRole
    employee_id: uuid.UUID | None
    language: AppLanguage
    is_active: bool
    is_2fa_enabled: bool
    is_2fa_enrolment_required: bool
    is_2fa_enrolment_prompted: bool
    #: Whether the login still owes a first-use password change (Requirement 1). Surfaced on the card
    #: so an administrator can see at a glance whether a non-admin has yet set their own password;
    #: always false for the admin role, which is exempt.
    must_change_password: bool
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime
    #: A site manager's assigned sites; empty for every other role.
    site_ids: list[uuid.UUID] = Field(default_factory=list)


class UserSitesResponse(BaseModel):
    """The sites a site manager is assigned to (Requirement 2.3), as served by `PUT /users/{id}/sites`."""

    user_id: uuid.UUID
    site_ids: list[uuid.UUID]
