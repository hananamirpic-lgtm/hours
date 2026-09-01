"""Signed, versioned site QR tokens (Requirement 8.4, 8.6, 8.7).

A site's QR payload has to be an *opaque signed token*, not the site number written plainly. Knowing
a site's number must not be enough to forge a code for it (Requirement 8.4), so the site identity
travels inside a payload sealed with a keyed signature: a client can read the identity but cannot
produce a token the server will accept without the signing key.

The token also carries a **version**. Regenerating a site's QR bumps `sites.qr_token_version`
(Requirement 8.6); the verifier is handed the version currently on the row and rejects any token
whose version does not match it. That is what makes a previously printed code stop working the moment
it is rotated: the old token is well-formed and correctly signed, but it names a version the site has
moved past, so it is *revoked* rather than *forged*, and the two are reported the same way — a scan
never learns why a token was refused (Requirement 8.7), only that it was.

For a site in **separate** check-in / check-out mode (Requirement 8.3) the token additionally carries
its `action`. The two printed codes then differ only in that field, and the scan path reads the
action from the token rather than inferring it from the employee's state. A **unified** site
(Requirement 8.2) mints one token with no action, and the server decides check-in versus check-out.

Wire format, deliberately compact so it fits comfortably in a QR at a low error-correction burden:

    site:<base64url(payload)>.<base64url(hmac-sha256(payload))>

The payload is `v1|<site_id>|<version>|<action>` with `action` empty for a unified token. The `site:`
prefix and the `v1` scheme marker are there so a future format is distinguishable from this one on
sight, the same reasoning as the `gcm1:`/`hmac1:` prefixes in `app.core.crypto`.

The signing key is `JWT_SECRET_KEY`, run through HKDF with a QR-specific label so the QR HMAC key is
cryptographically independent of the one that signs access tokens — the same secret produces both,
but a weakness in the use of one cannot bear on the other.
"""

from __future__ import annotations

import base64
import enum
import hmac
import uuid
from dataclasses import dataclass
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import get_settings

#: Marks the wire scheme, so a later format is recognisable and this one can be rejected cleanly.
TOKEN_PREFIX = "site:"
SCHEME = "v1"

# Domain separation from the access-token signing key. Frozen: changing it invalidates every token
# already printed, which is exactly the effect a rotation is supposed to have deliberately, not one
# to trigger by accident.
_QR_KEY_INFO = b"hours-api/qr-token-signing/v1"
_QR_KEY_LENGTH_BYTES = 32


class QrAction(enum.StrEnum):
    """The action a separate-mode token carries (Requirement 8.3).

    A unified token carries none; these values appear only on the two codes a separate-mode site
    prints, and the scan path trusts the action on the token rather than the employee's state.
    """

    CHECK_IN = "check_in"
    CHECK_OUT = "check_out"


@dataclass(frozen=True, slots=True)
class SiteToken:
    """A verified token's contents: which site, which version, and (separate mode) which action."""

    site_id: uuid.UUID
    version: int
    action: QrAction | None = None


class InvalidToken(Exception):
    """A token was malformed, unknown, wrongly signed, or names a version the site has moved past.

    One exception for every cause on purpose, mirroring `app.core.security.TokenError`: telling a
    scanner *why* a token failed is how a forgery oracle is built, and no honest client needs the
    distinction. Requirement 8.7 asks only that the scan be refused with a clear message and no time
    entry created; the message is the caller's to phrase.
    """


