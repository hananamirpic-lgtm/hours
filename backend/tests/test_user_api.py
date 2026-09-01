"""User management endpoints over HTTP (Requirement 1, 2.1, 2.3, 20.8).

The two claims Task 35 names explicitly are the subject here:

* **Deactivation ends sessions immediately (Requirement 20.8).** A user signs in, holds a working
  access token, is deactivated by an administrator, and the *same* token is refused on its next use —
  not at its expiry. This is the token-version bump doing its job through the real HTTP path.

* **A site assignment changes the manager's visible scope (Requirement 2.3).** A site manager with no
  assignment sees no sites; after `PUT /users/{id}/sites` grants them one, the same manager's site
  list shows exactly that site. Scope is read live from `user_sites`, so the manager's existing token
  reflects the new grant without a reissue.

Around those, the wiring: create names a conflict on a duplicate username, every endpoint is
administrator-only, and a response carries no credential. The deeper rules are pinned in
`test_user_service.py`; here the subject is the wiring.

The sign-in helper mirrors `test_site_api.py`: an administrator must have completed 2FA enrolment or
every endpoint answers 403 (Requirement 1.6), and it returns the created user so a test can act on it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.user import UserRole
from auth_support import DEFAULT_PASSWORD


@pytest.fixture
def users_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(users_client, make_user):
    """Sign a user in and return the auth header and the created user."""

    def _sign_in(role: UserRole, **user_overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **user_overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **user_overrides)
        payload["username"] = user.username

        response = users_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        header = {"Authorization": f"Bearer {response.json()['access_token']}"}
        return header, user

    return _sign_in


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


def _make_client_row(session: Session, name: str = "Acme") -> uuid.UUID:
    client = Client(name=name)
    session.add(client)
    session.commit()
    return client.id


def _site_body(client_id: uuid.UUID, *, number: str) -> dict[str, object]:
    return {"name": f"Site {number}", "site_number": number, "client_id": str(client_id)}


def _new_user_body(**overrides) -> dict[str, object]:
    body: dict[str, object] = {
        "username": "newmanager",
        "password": "correct-horse-battery",
        "role": "site_manager",
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- create and read


def test_create_and_read_a_user_as_admin(users_client, sign_in):
    """Requirement 1, 2.1: a login is created with a role and read back, with no credential on it."""
    admin, _ = sign_in(UserRole.ADMIN)

    created = users_client.post("/api/users", json=_new_user_body(), headers=admin)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["username"] == "newmanager"
    assert body["role"] == "site_manager"
    assert body["is_active"] is True
    # No credential ever leaves on a response.
    assert "password" not in body
    assert "password_hash" not in body
    assert "totp_secret" not in body


def test_a_created_user_can_sign_in(users_client, sign_in):
    """The password set at creation actually works — it was hashed, not dropped."""
    admin, _ = sign_in(UserRole.ADMIN)
    users_client.post(
        "/api/users",
        json=_new_user_body(username="signme", password="a-usable-password"),
        headers=admin,
    )

    response = users_client.post(
        "/api/auth/login", json={"username": "signme", "password": "a-usable-password"}
    )
    assert response.status_code == 200, response.text


def test_a_duplicate_username_is_a_conflict_that_names_the_holder(users_client, sign_in):
    admin, _ = sign_in(UserRole.ADMIN)
    first = users_client.post(
        "/api/users", json=_new_user_body(username="taken"), headers=admin
    )
    assert first.status_code == 201, first.text

    duplicate = users_client.post(
        "/api/users", json=_new_user_body(username="taken"), headers=admin
    )
    assert duplicate.status_code == 409
    error = duplicate.json()["detail"]["error"]
    assert error["code"] == "duplicate_username"
    assert error["params"]["user_id"] == first.json()["id"]


def test_assigning_sites_to_a_non_manager_is_rejected(users_client, sign_in, session: Session):
    """Requirement 2.3: scope is a site-manager concept; assigning it to accounting is a bad request."""
    admin, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    site_id = users_client.post(
        "/api/sites", json=_site_body(client_id, number="S-1"), headers=admin
    ).json()["id"]

    response = users_client.post(
        "/api/users",
        json=_new_user_body(username="acct", role="accounting", site_ids=[site_id]),
        headers=admin,
    )
    assert response.status_code == 400
    assert _code(response) == "site_scope_not_applicable"


# --------------------------------------------------------------------------- authorization


def test_a_site_manager_cannot_list_users(users_client, sign_in):
    """Managing accounts is administrator-only (Requirement 2.2)."""
    manager, _ = sign_in(UserRole.SITE_MANAGER)
    response = users_client.get("/api/users", headers=manager)
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


# --------------------------------------------------------------------------- deactivation (Requirement 20.8)


def test_deactivation_ends_sessions_immediately(users_client, sign_in):
    """Requirement 20.8: the target's own token is refused the moment they are deactivated.

    The manager signs in and confirms the token works. An administrator then deactivates them, and the
    *same* token — not a re-issued one — is rejected on its next use. That is the token-version bump,
    not a wait for expiry.
    """
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    admin_headers, _ = sign_in(UserRole.ADMIN)

    # The manager's token works before deactivation.
    assert users_client.get("/api/auth/me", headers=manager_headers).status_code == 200

    deactivated = users_client.post(
        f"/api/users/{manager.id}/deactivate", headers=admin_headers
    )
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["is_active"] is False

    # The same token is now dead.
    after = users_client.get("/api/auth/me", headers=manager_headers)
    assert after.status_code == 401


def test_a_deactivated_user_cannot_sign_in(users_client, sign_in):
    """The other half: deactivation also refuses a fresh login (Requirement 1.4)."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    admin_headers, _ = sign_in(UserRole.ADMIN)

    users_client.post(f"/api/users/{manager.id}/deactivate", headers=admin_headers)

    response = users_client.post(
        "/api/auth/login", json={"username": manager.username, "password": DEFAULT_PASSWORD}
    )
    # The generic failure of Requirement 1.2 — deactivation does not disclose itself as distinct.
    assert response.status_code == 401


