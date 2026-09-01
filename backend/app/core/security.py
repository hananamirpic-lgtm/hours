"""Password hashing and token minting.

Two unrelated primitives live here because both are pure functions over a secret and neither should
know anything about the database. `app.services.auth` composes them; nothing else should reach past
this module to `bcrypt` or `jwt` directly.

**Passwords.** bcrypt, work factor 12 (Requirement 1.3 sets the floor). The cost is a module constant
rather than a setting: it is a property of how the stored hashes were produced, and a deployment that
could lower it through the environment would be a deployment that could accidentally weaken every
password it writes. Raising it later is a code change plus a rehash-on-next-login, which is the
honest amount of work for that decision.

bcrypt reads at most 72 bytes of the password and version 4 refuses anything longer rather than
truncating silently. That refusal is kept — a password whose tail is ignored is a password that is
shorter than the user believes — so `hash_password` raises and `verify_password` returns false, the
latter because an over-long value arriving at a login endpoint is a failed attempt, not a server
error.

**Tokens.** Access tokens last 15 minutes, refresh tokens 7 days, as the design specifies. Both carry
`ver`, the user's `token_version`, which is what lets a deactivation invalidate tokens already issued
without the server keeping session state. Refresh tokens additionally carry `jti`, a per-token
rotation identity: rotation replaces one refresh token with another, and the identity is what a reuse
event can be reported against.

Signing is HS256 with the configured `JWT_SECRET_KEY`. Symmetric because one service both mints and
verifies; the moment a second service needs to verify without being able to mint, this becomes RS256
and the token shape does not have to change.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import bcrypt
import jwt

from app.core.config import get_settings

# Requirement 1.3 sets 12 as the floor; 12 is also where the cost is currently tolerable at login.
BCRYPT_ROUNDS = 12

#: bcrypt's own limit, not ours.
MAX_PASSWORD_BYTES = 72

JWT_ALGORITHM = "HS256"

#: Short enough that a stolen access token is useful only briefly, long enough that a working session
#: does not spend its time refreshing.
ACCESS_TOKEN_TTL = timedelta(minutes=15)
#: The outer bound on an idle session: after this the user signs in again (Requirement 1.8).
REFRESH_TOKEN_TTL = timedelta(days=7)

#: Tolerance for clock skew between this process and whatever issued the token. Kept small: the only
#: issuer is this service, so the allowance is for host clock drift, not for a third party.
LEEWAY_SECONDS = 10


class TokenType(enum.StrEnum):
    """What a token may be presented for. Carried as `typ` and always checked."""

    ACCESS = "access"
    REFRESH = "refresh"


class TokenError(Exception):
    """A token was absent, malformed, expired, signed with another key, or of the wrong type.

    One exception for all of them on purpose. Telling a caller which of those went wrong is how a
    token oracle gets built, and no legitimate client can act differently on the distinction.
    """


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The claims this service puts in a token, decoded and typed."""

    subject: uuid.UUID
    token_type: TokenType
    token_version: int
    issued_at: datetime
    expires_at: datetime
    #: Rotation identity. Present on refresh tokens, absent on access tokens.
    rotation_id: uuid.UUID | None = None
    role: str | None = None


# --------------------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    """Return a bcrypt hash at `BCRYPT_ROUNDS`.

    Raises `ValueError` for a password bcrypt would silently truncate. This is a programming-time or
    administration-time error — a create-user form validates length before it gets here — so it is
    loud rather than swallowed.
    """
    encoded = _encode_password(password)
    if encoded is None:
        raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes when UTF-8 encoded")
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Whether `password` matches `password_hash`. False for any hash this cannot read.

    Never raises. Every input here came from a request, and a malformed stored hash or an over-long
    submitted password is a failed authentication, not a 500.
    """
    encoded = _encode_password(password)
    if encoded is None:
        return False
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("ascii"))
    except (ValueError, AttributeError, UnicodeEncodeError):
        # `ValueError` for a hash that is not bcrypt; the others for a hash that is not even a
        # plausible string. All mean the same thing to a caller: this password does not match.
        return False


def bcrypt_cost(password_hash: str) -> int:
    """Work factor recorded in a bcrypt hash, read from its `$2b$NN$` prefix.

    Exists so the cost floor can be asserted against a hash rather than against the constant that
    produced it, which would only prove the constant equals itself.
    """
    parts = password_hash.split("$")
    if len(parts) < 4 or not parts[1].startswith("2"):
        raise ValueError("not a bcrypt hash")
    return int(parts[2])