def _b64encode(raw: bytes) -> str:
    """URL-safe base64 without padding, so the token carries no `=` that a QR or URL must escape."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    """Reverse `_b64encode`, restoring the stripped padding. Raises `ValueError` on bad input."""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


@lru_cache
def _signing_key() -> bytes:
    """The QR HMAC key, derived once per process from `JWT_SECRET_KEY` via HKDF.

    Cached for the same reason `app.core.crypto.get_encryptor` is: the derivation should run once,
    not per token. A test that swaps the secret clears this alongside `get_settings.cache_clear()`.
    """
    secret = get_settings().jwt_secret_key.get_secret_value().encode("utf-8")
    return HKDF(
        algorithm=hashes.SHA256(), length=_QR_KEY_LENGTH_BYTES, salt=None, info=_QR_KEY_INFO
    ).derive(secret)


def _payload_bytes(site_id: uuid.UUID, version: int, action: QrAction | None) -> bytes:
    """The signed body: `v1|<site_id>|<version>|<action>`, action empty when unified.

    Every field is inside the signature, so none of them can be altered — not the site, not the
    version, not the action — without the HMAC failing.
    """
    action_part = action.value if action is not None else ""
    return f"{SCHEME}|{site_id}|{version}|{action_part}".encode()


def _sign(payload: bytes) -> bytes:
    return hmac.new(_signing_key(), payload, "sha256").digest()


def mint(site_id: uuid.UUID, version: int, *, action: QrAction | None = None) -> str:
    """Produce a signed token for a site at a given version (Requirement 8.4).

    `action` is set only for the two codes a separate-mode site prints; a unified site mints one
    token with `action=None`. The returned string is what a QR image encodes and what a scan
    presents back.
    """
    if version < 1:
        raise ValueError("version must be at least 1")
    payload = _payload_bytes(site_id, version, action)
    return f"{TOKEN_PREFIX}{_b64encode(payload)}.{_b64encode(_sign(payload))}"


def parse(token: str) -> SiteToken:
    """Verify a token's signature and structure and return its contents.

    Checks, in order: the scheme prefix, the two-part shape, valid base64 in each part, a signature
    that matches the payload (compared in constant time), and a payload that parses into a site id, a
    positive version and a known-or-absent action. Any failure is one `InvalidToken` — the caller
    cannot tell a forged signature from a mistyped character, which is deliberate.

    This does **not** check the version against the site: whether the token is *current* depends on
    the row, which this pure function does not see. `verify` layers that on top; a caller that only
    needs to know the token is well-formed and correctly signed (for example, to look the site up)
    uses `parse`.
    """
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        raise InvalidToken("token is not in the expected scheme")
    body = token[len(TOKEN_PREFIX) :]
    payload_part, _, signature_part = body.partition(".")
    if not payload_part or not signature_part:
        raise InvalidToken("token is not well-formed")

    try:
        payload = _b64decode(payload_part)
        signature = _b64decode(signature_part)
    except (ValueError, UnicodeDecodeError) as error:
        raise InvalidToken("token is not valid base64") from error

    # Constant-time comparison: a byte-by-byte early return would leak, through timing, how much of a
    # forged signature was correct, which is enough to reconstruct one.
    if not hmac.compare_digest(signature, _sign(payload)):
        raise InvalidToken("token signature does not verify")

    return _decode_payload(payload)


def verify(token: str, *, expected_version: int) -> SiteToken:
    """Verify a token and confirm it is the site's current version (Requirement 8.6, 8.7).

    `expected_version` is `sites.qr_token_version` read from the row. A token whose version is behind
    it was minted before the last regeneration and is *revoked*; it is refused exactly as a forged
    one is, because a scanner is never told which it was. This is the check the scan path calls once
    it has resolved the token to a site and loaded the row.
    """
    parsed = parse(token)
    if parsed.version != expected_version:
        raise InvalidToken("token version is not current")
    return parsed


def _decode_payload(payload: bytes) -> SiteToken:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InvalidToken("token payload is not valid UTF-8") from error

    parts = text.split("|")
    if len(parts) != 4 or parts[0] != SCHEME:
        raise InvalidToken("token payload is not well-formed")
    _scheme, site_part, version_part, action_part = parts

    try:
        site_id = uuid.UUID(site_part)
    except ValueError as error:
        raise InvalidToken("token payload has no valid site") from error

    try:
        version = int(version_part)
    except ValueError as error:
        raise InvalidToken("token payload has no valid version") from error
    if version < 1:
        raise InvalidToken("token payload version is not positive")

    action: QrAction | None
    if action_part == "":
        action = None
    else:
        try:
            action = QrAction(action_part)
        except ValueError as error:
            raise InvalidToken("token payload has an unknown action") from error

    return SiteToken(site_id=site_id, version=version, action=action)
