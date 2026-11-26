"""Hours rejects a customer token with 403 and changes nothing else about sign-in.

Requirements 7.1, 7.2, 7.5, 7.6, 7.9, 7.10 and 20.6.

`get_current_user` and the refresh route answer 403 `customer_token_not_accepted` when the token they
could not accept as a staff token is a currently valid customer token, and otherwise answer exactly
what they always did. The staff cases below pin the pre-change status, body and headers literally, so
a change to the failure path of staff sign-in shows up here.

The full route enumeration (every Hours route, flag on and off, technician tokens) is task 4.3.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import create_access_token, create_refresh_token
from app.main import create_app
from app.models.change_log import ChangeLog
from app.service_tickets import customer_token
from auth_support import DEFAULT_PASSWORD

REJECTED = {"detail": {"error": {"code": "customer_token_not_accepted"}}}
INVALID_TOKEN = {"detail": {"error": {"code": "invalid_token"}}}
NOT_AUTHENTICATED = {"detail": {"error": {"code": "not_authenticated"}}}

#: Authenticated Hours GET routes from two different routers.
HOURS_GET_ROUTES = ["/api/auth/me", "/api/sites", "/api/users"]

#: The failure-path headers that are fixed. Anything else (date, request id) varies per request.
_FIXED_HEADERS = ("www-authenticate", "content-type")


def _valid_customer_token(customer_id: uuid.UUID | None = None) -> str:
    return customer_token.mint_customer_token(customer_id or uuid.uuid4(), 0)


def _expired_customer_token() -> str:
    return customer_token.mint_customer_token(
        uuid.uuid4(), 0, now=datetime.now(UTC) - timedelta(hours=9), ttl=28800
    )


def _badly_signed_customer_token(settings) -> str:
    """Customer-shaped claims signed with the raw staff secret instead of the derived customer key."""
    issued_at = int(datetime.now(UTC).timestamp())
    claims = {
        "sub": str(uuid.uuid4()),
        "typ": customer_token.TOKEN_TYPE,
        "aud": customer_token.AUDIENCE,
        "ver": 0,
        "jti": uuid.uuid4().hex,
        "iat": issued_at,
        "exp": issued_at + 3600,
    }
    return jwt.encode(claims, settings.jwt_secret_key.get_secret_value(), algorithm="HS256")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _fixed_headers(response) -> dict[str, str]:
    return {name: response.headers[name] for name in _FIXED_HEADERS if name in response.headers}


def _change_log_rows(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(ChangeLog))


def _hit(api_client, target: str, token: str | None):
    """One request to a target: a GET path, `refresh` (token in the body) or `logout` (bearer)."""
    headers = _bearer(token) if token is not None else {}
    if target == "refresh":
        return api_client.post("/api/auth/refresh", json={"refresh_token": token or ""})
    if target == "logout":
        return api_client.post("/api/auth/logout", headers=headers)
    return api_client.get(target, headers=headers)


TARGETS = [*HOURS_GET_ROUTES, "refresh", "logout"]


# --------------------------------------------------------------------------- 403 for a valid customer token


@pytest.mark.parametrize("target", TARGETS)
def test_a_valid_customer_token_gets_403_and_no_data(api_client, target):
    response = _hit(api_client, target, _valid_customer_token())

    assert response.status_code == 403
    assert response.json() == REJECTED
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize("target", ["refresh", "logout", "/api/auth/me"])
def test_a_customer_token_for_a_staff_users_id_is_still_403_and_changes_nothing(
    api_client, make_user, session: Session, target
):
    """Requirement 7.5: the subject is only a customer id here, so the staff row is never read."""
    user = make_user()
    version_before = user.token_version
    rows_before = _change_log_rows(session)

    response = _hit(api_client, target, _valid_customer_token(user.id))

    assert response.status_code == 403
    assert response.json() == REJECTED
    session.refresh(user)
    assert user.token_version == version_before
    assert _change_log_rows(session) == rows_before


def test_a_customer_token_for_a_staff_id_does_not_sign_the_staff_user_out(api_client, make_user):
    user = make_user()
    staff_access = create_access_token(user_id=user.id, token_version=user.token_version)

    _hit(api_client, "logout", _valid_customer_token(user.id))

    assert api_client.get("/api/auth/me", headers=_bearer(staff_access)).status_code == 200


# --------------------------------------------------------------------------- 401 is unchanged


@pytest.mark.parametrize("target", TARGETS)
def test_an_expired_customer_token_gets_the_same_401_as_a_garbage_token(api_client, target):
    garbage = _hit(api_client, target, "not-a-token")
    expired = _hit(api_client, target, _expired_customer_token())

    assert expired.status_code == garbage.status_code == 401
    assert expired.json() == garbage.json() == INVALID_TOKEN
    assert _fixed_headers(expired) == _fixed_headers(garbage)


@pytest.mark.parametrize("target", TARGETS)
def test_a_badly_signed_customer_token_gets_the_same_401_as_a_garbage_token(api_client, settings, target):
    garbage = _hit(api_client, target, "not-a-token")
    forged = _hit(api_client, target, _badly_signed_customer_token(settings))

    assert forged.status_code == garbage.status_code == 401
    assert forged.json() == garbage.json() == INVALID_TOKEN
    assert _fixed_headers(forged) == _fixed_headers(garbage)


# --------------------------------------------------------------------------- staff behaviour, pinned exactly


@pytest.mark.parametrize("target", [*HOURS_GET_ROUTES, "logout"])
def test_a_garbage_bearer_token_is_still_the_exact_401(api_client, target):
    response = _hit(api_client, target, "not-a-token")

    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN
    assert _fixed_headers(response) == {"www-authenticate": "Bearer", "content-type": "application/json"}


def test_a_garbage_refresh_token_is_still_the_exact_401(api_client):
    response = _hit(api_client, "refresh", "not-a-token")

    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN
    assert _fixed_headers(response) == {"www-authenticate": "Bearer", "content-type": "application/json"}


@pytest.mark.parametrize("target", [*HOURS_GET_ROUTES, "logout"])
def test_a_missing_token_is_still_the_exact_401(api_client, target):
    response = _hit(api_client, target, None)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert _fixed_headers(response) == {"www-authenticate": "Bearer", "content-type": "application/json"}


def test_an_empty_bearer_token_is_still_not_authenticated(api_client):
    response = api_client.get("/api/auth/me", headers={"Authorization": "Bearer "})

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


def test_an_expired_staff_token_is_still_the_exact_401(api_client, make_user):
    user = make_user()
    stale = create_access_token(
        user_id=user.id, token_version=user.token_version, now=datetime.now(UTC) - timedelta(hours=1)
    )

    response = _hit(api_client, "/api/auth/me", stale)

    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN
    assert _fixed_headers(response) == {"www-authenticate": "Bearer", "content-type": "application/json"}


def test_a_staff_refresh_token_presented_as_an_access_token_is_still_the_exact_401(api_client, make_user):
    user = make_user()
    refresh, _ = create_refresh_token(user_id=user.id, token_version=user.token_version)

    response = _hit(api_client, "/api/auth/me", refresh)

    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN


def test_a_valid_staff_token_still_works(api_client, make_user):
    user = make_user()
    token = create_access_token(user_id=user.id, token_version=user.token_version)

    me = api_client.get("/api/auth/me", headers=_bearer(token))
    sites = api_client.get("/api/sites", headers=_bearer(token))

    assert me.status_code == 200
    assert me.json()["username"] == user.username
    assert sites.status_code == 200


def test_a_valid_staff_refresh_token_still_rotates(api_client, make_user):
    user = make_user()
    refresh, _ = create_refresh_token(user_id=user.id, token_version=user.token_version)

    response = api_client.post("/api/auth/refresh", json={"refresh_token": refresh})

    assert response.status_code == 200
    assert response.json()["access_token"]


# --------------------------------------------------------------------------- sign-in is not touched


def _login_fields(response) -> tuple[int, object]:
    """Status and body, with the token values of a success reduced to their presence."""
    body = response.json()
    if response.status_code == 200:
        body = {key: bool(value) for key, value in body.items()}
    return response.status_code, body


def test_login_ignores_a_customer_token_in_the_authorization_header(api_client, make_user):
    """Requirement 7.10: the same request with and without the header gets the same response."""
    user = make_user()
    header = _bearer(_valid_customer_token())
    cases = [
        {"username": user.username, "password": DEFAULT_PASSWORD},
        {"username": user.username, "password": "wrong"},
        {"username": "nobody", "password": "wrong"},
    ]

    for payload in cases:
        bare = api_client.post("/api/auth/login", json=payload)
        with_token = api_client.post("/api/auth/login", json=payload, headers=header)

        assert _login_fields(with_token) == _login_fields(bare)
        assert _fixed_headers(with_token) == _fixed_headers(bare)


def test_login_with_a_customer_token_header_still_issues_a_working_staff_pair(api_client, make_user):
    user = make_user()

    response = api_client.post(
        "/api/auth/login",
        json={"username": user.username, "password": DEFAULT_PASSWORD},
        headers=_bearer(_valid_customer_token()),
    )

    assert response.status_code == 200
    token = response.json()["access_token"]
    assert api_client.get("/api/auth/me", headers=_bearer(token)).status_code == 200


def test_a_customer_token_in_the_header_does_not_make_login_a_403(api_client):
    response = api_client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "wrong"},
        headers=_bearer(_valid_customer_token()),
    )

    assert response.status_code == 401
    assert response.json() == {"detail": {"error": {"code": "invalid_credentials"}}}


# --------------------------------------------------------------------------- independent of the flag


@pytest.fixture(params=["false", "true"], ids=["flag-off", "flag-on"])
def flagged_client(request, settings, monkeypatch, session: Session) -> Iterator:
    """An app built with the service-tickets flag patched on or off.

    `settings` is requested before `monkeypatch` so that on teardown the environment is restored
    first and the settings cache is cleared after, leaving no flag-on settings behind.
    """
    from fastapi.testclient import TestClient

    from app.db.session import get_db

    monkeypatch.setenv("SERVICE_TICKETS_ENABLED", request.param)
    get_settings.cache_clear()
    assert get_settings().service_tickets_enabled == request.param
    app = create_app(get_settings())
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.mark.parametrize("target", TARGETS)
def test_the_403_does_not_depend_on_the_feature_flag(flagged_client, target):
    response = _hit(flagged_client, target, _valid_customer_token())

    assert response.status_code == 403
    assert response.json() == REJECTED


def test_staff_sign_in_does_not_depend_on_the_feature_flag(flagged_client, make_user):
    user = make_user()
    token = create_access_token(user_id=user.id, token_version=user.token_version)

    assert flagged_client.get("/api/auth/me", headers=_bearer(token)).status_code == 200
    assert _hit(flagged_client, "/api/auth/me", "not-a-token").json() == INVALID_TOKEN
