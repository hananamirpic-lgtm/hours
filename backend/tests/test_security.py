"""Password hashing and token minting.

The claims here are the ones a reviewer of Requirement 1.3 and 20.8 would want to see proved against
behaviour rather than against the constants that produced it: the stored hash records a cost of at
least 12, a token cannot be used for the wrong purpose, and a token cannot be edited.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.security import (
    ACCESS_TOKEN_TTL,
    BCRYPT_ROUNDS,
    MAX_PASSWORD_BYTES,
    REFRESH_TOKEN_TTL,
    TokenError,
    TokenType,
    bcrypt_cost,
    create_access_token,
    create_refresh_token,
    decode_token,
    dummy_password_hash,
    hash_password,
    verify_password,
)

USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

#: Anchored to the real clock, because decoding validates expiry: a fixed literal would make these
#: tests pass or fail depending on the date they were run. Whole seconds because that is the
#: resolution `iat` and `exp` are encoded at, which keeps the equality assertions exact.
#:
#: Re-anchored before every test by `_anchor_now_to_the_present`, not only at import. A token minted
#: at `NOW` is valid for 15 minutes against the real validation clock; frozen at collection time it
#: would already be expired by the time this module runs after a long earlier phase (the performance
#: suite runs for minutes in a combined invocation). Refreshing per test keeps mint and decode in the
#: same instant, so the "valid token" assertions do not depend on how long earlier tests took.
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture(autouse=True)
def _anchor_now_to_the_present() -> None:
    """Move `NOW` to the real present at the start of each test, so minted tokens are not stale."""
    global NOW
    NOW = datetime.now(UTC).replace(microsecond=0)


# --------------------------------------------------------------------------- passwords


def test_a_password_verifies_against_its_own_hash():
    hashed = hash_password("s3cret-passphrase")
    assert verify_password("s3cret-passphrase", hashed) is True


def test_a_different_password_does_not_verify():
    assert verify_password("wrong", hash_password("s3cret-passphrase")) is False


def test_the_stored_hash_records_a_cost_of_at_least_12():
    """Requirement 1.3, read off the hash rather than off the constant.

    The cost is recorded inside the hash, so this is what a future reader can check against a row in
    the database — and it is what would catch a well-meant change that lowered the constant.
    """
    assert bcrypt_cost(hash_password("s3cret-passphrase")) >= 12
    assert BCRYPT_ROUNDS >= 12


def test_the_same_password_hashes_differently_every_time():
    """A per-hash salt, so two users with the same password do not share a hash."""
    first = hash_password("s3cret-passphrase")
    second = hash_password("s3cret-passphrase")
    assert first != second
    assert verify_password("s3cret-passphrase", second) is True


def test_a_hebrew_password_round_trips():
    """UTF-8 in, UTF-8 out. The interface is Hebrew-first, so this is not an exotic case."""
    password = "סיסמה-חזקה-2026"
    assert verify_password(password, hash_password(password)) is True


@pytest.mark.parametrize("stored", ["", "not-a-hash", "$2b$broken", "$argon2id$v=19$m=65536"])
def test_a_hash_this_cannot_read_is_a_failed_verification_not_an_error(stored: str):
    """Every input here arrived in a request. A 500 on a malformed stored hash would turn a data
    problem into an availability problem, and would tell a caller more than a refusal does."""
    assert verify_password("s3cret-passphrase", stored) is False


def test_hashing_refuses_a_password_bcrypt_would_truncate():
    """bcrypt reads 72 bytes. Accepting more would mean the tail of a long passphrase is decorative,
    which is worse than refusing it, because nobody would know."""
    with pytest.raises(ValueError, match="72 bytes"):
        hash_password("x" * (MAX_PASSWORD_BYTES + 1))


def test_verifying_an_over_long_password_is_a_failure_not_an_error():
    hashed = hash_password("s3cret-passphrase")
    assert verify_password("x" * (MAX_PASSWORD_BYTES + 1), hashed) is False


def test_the_dummy_hash_is_a_real_hash_that_nothing_matches():
    """It stands in for an absent user's hash, so it has to cost what a real one costs."""
    hashed = dummy_password_hash()
    assert bcrypt_cost(hashed) == BCRYPT_ROUNDS
    assert verify_password("", hashed) is False
    assert verify_password("s3cret-passphrase", hashed) is False


# --------------------------------------------------------------------------- tokens


def test_an_access_token_round_trips(settings):
    token = create_access_token(user_id=USER_ID, token_version=4, role="admin", now=NOW)
    claims = decode_token(token, expected_type=TokenType.ACCESS)

    assert claims.subject == USER_ID
    assert claims.token_type is TokenType.ACCESS
    assert claims.token_version == 4
    assert claims.role == "admin"
    assert claims.issued_at == NOW
    assert claims.expires_at == NOW + ACCESS_TOKEN_TTL