def test_a_password_reset_ends_existing_sessions(users_client, sign_in):
    """A password reset carries the same immediacy as deactivation, applied to a reset."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    admin_headers, _ = sign_in(UserRole.ADMIN)

    assert users_client.get("/api/auth/me", headers=manager_headers).status_code == 200

    reset = users_client.patch(
        f"/api/users/{manager.id}", json={"password": "a-brand-new-password"}, headers=admin_headers
    )
    assert reset.status_code == 200, reset.text

    assert users_client.get("/api/auth/me", headers=manager_headers).status_code == 401


# ------------------------------------------------------------------- site assignment (Requirement 2.3)


def test_site_assignment_changes_the_managers_visible_scope(users_client, sign_in, session: Session):
    """Requirement 2.3: assigning a site through `PUT /users/{id}/sites` widens what the manager sees.

    The manager starts with no assignment and sees no sites. After the administrator grants them one
    site, the *same* manager token lists exactly that site — scope is read live from `user_sites`, so
    the grant takes effect without a token reissue. A second, unassigned site stays invisible.
    """
    admin_headers, _ = sign_in(UserRole.ADMIN)
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    client_id = _make_client_row(session)

    mine = users_client.post(
        "/api/sites", json=_site_body(client_id, number="S-MINE"), headers=admin_headers
    ).json()["id"]
    users_client.post(
        "/api/sites", json=_site_body(client_id, number="S-OTHER"), headers=admin_headers
    )

    # Before assignment: an unassigned manager sees nothing.
    before = users_client.get("/api/sites", headers=manager_headers)
    assert before.status_code == 200, before.text
    assert before.json()["items"] == []

    # Assign the one site.
    assigned = users_client.put(
        f"/api/users/{manager.id}/sites", json={"site_ids": [mine]}, headers=admin_headers
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["site_ids"] == [mine]

    # After assignment: the same token now sees exactly that site, and no other.
    after = users_client.get("/api/sites", headers=manager_headers)
    assert after.status_code == 200, after.text
    ids = {item["id"] for item in after.json()["items"]}
    assert ids == {mine}

    # And the user card reflects the scope.
    card = users_client.get(f"/api/users/{manager.id}", headers=admin_headers)
    assert card.json()["site_ids"] == [mine]


def test_removing_a_site_assignment_narrows_the_scope(users_client, sign_in, session: Session):
    """The mirror: replacing the scope with an empty set takes the site away again."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    client_id = _make_client_row(session)

    site_id = users_client.post(
        "/api/sites", json=_site_body(client_id, number="S-1"), headers=admin_headers
    ).json()["id"]

    users_client.put(
        f"/api/users/{manager.id}/sites", json={"site_ids": [site_id]}, headers=admin_headers
    )
    assert len(users_client.get("/api/sites", headers=manager_headers).json()["items"]) == 1

    users_client.put(
        f"/api/users/{manager.id}/sites", json={"site_ids": []}, headers=admin_headers
    )
    assert users_client.get("/api/sites", headers=manager_headers).json()["items"] == []


# --------------------------------------------------------------------------- first-use password change


def test_a_new_non_admin_user_must_change_their_password(users_client, sign_in):
    """Requirement 1: a login created with an administrator-chosen password must change it on first
    use, so the person who owns the account picks their own."""
    admin, _ = sign_in(UserRole.ADMIN)
    created = users_client.post("/api/users", json=_new_user_body(role="site_manager"), headers=admin)
    assert created.status_code == 201, created.text
    assert created.json()["must_change_password"] is True


def test_a_new_admin_user_is_not_forced_to_change(users_client, sign_in):
    """The admin role is exempt: its obligation is 2FA, not a first-use change."""
    admin, _ = sign_in(UserRole.ADMIN)
    created = users_client.post(
        "/api/users",
        json=_new_user_body(username="secondadmin", role="admin"),
        headers=admin,
    )
    assert created.status_code == 201, created.text
    assert created.json()["must_change_password"] is False
