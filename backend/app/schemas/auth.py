"""Authentication request and response schemas.

Locale-neutral like every other response: no message text, only values the front end formats.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.user import AppLanguage, UserRole


class LoginRequest(BaseModel):
    """Credentials, with the TOTP code when the account has 2FA enabled."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=150)
    # Generous upper bound so a passphrase is not rejected here; `verify_password` refuses anything
    # bcrypt cannot read, which is the limit that actually applies.
    password: str = Field(min_length=1, max_length=256)
    #: Six digits. Absent on the first attempt, which is how the client learns 2FA is required.
    totp_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")


class RefreshRequest(BaseModel):
    """The refresh token being exchanged. Rotated on use, so it is single-use by design."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=1)


class TotpVerifyRequest(BaseModel):
    """The code proving the pending secret reached an authenticator."""

    model_config = ConfigDict(extra="forbid")

    totp_code: str = Field(pattern=r"^[0-9]{6}$")


class LanguagePreferenceRequest(BaseModel):
    """The interface language the caller wants persisted (Requirement 21.2).

    The toggle lives in the browser, but the preference belongs to the account: a user who signs in
    on a second device should find the language they chose, not the browser's guess. So the front end
    keeps a local copy for the first paint and writes it here, where it survives the browser.
    """

    model_config = ConfigDict(extra="forbid")

    language: AppLanguage


class ChangePasswordRequest(BaseModel):
    """A self-service password change: the current password, and the new one.

    The current password proves the caller before the change, so the endpoint is safe to reach while
    the caller is otherwise blocked by the first-use obligation. The new password's minimum length
    mirrors the create/reset path; the generous upper bound keeps a passphrase from being rejected
    here, and `hash_password` refuses anything bcrypt cannot read. `extra="forbid"`, like the rest of
    the write surface.
    """

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=256)
    #: Floor is 4 to admit an employee's 4-8 digit numeric PIN; the role-specific rule (employee =
    #: 4-8 digits, console roles >= 8) is enforced by `app.services.auth.change_password`.
    new_password: str = Field(min_length=4, max_length=256)


class TotpSetupResponse(BaseModel):
    """A newly issued secret, in both forms a client needs.

    This is the one response in the system that carries a credential, so it is `no-store` at the HTTP
    layer and must not be logged. It does not enable 2FA: `POST /auth/2fa/verify` does, once a code
    proves the secret arrived somewhere that can generate them.
    """

    #: Base32, for a user typing it into an authenticator by hand.
    secret: str
    #: `otpauth://totp/...`, for rendering as a QR code.
    provisioning_uri: str


class TokenResponse(BaseModel):
    """A freshly issued token pair. `expires_in` is the access token's lifetime in seconds."""

    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class CurrentUserResponse(BaseModel):
    """What `GET /auth/me` returns.

    No `password_hash`, no `totp_secret`, no counters: this response is handed to a browser, and the
    fields it does not need are fields that cannot leak.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    role: UserRole
    employee_id: uuid.UUID | None
    language: AppLanguage
    is_2fa_enabled: bool

    #: Requirement 1.6. True when the caller's role makes 2FA mandatory and they have not enrolled, in
    #: which case every endpoint outside this router answers 403 until they do. Served here so the
    #: front end can route straight to the enrolment screen rather than inferring the state from a
    #: failed request.
    is_2fa_enrolment_required: bool
    #: True when enrolment should be offered. Also true for the mandatory case, so a client that only
    #: reads this field prompts everyone who ought to be prompted; `is_2fa_enrolment_required` is what
    #: says whether declining is an option.
    is_2fa_enrolment_prompted: bool

    #: True when the caller owes a first-use password change (a non-administrator created with an
    #: administrator-chosen password, or an existing non-admin backfilled by migration 0005). While
    #: true, every endpoint outside this router answers 403 until `POST /auth/change-password`
    #: succeeds. Served here so the front end can route straight to the change screen rather than
    #: inferring the block from a failed request — the same treatment the 2FA flags get. Always false
    #: for the admin role, which is exempt.
    must_change_password: bool

    last_login_at: datetime | None