def test_an_access_token_lasts_fifteen_minutes(settings):
    claims = decode_token(create_access_token(user_id=USER_ID, token_version=1, now=NOW))
    assert claims.expires_at - claims.issued_at == timedelta(minutes=15)


def test_a_refresh_token_lasts_seven_days_and_names_its_rotation(settings):
    token, rotation_id = create_refresh_token(user_id=USER_ID, token_version=1, now=NOW)
    claims = decode_token(token, expected_type=TokenType.REFRESH)

    assert claims.expires_at - claims.issued_at == REFRESH_TOKEN_TTL == timedelta(days=7)
    assert claims.rotation_id == rotation_id


def test_two_refresh_tokens_have_different_rotation_identities(settings):
    _, first = create_refresh_token(user_id=USER_ID, token_version=1, now=NOW)
    _, second = create_refresh_token(user_id=USER_ID, token_version=1, now=NOW)
    assert first != second


def test_a_refresh_token_is_refused_where_an_access_token_is_required(settings):
    """The one type confusion that matters: a refresh token lives 7 days, so accepting it as an
    access token would hand out a 7-day access token."""
    token, _ = create_refresh_token(user_id=USER_ID, token_version=1, now=NOW)
    with pytest.raises(TokenError):
        decode_token(token, expected_type=TokenType.ACCESS)


def test_an_access_token_is_refused_where_a_refresh_token_is_required(settings):
    token = create_access_token(user_id=USER_ID, token_version=1, now=NOW)
    with pytest.raises(TokenError):
        decode_token(token, expected_type=TokenType.REFRESH)


def test_an_expired_token_is_refused(settings):
    stale = datetime.now(UTC) - ACCESS_TOKEN_TTL - timedelta(minutes=1)
    token = create_access_token(user_id=USER_ID, token_version=1, now=stale)
    with pytest.raises(TokenError):
        decode_token(token)


def test_a_token_still_inside_its_lifetime_is_accepted(settings):
    recent = datetime.now(UTC) - timedelta(minutes=14)
    assert decode_token(create_access_token(user_id=USER_ID, token_version=1, now=recent))


def test_a_token_signed_with_another_key_is_refused(settings):
    """The signature is the only thing standing between a claim and a forged claim."""
    forged = jwt.encode(
        {
            "sub": str(USER_ID),
            "typ": "access",
            "ver": 1,
            "iat": NOW,
            "exp": NOW + ACCESS_TOKEN_TTL,
        },
        "a-different-signing-key-entirely-not-ours",
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        decode_token(forged)


def test_an_edited_token_is_refused(settings):
    token = create_access_token(user_id=USER_ID, token_version=1, now=NOW)
    header, payload, signature = token.split(".")
    with pytest.raises(TokenError):
        decode_token(f"{header}.{payload}.{signature[:-4]}AAAA")


@pytest.mark.parametrize("token", ["", "rubbish", "a.b.c", "Bearer something"])
def test_a_malformed_token_is_refused(settings, token: str):
    with pytest.raises(TokenError):
        decode_token(token)


def test_an_unsigned_token_is_refused(settings):
    """`alg: none` is the oldest JWT trick there is, and the only defence is naming the algorithm."""
    unsigned = jwt.encode(
        {"sub": str(USER_ID), "typ": "access", "ver": 1, "iat": NOW, "exp": NOW + ACCESS_TOKEN_TTL},
        key="",
        algorithm="none",
    )
    with pytest.raises(TokenError):
        decode_token(unsigned)


def test_a_token_missing_a_required_claim_is_refused(settings):
    """Without `ver` there is nothing to compare against the user's token version, so the whole
    immediate-invalidation mechanism would quietly stop applying to that token."""
    secret = settings.jwt_secret_key.get_secret_value()
    incomplete = jwt.encode(
        {"sub": str(USER_ID), "typ": "access", "iat": NOW, "exp": NOW + ACCESS_TOKEN_TTL},
        secret,
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        decode_token(incomplete)


def test_a_refresh_token_without_a_rotation_identity_is_refused(settings):
    """It could never be rotated, so it could never be detected as reused."""
    secret = settings.jwt_secret_key.get_secret_value()
    unrotatable = jwt.encode(
        {"sub": str(USER_ID), "typ": "refresh", "ver": 1, "iat": NOW, "exp": NOW + REFRESH_TOKEN_TTL},
        secret,
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        decode_token(unrotatable, expected_type=TokenType.REFRESH)
