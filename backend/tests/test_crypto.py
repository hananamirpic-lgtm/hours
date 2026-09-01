"""Encryption primitive tests.

The claims worth pinning down are the ones a later refactor could quietly break: a value survives a
round trip, the same value does not produce the same ciphertext twice, a tampered ciphertext is
rejected rather than decrypted to rubbish, and a deterministic hash is stable — because a unique
index built on an unstable hash silently stops enforcing anything.
"""

from __future__ import annotations

import base64

import pytest

from app.core.crypto import (
    CIPHER_PREFIX,
    HASH_PREFIX,
    NONCE_LENGTH_BYTES,
    DecryptionError,
    Encryptor,
    get_encryptor,
    normalise_for_hash,
)

SECRET = "unit-test-secret-key-of-sufficient-length"
OTHER_SECRET = "a-different-secret-key-of-sufficient-length"

PLAINTEXTS = [
    "A1234567",  # a passport number
    "+972-50-123-4567",  # a phone number
    "רחוב הרצל 15, תל אביב",  # Hebrew, so UTF-8 is exercised
    "1985-03-17",
    "x" * 4096,  # long enough to cross any buffer boundary
    " leading and trailing ",  # whitespace must survive encryption untouched
]


@pytest.fixture
def encryptor() -> Encryptor:
    return Encryptor(SECRET)


# --------------------------------------------------------------------------- encryption


@pytest.mark.parametrize("plaintext", PLAINTEXTS)
def test_round_trip_returns_the_original_value(encryptor: Encryptor, plaintext: str):
    assert encryptor.decrypt(encryptor.encrypt(plaintext)) == plaintext


def test_empty_string_round_trips(encryptor: Encryptor):
    """An empty string is a value, not a missing one; `None` is what missing looks like."""
    assert encryptor.decrypt(encryptor.encrypt("")) == ""


def test_ciphertext_carries_the_scheme_prefix(encryptor: Encryptor):
    assert encryptor.encrypt("A1234567").startswith(CIPHER_PREFIX)


def test_ciphertext_does_not_contain_the_plaintext(encryptor: Encryptor):
    assert "A1234567" not in encryptor.encrypt("A1234567")


def test_same_plaintext_encrypts_differently_each_time(encryptor: Encryptor):
    """Non-determinism is the point: an equal ciphertext would leak an equal plaintext."""
    ciphertexts = {encryptor.encrypt("A1234567") for _ in range(20)}
    assert len(ciphertexts) == 20


def test_nonce_is_not_reused(encryptor: Encryptor):
    """A repeated nonce under one AES-GCM key is a total loss of confidentiality, not a weakening."""
    nonces = {
        base64.urlsafe_b64decode(encryptor.encrypt("A1234567")[len(CIPHER_PREFIX) :])[:NONCE_LENGTH_BYTES]
        for _ in range(50)
    }
    assert len(nonces) == 50


def test_another_key_cannot_decrypt(encryptor: Encryptor):
    with pytest.raises(DecryptionError):
        Encryptor(OTHER_SECRET).decrypt(encryptor.encrypt("A1234567"))


def test_tampered_ciphertext_is_rejected(encryptor: Encryptor):
    """GCM authenticates; a flipped bit must fail loudly rather than yield a different value."""
    sealed = encryptor.encrypt("A1234567")
    raw = bytearray(base64.urlsafe_b64decode(sealed[len(CIPHER_PREFIX) :]))
    raw[-1] ^= 0x01
    tampered = CIPHER_PREFIX + base64.urlsafe_b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(DecryptionError):
        encryptor.decrypt(tampered)


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "A1234567",  # plaintext that was never encrypted
        "gcm1:not-valid-base64!!",
        CIPHER_PREFIX + base64.urlsafe_b64encode(b"short").decode("ascii"),
        "gcm2:" + base64.urlsafe_b64encode(b"x" * 40).decode("ascii"),  # unknown scheme
    ],
)
def test_unusable_stored_values_raise_decryption_error(encryptor: Encryptor, stored: str):
    with pytest.raises(DecryptionError):
        encryptor.decrypt(stored)


