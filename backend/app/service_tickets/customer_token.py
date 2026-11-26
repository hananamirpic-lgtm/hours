"""The customer token: minting, decoding, and the "is this a customer token" question.

A customer is not a row in `users`, so a customer's credential must never be usable as a staff
credential, and a staff credential must never be usable as a customer's. Three independent
properties keep the first true, and any one of them is enough on its own:

1. **Different key.** The signing key is `HMAC-SHA256(JWT_SECRET_KEY, LABEL)`, not the raw secret
   that `app.core.security.decode_token` verifies with.
2. **Different `typ`.** The claim is `"customer"`; the staff decoder only knows `access` and
   `refresh`.
3. **Different `aud`.** A token carrying an `aud` claim is refused by PyJWT when the caller passes
   no audience, which is how `decode_token` calls it.

A staff token fails here for the first two reasons: wrong key, wrong `typ`.

This module imports only `app.core.config`, `jwt`, `hmac`, `hashlib` and the standard library. It
imports nothing from `app.api`, because `app/api/deps.py` imports this module (a cycle otherwise).
It reads no database and does not look at the feature flag: `is_valid_customer_token` answers the
same thing whatever the flag says and whether or not the customer is still active.

There is no refresh token. When this one expires the customer signs in again.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt

from app.core.config import get_settings

#: Domain-separation label for the key derivation. The `v1` is the version of the derivation, not of
#: any token: changing it invalidates every outstanding customer token at once.
KEY_DERIVATION_LABEL = b"hours/service-tickets/customer-token/v1"

JWT_ALGORITHM = "HS256"

TOKEN_TYPE = "customer"
AUDIENCE = "hours-service-customer"

#: Eight hours. Callers pass the configured value; this is what they get when they do not.
DEFAULT_CUSTOMER_TOKEN_TTL_SECONDS = 28800

#: No tolerance. The only issuer is this service, and a customer token is short-lived enough that a
#: second of grace would be a larger share of its life than it is worth.
LEEWAY_SECONDS = 0

#: One code for every failure, on purpose. Telling a caller whether a token was expired, forged or
#: of the wrong kind is how a token oracle gets built (the same reasoning as `TokenError`).
CODE_INVALID_TOKEN = "invalid_token"

_REQUIRED_CLAIMS = ["sub", "typ", "aud", "ver", "jti", "iat", "exp"]


class CustomerTokenError(Exception):
    """A customer token was absent, malformed, expired, mis-signed, or of the wrong kind."""

    def __init__(self, message: str = "token is not valid", *, code: str = CODE_INVALID_TOKEN) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CustomerTokenClaims:
    """What a verified customer token says. The row behind `customer_id` is not consulted here."""

    customer_id: uuid.UUID
    version: int
    expires_at: datetime


def mint_customer_token(
    customer_id: uuid.UUID,
    version: int,
    now: datetime | None = None,
    ttl: int = DEFAULT_CUSTOMER_TOKEN_TTL_SECONDS,
) -> str:
    """Mint a customer token that expires `ttl` seconds after `now`.

    `version` is the customer's `token_version`; bumping it on the row is what invalidates tokens
    already issued. `iat` and `exp` are whole seconds, the resolution a JWT encodes them at.
    """
    issued_at = int((now or datetime.now(UTC)).timestamp())
    payload = {
        "sub": str(customer_id),
        "typ": TOKEN_TYPE,
        "aud": AUDIENCE,
        "ver": version,
        "jti": uuid.uuid4().hex,
        "iat": issued_at,
        "exp": issued_at + int(ttl),
    }
    return jwt.encode(payload, _signing_key(), algorithm=JWT_ALGORITHM)


def decode_customer_token(token: str) -> CustomerTokenClaims:
    """Verify signature, expiry, `typ` and `aud`, and return the claims.

    Raises `CustomerTokenError` for anything else, including a staff token.
    """
    try:
        payload = jwt.decode(
            token,
            _signing_key(),
            algorithms=[JWT_ALGORITHM],
            audience=AUDIENCE,
            leeway=LEEWAY_SECONDS,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as error:
        raise CustomerTokenError() from error

    if payload.get("typ") != TOKEN_TYPE:
        raise CustomerTokenError()

    version = payload["ver"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise CustomerTokenError()
    try:
        return CustomerTokenClaims(
            customer_id=uuid.UUID(payload["sub"]),
            version=version,
            expires_at=datetime.fromtimestamp(payload["exp"], UTC),
        )
    except (TypeError, ValueError, AttributeError, OverflowError, OSError) as error:
        raise CustomerTokenError() from error


def is_valid_customer_token(token: object) -> bool:
    """Whether `token` is a currently valid customer token. Never raises.

    It reads no database and ignores the feature flag, so the answer does not depend on either, nor
    on whether the customer is still active. It exists to let the Hours-side auth failure path answer
    403 instead of 401 for a customer's token. Input that is not a string, an empty string and an
    enormous string are all simply "no".
    """
    if not isinstance(token, str):
        return False
    try:
        decode_customer_token(token)
    except Exception:  # noqa: BLE001 - the contract is "never raises", whatever went wrong
        return False
    return True


def _signing_key() -> bytes:
    """`HMAC-SHA256(JWT_SECRET_KEY, LABEL)`, derived on each call and never stored."""
    secret = get_settings().jwt_secret_key.get_secret_value()
    return hmac.new(secret.encode("utf-8"), KEY_DERIVATION_LABEL, hashlib.sha256).digest()
