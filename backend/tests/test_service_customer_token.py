"""The customer token: what it is, and the three independent reasons it cannot pass as a staff token.

Each of the three separations (key, `typ`, `aud`) is proved on its own, by minting a token that
differs from a valid staff token in that one respect only. A test that changed two things at once
would still pass if one of the separations were removed.
"""

from __future__ import annotations

import ast
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest

from app.core.security import TokenError, create_access_token, decode_token
from app.service_tickets import customer_token
from app.service_tickets.customer_token import (
    AUDIENCE,
    DEFAULT_CUSTOMER_TOKEN_TTL_SECONDS,
    CustomerTokenError,
    decode_customer_token,
    is_valid_customer_token,
    mint_customer_token,
)

CUSTOMER_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture
def now() -> datetime:
    """The real present in whole seconds: decoding validates expiry against the real clock."""
    return datetime.now(UTC).replace(microsecond=0)


def _staff_secret(settings) -> str:
    return settings.jwt_secret_key.get_secret_value()


def _customer_claims(now: datetime, **overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "sub": str(CUSTOMER_ID),
        "typ": "customer",
        "aud": AUDIENCE,
        "ver": 1,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=8)).timestamp()),
    }
    claims.update(overrides)
    return claims


def _b64(data: dict[str, object] | bytes) -> str:
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# --------------------------------------------------------------------------- round trip


def test_a_customer_token_round_trips(settings, now):
    token = mint_customer_token(CUSTOMER_ID, 7, now)
    claims = decode_customer_token(token)

    assert claims.customer_id == CUSTOMER_ID
    assert isinstance(claims.customer_id, uuid.UUID)
    assert claims.version == 7
    assert is_valid_customer_token(token) is True


def test_the_token_carries_the_documented_claims(settings, now):
    token = mint_customer_token(CUSTOMER_ID, 3, now)
    payload = jwt.decode(token, options={"verify_signature": False})

    assert payload["sub"] == str(CUSTOMER_ID)
    assert payload["typ"] == "customer"
    assert payload["aud"] == "hours-service-customer"
    assert payload["ver"] == 3
    assert payload["jti"]
    assert payload["iat"] == int(now.timestamp())
    assert jwt.get_unverified_header(token)["alg"] == "HS256"


def test_the_token_expires_exactly_eight_hours_after_issue_by_default(settings, now):
    token = mint_customer_token(CUSTOMER_ID, 1, now)
    payload = jwt.decode(token, options={"verify_signature": False})

    assert DEFAULT_CUSTOMER_TOKEN_TTL_SECONDS == 28800
    assert payload["exp"] - payload["iat"] == 28800 == int(timedelta(hours=8).total_seconds())
    assert decode_customer_token(token).expires_at == now + timedelta(hours=8)


def test_a_caller_supplied_ttl_is_used(settings, now):
    token = mint_customer_token(CUSTOMER_ID, 1, now, 600)
    assert decode_customer_token(token).expires_at == now + timedelta(seconds=600)


def test_two_tokens_for_one_customer_have_different_identities(settings, now):
    first = jwt.decode(mint_customer_token(CUSTOMER_ID, 1, now), options={"verify_signature": False})
    second = jwt.decode(mint_customer_token(CUSTOMER_ID, 1, now), options={"verify_signature": False})
    assert first["jti"] != second["jti"]


# --------------------------------------------------------------------------- not a staff token


def test_a_customer_token_is_refused_by_the_staff_decoder(settings, now):
    with pytest.raises(TokenError):
        decode_token(mint_customer_token(CUSTOMER_ID, 1, now))


def test_a_customer_token_fails_staff_decoding_because_of_the_key_alone(settings, now):
    """Everything the staff decoder checks is right (`typ`, no `aud`, claims, expiry) except the key."""
    staff_shaped = {
        "sub": str(USER_ID),
        "typ": "access",
        "ver": 1,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
    }
    # Control: this exact shape is accepted when signed with the staff key.
    assert decode_token(jwt.encode(staff_shaped, _staff_secret(settings), algorithm="HS256"))

    derived_key = customer_token._signing_key()
    assert derived_key != _staff_secret(settings).encode()
    with pytest.raises(TokenError):
        decode_token(jwt.encode(staff_shaped, derived_key, algorithm="HS256"))


def test_a_customer_token_fails_staff_decoding_because_of_typ_alone(settings, now):
    """Signed with the staff key and carrying no `aud`: only `typ` is wrong."""
    claims = _customer_claims(now, sub=str(USER_ID))
    del claims["aud"]
    token = jwt.encode(claims, _staff_secret(settings), algorithm="HS256")
    with pytest.raises(TokenError):
        decode_token(token)