def test_encrypt_rejects_a_non_string(encryptor: Encryptor):
    with pytest.raises(TypeError):
        encryptor.encrypt(1985)  # type: ignore[arg-type]


def test_empty_secret_is_refused():
    with pytest.raises(ValueError, match="must not be empty"):
        Encryptor("")


def test_is_encrypted_recognises_only_its_own_scheme(encryptor: Encryptor):
    assert Encryptor.is_encrypted(encryptor.encrypt("A1234567"))
    assert not Encryptor.is_encrypted("A1234567")


# --------------------------------------------------------------------------- deterministic hashing


def test_hash_is_stable_across_calls(encryptor: Encryptor):
    """The uniqueness index depends on this. If it drifts, duplicates stop being detected."""
    digests = {encryptor.deterministic_hash("A1234567") for _ in range(20)}
    assert len(digests) == 1


def test_hash_is_stable_across_instances():
    """A restarted process, or a second worker, must agree with the digests already in the table."""
    assert Encryptor(SECRET).deterministic_hash("A1234567") == Encryptor(SECRET).deterministic_hash(
        "A1234567"
    )


def test_hash_carries_the_scheme_prefix_and_a_sha256_digest(encryptor: Encryptor):
    digest = encryptor.deterministic_hash("A1234567")
    assert digest.startswith(HASH_PREFIX)
    assert len(digest.removeprefix(HASH_PREFIX)) == 64
    assert Encryptor.is_hashed(digest)


def test_hash_distinguishes_different_values(encryptor: Encryptor):
    assert encryptor.deterministic_hash("A1234567") != encryptor.deterministic_hash("A1234568")


@pytest.mark.parametrize("variant", ["a1234567", " A1234567 ", "\tA1234567\n", "A1234567"])
def test_hash_normalises_case_and_surrounding_whitespace(encryptor: Encryptor, variant: str):
    """Otherwise the same passport typed two ways slips past the uniqueness index."""
    assert encryptor.deterministic_hash(variant) == encryptor.deterministic_hash("A1234567")


def test_hash_preserves_interior_spacing(encryptor: Encryptor):
    """Interior spacing can be part of an identifier; collapsing it would merge distinct values."""
    assert encryptor.deterministic_hash("A123 4567") != encryptor.deterministic_hash("A1234567")


def test_hash_is_keyed_so_another_key_gives_another_digest(encryptor: Encryptor):
    assert encryptor.deterministic_hash("A1234567") != Encryptor(OTHER_SECRET).deterministic_hash("A1234567")


def test_cipher_and_hash_keys_are_independent(encryptor: Encryptor):
    """One configured secret, two derived keys. The digest must not be recoverable from the key
    material used for encryption, so HKDF's domain separation is what is being checked."""
    digest = encryptor.deterministic_hash("A1234567").removeprefix(HASH_PREFIX)
    sealed = encryptor.encrypt("A1234567").removeprefix(CIPHER_PREFIX)
    assert digest not in sealed


def test_normalise_rejects_a_non_string():
    with pytest.raises(TypeError):
        normalise_for_hash(None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- wiring


def test_configured_encryptor_uses_the_configured_key(settings):
    """The application's encryptor must come from settings, not from a literal somewhere."""
    get_encryptor.cache_clear()
    try:
        configured = get_encryptor()
        expected = Encryptor(settings.encryption_key.get_secret_value())
        assert configured.decrypt(expected.encrypt("A1234567")) == "A1234567"
        assert configured.deterministic_hash("A1234567") == expected.deterministic_hash("A1234567")
    finally:
        get_encryptor.cache_clear()


def test_configured_encryptor_is_a_singleton():
    assert get_encryptor() is get_encryptor()
