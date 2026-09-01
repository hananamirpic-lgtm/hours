"""Encryption primitives for the sensitive columns.

Two operations are needed, and they are not the same operation:

* **Encryption** protects a value at rest and must be non-deterministic — encrypting the same
  passport number twice has to produce two different ciphertexts, otherwise the column leaks
  equality and anyone with read access can count how many employees share a value.
* **Deterministic hashing** is what makes a protected value unique or searchable. The passport
  number has a partial unique index on it (Requirement 3.3), and an index cannot work over a value
  that changes every time it is written. So the same value always yields the same digest, and the
  index enforces uniqueness without the plaintext ever being stored.

Both keys are derived from one configured secret (`ENCRYPTION_KEY`) through HKDF with distinct
`info` labels. Deriving rather than using the secret directly means the configured value may be any
sufficiently long string instead of exactly 32 bytes, and the encryption key and the HMAC key are
cryptographically independent even though one secret produced them — reusing a single key for both
would let a chosen-plaintext attack on one weaken the other.

Every stored value carries a scheme prefix (`gcm1:`, `hmac1:`). When a key or an algorithm has to
change, the prefix is what tells a migration which rows are in the old scheme; without it the only
way to find out is to try and see what fails.
"""

from __future__ import annotations

import base64
import hmac
import os
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import get_settings

# Versioned so a future scheme is distinguishable from this one on sight.
CIPHER_PREFIX = "gcm1:"
HASH_PREFIX = "hmac1:"

# AES-256, so 32 bytes of key material for the cipher and the same for HMAC-SHA256.
KEY_LENGTH_BYTES = 32
# 96 bits is the nonce size AES-GCM is specified and optimised for; anything else forces an extra
# derivation step inside the cipher and buys nothing.
NONCE_LENGTH_BYTES = 12

# Domain separation for the two derived keys. Changing either string changes every value already
# stored under it, so these are effectively frozen.
_CIPHER_KEY_INFO = b"hours-api/column-encryption/v1"
_HASH_KEY_INFO = b"hours-api/deterministic-hash/v1"


class DecryptionError(Exception):
    """A stored value could not be decrypted.

    Raised for a wrong key, a corrupted ciphertext and a tampered one alike. The cases are
    deliberately not distinguished: telling a caller *why* authentication failed is how padding and
    tag oracles get built.
    """


def _derive_key(secret: bytes, info: bytes) -> bytes:
    # No salt: the input is a high-entropy configured secret, not a password, and a random salt
    # would have to be stored alongside every value to be usable. HKDF's `info` provides the
    # separation that matters here.
    return HKDF(algorithm=hashes.SHA256(), length=KEY_LENGTH_BYTES, salt=None, info=info).derive(secret)


class Encryptor:
    """Encrypt, decrypt and deterministically hash column values.

    Instances are cheap to hold and safe to share: `AESGCM` is stateless once constructed, and the
    nonce is drawn per call.
    """

    __slots__ = ("_cipher", "_hash_key")

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("encryption secret must not be empty")
        secret_bytes = secret.encode("utf-8")
        self._cipher = AESGCM(_derive_key(secret_bytes, _CIPHER_KEY_INFO))
        self._hash_key = _derive_key(secret_bytes, _HASH_KEY_INFO)

    # ------------------------------------------------------------------ encryption
    def encrypt(self, plaintext: str) -> str:
        """Return `gcm1:<base64url(nonce || ciphertext || tag)>`.

        The nonce travels with the ciphertext because decryption needs it and it is not secret. GCM
        appends its authentication tag to the ciphertext, so a modified value fails to decrypt
        rather than decrypting to rubbish.
        """
        if not isinstance(plaintext, str):
            raise TypeError(f"plaintext must be str, got {type(plaintext).__name__}")
        nonce = os.urandom(NONCE_LENGTH_BYTES)
        sealed = self._cipher.encrypt(nonce, plaintext.encode("utf-8"), None)
        return CIPHER_PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")

    def decrypt(self, stored: str) -> str:
        """Reverse `encrypt`. Raises `DecryptionError` for anything this key did not produce."""
        if not isinstance(stored, str) or not stored.startswith(CIPHER_PREFIX):
            raise DecryptionError("value is not in the expected encryption scheme")
        try:
            raw = base64.urlsafe_b64decode(stored[len(CIPHER_PREFIX) :].encode("ascii"))
        except (ValueError, UnicodeEncodeError) as error:
            raise DecryptionError("value is not valid base64") from error
        if len(raw) <= NONCE_LENGTH_BYTES:
            raise DecryptionError("value is too short to contain a nonce and a tag")
        nonce, sealed = raw[:NONCE_LENGTH_BYTES], raw[NONCE_LENGTH_BYTES:]
        try:
            return self._cipher.decrypt(nonce, sealed, None).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as error:
            raise DecryptionError("value failed authentication or is not valid UTF-8") from error

    @staticmethod
    def is_encrypted(value: str) -> bool:
        return isinstance(value, str) and value.startswith(CIPHER_PREFIX)

    # ------------------------------------------------------------------ deterministic hashing
    def deterministic_hash(self, value: str) -> str:
        """Return `hmac1:<hex>` — stable for the same key and normalised input.

        Keyed rather than a plain digest: a passport number is short and drawn from a guessable
        space, so an unkeyed SHA-256 column would be trivially reversible by enumeration. Without
        the key an attacker holding the column can neither reverse a digest nor build a rainbow
        table for it.
        """
        digest = hmac.new(self._hash_key, normalise_for_hash(value).encode("utf-8"), "sha256")
        return HASH_PREFIX + digest.hexdigest()

    @staticmethod
    def is_hashed(value: str) -> bool:
        return isinstance(value, str) and value.startswith(HASH_PREFIX)


def normalise_for_hash(value: str) -> str:
    """Canonical form used before hashing.

    Case and surrounding whitespace must not decide whether two passport numbers are the same one,
    or `a1234567` typed on a Monday defeats the uniqueness index against `A1234567` typed on the
    Tuesday. Interior spacing is left alone: it can be meaningful in an identifier, and stripping it
    would make two genuinely different values collide.
    """
    if not isinstance(value, str):
        raise TypeError(f"value must be str, got {type(value).__name__}")
    return value.strip().upper()


@lru_cache
def get_encryptor() -> Encryptor:
    """Process-wide encryptor, following the same singleton pattern as the engine and settings.

    Cached because HKDF should run once per process, not once per row read. A test that needs a
    different key builds `Encryptor(...)` directly, or clears this cache along with
    `get_settings.cache_clear()`; the two caches are independent and a stale one here would keep
    decrypting with the previous key.
    """
    return Encryptor(get_settings().encryption_key.get_secret_value())