def test_a_customer_token_fails_staff_decoding_because_of_aud_alone(settings, now):
    """Signed with the staff key and `typ=access`: only the `aud` claim is wrong."""
    claims = _customer_claims(now, sub=str(USER_ID), typ="access")
    secret = _staff_secret(settings)
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(
            jwt.encode(claims, secret, algorithm="HS256"),
            secret,
            algorithms=["HS256"],
            options={"require": ["sub", "typ", "ver", "iat", "exp"]},
        )
    with pytest.raises(TokenError):
        decode_token(jwt.encode(claims, secret, algorithm="HS256"))

    # Control: without `aud` the same claims are a valid staff token.
    del claims["aud"]
    assert decode_token(jwt.encode(claims, secret, algorithm="HS256"))


# --------------------------------------------------------------------------- not a customer token


def test_a_staff_access_token_is_refused_as_a_customer_token(settings, now):
    staff = create_access_token(user_id=USER_ID, token_version=1, role="admin", now=now)

    with pytest.raises(CustomerTokenError):
        decode_customer_token(staff)
    assert is_valid_customer_token(staff) is False


def test_a_customer_shaped_token_signed_with_the_staff_key_is_not_valid(settings, now):
    token = jwt.encode(_customer_claims(now), _staff_secret(settings), algorithm="HS256")
    assert is_valid_customer_token(token) is False


def test_a_token_signed_with_the_right_key_but_another_typ_is_not_valid(settings, now):
    token = jwt.encode(_customer_claims(now, typ="access"), customer_token._signing_key(), algorithm="HS256")
    assert is_valid_customer_token(token) is False


@pytest.mark.parametrize("audience", ["hours", "hours-service-customer-x", None])
def test_a_token_signed_with_the_right_key_but_another_aud_is_not_valid(settings, now, audience):
    claims = _customer_claims(now)
    if audience is None:
        del claims["aud"]
    else:
        claims["aud"] = audience
    token = jwt.encode(claims, customer_token._signing_key(), algorithm="HS256")
    assert is_valid_customer_token(token) is False


@pytest.mark.parametrize("claim", ["sub", "ver", "jti", "iat", "exp"])
def test_a_token_missing_a_required_claim_is_not_valid(settings, now, claim):
    claims = _customer_claims(now)
    del claims[claim]
    token = jwt.encode(claims, customer_token._signing_key(), algorithm="HS256")
    assert is_valid_customer_token(token) is False


@pytest.mark.parametrize("version", ["1", 1.5, True, None])
def test_a_token_whose_version_is_not_an_integer_is_not_valid(settings, now, version):
    token = jwt.encode(_customer_claims(now, ver=version), customer_token._signing_key(), algorithm="HS256")
    assert is_valid_customer_token(token) is False


def test_a_token_whose_subject_is_not_a_uuid_is_not_valid(settings, now):
    token = jwt.encode(
        _customer_claims(now, sub="not-a-uuid"), customer_token._signing_key(), algorithm="HS256"
    )
    assert is_valid_customer_token(token) is False


# --------------------------------------------------------------------------- invalid input


def test_an_expired_token_is_not_valid(settings):
    long_ago = datetime.now(UTC) - timedelta(hours=9)
    token = mint_customer_token(CUSTOMER_ID, 1, long_ago)

    assert is_valid_customer_token(token) is False
    with pytest.raises(CustomerTokenError):
        decode_customer_token(token)


def test_there_is_no_leeway_at_expiry(settings):
    """One second past `exp` is expired. A grace period would be a second of extra access."""
    issued = datetime.now(UTC) - timedelta(seconds=11)
    assert is_valid_customer_token(mint_customer_token(CUSTOMER_ID, 1, issued, 10)) is False
    assert is_valid_customer_token(mint_customer_token(CUSTOMER_ID, 1, issued, 60)) is True


def test_a_token_still_inside_its_lifetime_is_valid(settings):
    recent = datetime.now(UTC) - timedelta(hours=7, minutes=59)
    assert is_valid_customer_token(mint_customer_token(CUSTOMER_ID, 1, recent)) is True


def test_a_token_signed_with_another_key_is_not_valid(settings, now):
    token = jwt.encode(_customer_claims(now), "a-different-key-entirely-not-ours-1234", algorithm="HS256")

    assert is_valid_customer_token(token) is False
    with pytest.raises(CustomerTokenError):
        decode_customer_token(token)


