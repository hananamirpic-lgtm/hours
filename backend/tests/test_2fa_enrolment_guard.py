"""Mandatory two-factor enrolment for the administrator role (Requirement 1.6).

The requirement is that an administrator who has not enrolled cannot use the system, and the mechanism
is `app.api.deps.get_enrolled_user`: the auth router depends on `CurrentUser`, every other router
depends on `EnrolledUser`, and the second refuses until the obligation is met. A flag on the
current-user dependency was the alternative and was rejected — a flag is something each router has to
remember to read, and the endpoint that forgets is unguarded with nothing in its signature to say so.

No router outside `auth` exists yet, so these tests mount a probe endpoint that asks for
`EnrolledUser` and nothing else. That is the whole contract under test: what happens to a request for
a resource whose only requirement is an enrolled caller. When the real routers land they inherit this
behaviour by asking for the same type, and the probe keeps testing the guard rather than whichever
endpoint happened to be first.
"""

from __future__ import annotations

from collections.abc import Iterator

import pyotp
import pytest
from fastapi import APIRouter
from sqlalchemy.orm import Session

# `EnrolledUser` is imported here rather than inside the fixture on purpose. `from __future__ import
# annotations` makes every annotation a string, and FastAPI resolves a dependency annotation against
# the module's globals — a name bound only inside the fixture is not there to be found.
from app.api.deps import EnrolledUser
from app.models.user import User, UserRole
from auth_support import DEFAULT_PASSWORD

PROBE_PATH = "/api/probe"


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


def _auth(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# --------------------------------------------------------------------------- the block


def test_an_admin_without_2fa_is_blocked_from_a_protected_endpoint(guarded_client, make_user):
    """Requirement 1.6. Signing in is allowed — the block is on everything the token would buy."""
    tokens = _tokens(guarded_client, make_user(role=UserRole.ADMIN))
    response = guarded_client.get(PROBE_PATH, headers=_auth(tokens))

    assert response.status_code == 403
    assert _code(response) == "totp_enrolment_required"


def test_the_block_is_a_403_and_not_a_401(guarded_client, make_user):
    """The distinction is the whole message. A 401 tells a client to send credentials again, and
    fresh credentials are exactly what does not help here — a front end treating the two alike would
    sign the administrator out in a loop instead of showing them the enrolment screen."""
    tokens = _tokens(guarded_client, make_user(role=UserRole.ADMIN))
    assert guarded_client.get(PROBE_PATH, headers=_auth(tokens)).status_code != 401


def test_an_unauthenticated_request_is_still_a_401(guarded_client):
    """The enrolment guard sits behind authentication, not in front of it."""
    response = guarded_client.get(PROBE_PATH)
    assert response.status_code == 401
    assert _code(response) == "not_authenticated"


# --------------------------------------------------------------------------- the way out


def test_a_blocked_admin_can_reach_the_enrolment_endpoints(guarded_client, make_user):
    """The exception list, asserted in full. If any of these were guarded the administrator would be
    locked out of the only account that can fix it."""
    tokens = _tokens(guarded_client, make_user(role=UserRole.ADMIN))
    headers = _auth(tokens)

    assert guarded_client.get("/api/auth/me", headers=headers).status_code == 200
    setup = guarded_client.post("/api/auth/2fa/setup", headers=headers)
    assert setup.status_code == 200
    verify = guarded_client.post(
        "/api/auth/2fa/verify",
        headers=headers,
        json={"totp_code": pyotp.TOTP(setup.json()["secret"]).now()},
    )
    assert verify.status_code == 200


def test_a_blocked_admin_can_sign_out(guarded_client, make_user):
    """So a wrong account can be got out of without enrolling in it first."""
    tokens = _tokens(guarded_client, make_user(role=UserRole.ADMIN))
    assert guarded_client.post("/api/auth/logout", headers=_auth(tokens)).status_code == 204


def test_completing_enrolment_unblocks_the_protected_endpoint(guarded_client, make_user):
    """With the same access token: enrolment does not bump the token version, so there is no reason
    to make the user sign in again at the moment they have just proved who they are."""
    user = make_user(role=UserRole.ADMIN)
    headers = _auth(_tokens(guarded_client, user))
    assert guarded_client.get(PROBE_PATH, headers=headers).status_code == 403

    secret = guarded_client.post("/api/auth/2fa/setup", headers=headers).json()["secret"]
    guarded_client.post("/api/auth/2fa/verify", headers=headers, json={"totp_code": pyotp.TOTP(secret).now()})

    response = guarded_client.get(PROBE_PATH, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"username": user.username}


def test_an_enrolled_admin_is_not_blocked(guarded_client, make_user):
    secret = pyotp.random_base32()
    user = make_user(role=UserRole.ADMIN, is_2fa_enabled=True, totp_secret=secret)
    tokens = _tokens(guarded_client, user, totp_code=pyotp.TOTP(secret).now())

    assert guarded_client.get(PROBE_PATH, headers=_auth(tokens)).status_code == 200


# --------------------------------------------------------------------------- everyone else


@pytest.mark.parametrize("role", [UserRole.SITE_MANAGER, UserRole.ACCOUNTING, UserRole.EMPLOYEE])
def test_a_non_admin_without_2fa_is_not_blocked(guarded_client, make_user, role: UserRole):
    """Requirement 1.6 makes 2FA mandatory for the administrator only. The other roles are prompted
    through `is_2fa_enrolment_prompted` on `GET /auth/me`, and prompting is advisory: a site manager
    who declines keeps working."""
    tokens = _tokens(guarded_client, make_user(role=role))
    assert guarded_client.get(PROBE_PATH, headers=_auth(tokens)).status_code == 200
