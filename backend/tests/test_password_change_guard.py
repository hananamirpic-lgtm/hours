"""The first-use password-change gate (Requirement 1).

A non-administrator who owes a password change cannot use the system until they set one, and the
mechanism is the same as the mandatory-2FA gate: the auth router depends on `CurrentUser`, every other
router depends on `EnrolledUser`, and `app.api.deps.get_enrolled_user` refuses until the obligation is
met. This file mirrors `test_2fa_enrolment_guard.py` and mounts a probe endpoint that asks for
`EnrolledUser` and nothing else, so the subject under test is the guard rather than whichever real
endpoint happened to be first.

The administrator is exempt twice over — the create path never sets the flag on an admin, and the gate
checks the role as well — so the interesting admin case here is the belt-and-suspenders one: even an
admin whose column has somehow been set true is not blocked.
"""

from __future__ import annotations

from collections.abc import Iterator

import pyotp
import pytest
from fastapi import APIRouter
from sqlalchemy.orm import Session

# Imported at module scope, not inside the fixture: `from __future__ import annotations` makes every
# annotation a string, and FastAPI resolves a dependency annotation against the module globals.
from app.api.deps import EnrolledUser
from app.models.user import User, UserRole
from auth_support import DEFAULT_PASSWORD

PROBE_PATH = "/api/probe"
NEW_PASSWORD = "a-brand-new-passphrase"


@pytest.fixture
def guarded_client(settings, session: Session) -> Iterator:
    """Test client for an application carrying one endpoint that requires an enrolled caller."""
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    probe = APIRouter()

    @probe.get(PROBE_PATH)
    def read_probe(user: EnrolledUser) -> dict[str, str]:
        return {"username": user.username}

    app = create_app(settings)
    app.include_router(probe)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


def _tokens(client, user: User, **overrides) -> dict[str, str]:
    payload = {"username": user.username, "password": DEFAULT_PASSWORD}
    payload.update(overrides)
    response = client.post("/api/auth/login", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _admin_tokens(client, user: User, secret: str) -> dict[str, str]:
    return _tokens(client, user, totp_code=pyotp.TOTP(secret).now())


def _auth(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# --------------------------------------------------------------------------- the block


@pytest.mark.parametrize("role", [UserRole.SITE_MANAGER, UserRole.ACCOUNTING, UserRole.EMPLOYEE])
def test_a_non_admin_owing_a_change_is_blocked_from_a_protected_endpoint(guarded_client, make_user, role):
    """Requirement 1. Signing in is allowed — the block is on everything the token would buy."""
    tokens = _tokens(guarded_client, make_user(role=role, must_change_password=True))
    response = guarded_client.get(PROBE_PATH, headers=_auth(tokens))

    assert response.status_code == 403
    assert _code(response) == "password_change_required"


def test_an_unauthenticated_request_is_still_a_401(guarded_client):
    """The gate sits behind authentication, not in front of it."""
    response = guarded_client.get(PROBE_PATH)
    assert response.status_code == 401
    assert _code(response) == "not_authenticated"


def test_a_non_admin_without_the_obligation_is_not_blocked(guarded_client, make_user):
    tokens = _tokens(guarded_client, make_user(role=UserRole.SITE_MANAGER, must_change_password=False))
    assert guarded_client.get(PROBE_PATH, headers=_auth(tokens)).status_code == 200


# --------------------------------------------------------------------------- the admin exemption


def test_an_admin_is_never_blocked_even_if_the_flag_is_set(guarded_client, make_user):
    """The belt-and-suspenders role check: the create path never sets this on an admin, but even if
    the column were somehow true the admin must not be locked out."""
    secret = pyotp.random_base32()
    admin = make_user(
        role=UserRole.ADMIN, is_2fa_enabled=True, totp_secret=secret, must_change_password=True
    )
    tokens = _admin_tokens(guarded_client, admin, secret)

    assert guarded_client.get(PROBE_PATH, headers=_auth(tokens)).status_code == 200


# --------------------------------------------------------------------------- the way out


def test_a_blocked_user_can_reach_the_endpoints_that_let_them_change(guarded_client, make_user):
    """The exception list, asserted in full: see why you are blocked, change the password, sign out."""
    tokens = _tokens(guarded_client, make_user(role=UserRole.SITE_MANAGER, must_change_password=True))
    headers = _auth(tokens)

    assert guarded_client.get("/api/auth/me", headers=headers).status_code == 200
    changed = guarded_client.post(
        "/api/auth/change-password",
        headers=headers,
        json={"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert changed.status_code == 200


def test_a_blocked_user_can_sign_out(guarded_client, make_user):
    tokens = _tokens(guarded_client, make_user(role=UserRole.SITE_MANAGER, must_change_password=True))
    assert guarded_client.post("/api/auth/logout", headers=_auth(tokens)).status_code == 204


def test_changing_the_password_unblocks_the_protected_endpoint(guarded_client, make_user):
    """The change bumps the token version, so the caller signs in fresh with the new password and the
    obligation is gone."""
    user = make_user(role=UserRole.SITE_MANAGER, must_change_password=True)
    headers = _auth(_tokens(guarded_client, user))
    assert guarded_client.get(PROBE_PATH, headers=headers).status_code == 403

    guarded_client.post(
        "/api/auth/change-password",
        headers=headers,
        json={"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD},
    )

    fresh = _auth(_tokens(guarded_client, user, password=NEW_PASSWORD))
    response = guarded_client.get(PROBE_PATH, headers=fresh)
    assert response.status_code == 200
    assert response.json() == {"username": user.username}