def test_a_tampered_payload_is_not_valid(settings, now):
    """Edit the claims (here, point `sub` at someone else and extend `ver`) and keep the signature."""
    header, payload, signature = mint_customer_token(CUSTOMER_ID, 1, now).split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = str(uuid.uuid4())
    claims["ver"] = 99
    tampered = f"{header}.{_b64(claims)}.{signature}"

    assert is_valid_customer_token(tampered) is False
    with pytest.raises(CustomerTokenError):
        decode_customer_token(tampered)


def test_an_edited_signature_is_not_valid(settings, now):
    header, payload, signature = mint_customer_token(CUSTOMER_ID, 1, now).split(".")
    assert is_valid_customer_token(f"{header}.{payload}.{signature[:-4]}AAAA") is False


def test_an_unsigned_token_is_not_valid(settings, now):
    """`alg: none` is refused because the algorithm is named."""
    unsigned = jwt.encode(_customer_claims(now), key="", algorithm="none")
    hand_built = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(_customer_claims(now))}."

    for candidate in (unsigned, hand_built):
        assert is_valid_customer_token(candidate) is False
        with pytest.raises(CustomerTokenError):
            decode_customer_token(candidate)


def test_a_token_naming_another_hmac_algorithm_is_not_valid(settings, now):
    token = jwt.encode(_customer_claims(now), customer_token._signing_key(), algorithm="HS512")
    assert is_valid_customer_token(token) is False


@pytest.mark.parametrize(
    "token",
    [
        "",
        " ",
        "rubbish",
        "a.b.c",
        "..",
        "Bearer something",
        "\x00\x00",
        "טוקן.לא.תקין",
        "x" * 1_000_000,
        ("a" * 5000 + ".") * 200,
        None,
        b"bytes",
        12345,
        ["a", "b"],
    ],
)
def test_garbage_is_not_valid_and_never_raises(settings, token: object):
    assert is_valid_customer_token(token) is False


def test_decoding_garbage_raises_the_package_error_with_a_stable_code(settings):
    for token in ("", "rubbish", "a.b.c"):
        with pytest.raises(CustomerTokenError) as caught:
            decode_customer_token(token)
        assert caught.value.code == "invalid_token"


def test_the_validity_check_never_raises_even_when_settings_are_unusable(settings, monkeypatch):
    def broken() -> None:
        raise RuntimeError("settings could not be loaded")

    monkeypatch.setattr(customer_token, "get_settings", broken)
    assert is_valid_customer_token("a.b.c") is False


# --------------------------------------------------------------------------- no database, no flag


def test_the_validity_check_reads_no_database(settings, now, monkeypatch):
    """The engine and session factory are made to fail loudly; a valid token still validates."""
    from app.db import session as session_module

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the database was touched")

    monkeypatch.setattr(session_module, "get_engine", forbidden)
    monkeypatch.setattr(session_module, "get_session_factory", forbidden)
    monkeypatch.setattr(session_module, "get_db", forbidden)

    token = mint_customer_token(CUSTOMER_ID, 1, now)
    assert is_valid_customer_token(token) is True
    assert is_valid_customer_token("garbage") is False
    assert decode_customer_token(token).customer_id == CUSTOMER_ID


@pytest.mark.parametrize("flag", ["true", "false", ""])
def test_the_validity_check_does_not_depend_on_the_feature_flag(monkeypatch, now, flag: str):
    from app.core.config import get_settings

    monkeypatch.setenv("SERVICE_TICKETS_ENABLED", flag)
    get_settings.cache_clear()
    try:
        token = mint_customer_token(CUSTOMER_ID, 1, now)
        assert is_valid_customer_token(token) is True
        assert is_valid_customer_token("garbage") is False
    finally:
        get_settings.cache_clear()


def test_a_token_minted_with_the_flag_on_is_valid_with_it_off(monkeypatch, now):
    from app.core.config import get_settings

    monkeypatch.setenv("SERVICE_TICKETS_ENABLED", "true")
    get_settings.cache_clear()
    try:
        token = mint_customer_token(CUSTOMER_ID, 1, now)
        monkeypatch.setenv("SERVICE_TICKETS_ENABLED", "false")
        get_settings.cache_clear()
        assert is_valid_customer_token(token) is True
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- import boundary


def test_the_module_imports_only_what_the_design_allows():
    """No `app.api` import (deps.py imports this module in task 4.2), and no database import."""
    source = Path(customer_token.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)

    app_imports = {name for name in imported if name == "app" or name.startswith("app.")}
    assert app_imports == {"app.core.config"}
    assert not any(name.split(".")[0] in {"sqlalchemy", "redis", "boto3"} for name in imported)