@lru_cache(maxsize=1)
def dummy_password_hash() -> str:
    """A hash of a value no password equals, verified against when the username does not exist.

    Requirement 1.2 asks for a failure message that does not reveal whether a username exists, and a
    message is only half of it: skipping the hash comparison for an unknown username makes that
    response arrive an order of magnitude sooner, which answers the same question through a
    stopwatch. Verifying against this hash spends the same time either way.

    Cached because it costs a full bcrypt round to produce and its value is irrelevant — only its
    cost is. Randomised per process so it cannot be mistaken for a usable credential.
    """
    return hash_password(uuid.uuid4().hex + uuid.uuid4().hex[:8])


def _encode_password(password: str) -> bytes | None:
    """UTF-8 bytes of a password, or `None` if bcrypt would not accept them."""
    if not isinstance(password, str):
        return None
    encoded = password.encode("utf-8")
    return None if len(encoded) > MAX_PASSWORD_BYTES else encoded


# --------------------------------------------------------------------------- tokens


def create_access_token(
    *,
    user_id: uuid.UUID,
    token_version: int,
    role: str | None = None,
    now: datetime | None = None,
) -> str:
    """Mint a 15-minute access token.

    `role` rides along so a permission check does not need a database read on every request. It is
    advisory: the authoritative role is the column, and the token dies within 15 minutes of a role
    change either way.
    """
    issued_at = now or _utcnow()
    return _encode(
        {
            "sub": str(user_id),
            "typ": TokenType.ACCESS.value,
            "ver": token_version,
            "role": role,
            "iat": issued_at,
            "exp": issued_at + ACCESS_TOKEN_TTL,
        }
    )


def create_refresh_token(
    *,
    user_id: uuid.UUID,
    token_version: int,
    rotation_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> tuple[str, uuid.UUID]:
    """Mint a 7-day refresh token, returning it with its rotation identity.

    The identity is returned rather than left to be dug out of the token, because the caller records
    it in the audit log; a rotation that cannot be named is a rotation that cannot be reported.
    """
    issued_at = now or _utcnow()
    identity = rotation_id or uuid.uuid4()
    token = _encode(
        {
            "sub": str(user_id),
            "typ": TokenType.REFRESH.value,
            "ver": token_version,
            "jti": str(identity),
            "iat": issued_at,
            "exp": issued_at + REFRESH_TOKEN_TTL,
        }
    )
    return token, identity


def decode_token(token: str, *, expected_type: TokenType | None = None) -> TokenClaims:
    """Verify a token's signature and expiry and return its claims.

    Raises `TokenError` for anything else, including a refresh token presented where an access token
    was required — which is the mistake worth catching, since a refresh token lives 7 days and would
    otherwise act as a very long-lived access token.
    """
    secret = get_settings().jwt_secret_key.get_secret_value()
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            leeway=LEEWAY_SECONDS,
            options={"require": ["sub", "typ", "ver", "iat", "exp"]},
        )
    except jwt.PyJWTError as error:
        raise TokenError("token is not valid") from error

    try:
        token_type = TokenType(payload["typ"])
        claims = TokenClaims(
            subject=uuid.UUID(payload["sub"]),
            token_type=token_type,
            token_version=int(payload["ver"]),
            issued_at=datetime.fromtimestamp(payload["iat"], UTC),
            expires_at=datetime.fromtimestamp(payload["exp"], UTC),
            rotation_id=uuid.UUID(payload["jti"]) if payload.get("jti") else None,
            role=payload.get("role"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TokenError("token claims are not valid") from error

    if expected_type is not None and claims.token_type is not expected_type:
        raise TokenError("token is not valid")
    if claims.token_type is TokenType.REFRESH and claims.rotation_id is None:
        # A refresh token without a rotation identity cannot participate in rotation, so accepting it
        # would create a token that never rotates and never gets detected as reused.
        raise TokenError("token is not valid")
    return claims


def _encode(payload: dict[str, object]) -> str:
    secret = get_settings().jwt_secret_key.get_secret_value()
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def _utcnow() -> datetime:
    return datetime.now(UTC)
