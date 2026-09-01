"""Authentication endpoints.

The service tests cover the rules; these cover the boundary — status codes, the machine code in the
body, and what the response does and does not contain. `GET /auth/me` gets the closest reading,
because it is the one response here that carries user columns and therefore the one that can leak.
"""

from __future__ import annotations

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.models.user import AppLanguage, User, UserRole
from auth_support import DEFAULT_PASSWORD


def _code(response) -> str:
    """The machine code out of the error envelope.

    Nested under `detail` because the API-conventions task has not yet installed the handler that
    lifts it to the top level; this helper is the single place that has to change when it does.
    """
    return response.json()["detail"]["error"]["code"]


def _login(api_client, user: User, **overrides):
    payload = {"username": user.username, "password": DEFAULT_PASSWORD}
    payload.update(overrides)
    return api_client.post("/api/auth/login", json=payload)


def _tokens(api_client, user: User) -> dict[str, str]:
    response = _login(api_client, user)
    assert response.status_code == 200, response.text
    return response.json()


def _auth(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# --------------------------------------------------------------------------- login


def test_login_returns_a_token_pair(api_client, make_user):
    body = _tokens(api_client, make_user())
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60
    assert body["access_token"] and body["refresh_token"]
    assert body["access_token"] != body["refresh_token"]


def test_login_with_a_wrong_password_is_401_with_a_generic_code(api_client, make_user):
    response = _login(api_client, make_user(), password="wrong")
    assert response.status_code == 401
    assert _code(response) == "invalid_credentials"


def test_login_with_an_unknown_username_returns_the_same_body(api_client, make_user):
    """Requirement 1.2 at the boundary: the response is what an attacker sees, so the response is
    where the indistinguishability has to hold."""
    known = _login(api_client, make_user(), password="wrong")
    unknown = api_client.post("/api/auth/login", json={"username": "nobody-at-all", "password": "wrong"})
    assert unknown.status_code == known.status_code
    assert unknown.json() == known.json()


def test_login_by_a_deactivated_user_is_refused(api_client, make_user):
    response = _login(api_client, make_user(is_active=False))
    assert response.status_code == 401
    assert _code(response) == "invalid_credentials"


def test_login_with_2fa_enabled_asks_for_a_code(api_client, make_user):
    response = _login(api_client, make_user(is_2fa_enabled=True, totp_secret=pyotp.random_base32()))
    assert response.status_code == 401
    assert _code(response) == "totp_required"


def test_login_with_2fa_enabled_succeeds_with_the_code(api_client, make_user):
    secret = pyotp.random_base32()
    user = make_user(is_2fa_enabled=True, totp_secret=secret, role=UserRole.ADMIN)
    response = _login(api_client, user, totp_code=pyotp.TOTP(secret).now())
    assert response.status_code == 200, response.text
    assert response.json()["access_token"]


def test_a_lockout_is_reached_through_the_endpoint(api_client, make_user):
    """Requirement 1.7 end to end, since the counter is only useful if the endpoint moves it."""
    user = make_user()
    for _ in range(5):
        assert _login(api_client, user, password="wrong").status_code == 401

    assert _login(api_client, user).status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"password": DEFAULT_PASSWORD},
        {"username": "someone"},
        {"username": "", "password": DEFAULT_PASSWORD},
        {"username": "someone", "password": ""},
        {"username": "someone", "password": "p", "totp_code": "12ab56"},
        {"username": "someone", "password": "p", "totp_code": "1234567"},
    ],
)
def test_a_malformed_login_is_a_validation_error_not_an_authentication_failure(api_client, payload):
    assert api_client.post("/api/auth/login", json=payload).status_code == 422


def test_an_unexpected_field_on_login_is_refused(api_client):
    """`extra="forbid"`, so a client sending something the server does not model finds out."""
    response = api_client.post(
        "/api/auth/login",
        json={"username": "someone", "password": "p", "remember_me": True},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- refresh


def test_refresh_returns_a_new_pair(api_client, make_user):
    tokens = _tokens(api_client, make_user())
    response = api_client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    assert response.status_code == 200, response.text
    assert response.json()["refresh_token"] != tokens["refresh_token"]


def test_the_renewed_access_token_works(api_client, make_user):
    """Requirement 1.8: renewal is only renewal if what comes back is usable."""
    tokens = _tokens(api_client, make_user())
    renewed = api_client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).json()

    assert api_client.get("/api/auth/me", headers=_auth(renewed)).status_code == 200


def test_replaying_a_refresh_token_is_refused(api_client, make_user):
    tokens = _tokens(api_client, make_user())
    first = api_client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert first.status_code == 200

    replayed = api_client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert replayed.status_code == 401
    assert _code(replayed) == "invalid_token"


def test_a_replay_also_revokes_the_token_that_replaced_it(api_client, make_user):
    tokens = _tokens(api_client, make_user())
    rotated = api_client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).json()
    api_client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    response = api_client.post("/api/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert response.status_code == 401


def test_refresh_with_rubbish_is_refused(api_client):
    response = api_client.post("/api/auth/refresh", json={"refresh_token": "not-a-token"})
    assert response.status_code == 401
    assert _code(response) == "invalid_token"


# --------------------------------------------------------------------------- two-factor enrolment


def _setup_two_factor(api_client, tokens: dict[str, str]):
    return api_client.post("/api/auth/2fa/setup", headers=_auth(tokens))


def _verify_two_factor(api_client, tokens: dict[str, str], code: str):
    return api_client.post("/api/auth/2fa/verify", headers=_auth(tokens), json={"totp_code": code})


def _enrol(api_client, tokens: dict[str, str]) -> str:
    """Complete enrolment through the endpoints and return the secret now in force."""
    secret = _setup_two_factor(api_client, tokens).json()["secret"]
    assert _verify_two_factor(api_client, tokens, pyotp.TOTP(secret).now()).status_code == 200
    return secret


def test_setup_returns_a_usable_secret_and_a_provisioning_uri(api_client, make_user):
    """Requirement 1.5. Both forms, because a QR code is unreadable on the device displaying it."""
    tokens = _tokens(api_client, make_user(role=UserRole.ADMIN))
    response = _setup_two_factor(api_client, tokens)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"secret", "provisioning_uri"}
    assert body["provisioning_uri"].startswith("otpauth://totp/")
    assert body["secret"] in body["provisioning_uri"]
    # Usable: a code generated from what came back is the code the endpoint will accept.
    assert _verify_two_factor(api_client, tokens, pyotp.TOTP(body["secret"]).now()).status_code == 200


def test_setup_is_served_no_store(api_client, make_user):
    """The one response in the system that carries a credential."""
    tokens = _tokens(api_client, make_user(role=UserRole.ADMIN))
    response = _setup_two_factor(api_client, tokens)
    assert response.headers["cache-control"] == "no-store"


def test_setup_without_a_token_is_refused(api_client):
    response = api_client.post("/api/auth/2fa/setup")
    assert response.status_code == 401
    assert _code(response) == "not_authenticated"


def test_setup_alone_does_not_enable_2fa(api_client, make_user):
    """Enabling on an unproven secret would lock the user out of an account they can no longer
    authenticate to, so the endpoint that issues a secret is not the endpoint that commits to it."""
    user = make_user(role=UserRole.ADMIN)
    tokens = _tokens(api_client, user)
    _setup_two_factor(api_client, tokens)

    assert api_client.get("/api/auth/me", headers=_auth(tokens)).json()["is_2fa_enabled"] is False
    # Still the sign-in it was before: no code demanded.
    assert _login(api_client, user).status_code == 200


def test_verify_with_a_correct_code_enables_2fa(api_client, make_user):
    tokens = _tokens(api_client, make_user(role=UserRole.ADMIN))
    secret = _setup_two_factor(api_client, tokens).json()["secret"]

    response = _verify_two_factor(api_client, tokens, pyotp.TOTP(secret).now())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_2fa_enabled"] is True
    assert body["is_2fa_enrolment_required"] is False
    assert body["is_2fa_enrolment_prompted"] is False


def test_verify_with_a_wrong_code_does_not_enable_2fa(api_client, make_user):
    tokens = _tokens(api_client, make_user(role=UserRole.ADMIN))
    secret = _setup_two_factor(api_client, tokens).json()["secret"]
    wrong = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"

    response = _verify_two_factor(api_client, tokens, wrong)
    assert response.status_code == 400
    assert _code(response) == "invalid_totp_code"
    assert api_client.get("/api/auth/me", headers=_auth(tokens)).json()["is_2fa_enabled"] is False


def test_verify_after_a_wrong_code_still_works(api_client, make_user):
    """A mistyped code leaves the user exactly where they were, still enrolling."""
    tokens = _tokens(api_client, make_user(role=UserRole.ADMIN))
    secret = _setup_two_factor(api_client, tokens).json()["secret"]
    _verify_two_factor(api_client, tokens, "000000")

    assert _verify_two_factor(api_client, tokens, pyotp.TOTP(secret).now()).status_code == 200


def test_verify_with_no_enrolment_in_progress_is_a_conflict(api_client, make_user):
    """409 rather than 400: nothing the client could put in this request would help, because the
    resource is not in a state to accept it. The next move is `POST /auth/2fa/setup`."""
    tokens = _tokens(api_client, make_user(role=UserRole.ADMIN))
    response = _verify_two_factor(api_client, tokens, "000000")

    assert response.status_code == 409
    assert _code(response) == "totp_enrolment_not_started"


def test_verify_without_a_token_is_refused(api_client):
    response = api_client.post("/api/auth/2fa/verify", json={"totp_code": "000000"})
    assert response.status_code == 401


@pytest.mark.parametrize("payload", [{}, {"totp_code": "12ab56"}, {"totp_code": "1234567"}])
def test_a_malformed_verify_is_a_validation_error(api_client, make_user, payload):
    tokens = _tokens(api_client, make_user(role=UserRole.ADMIN))
    response = api_client.post("/api/auth/2fa/verify", headers=_auth(tokens), json=payload)
    assert response.status_code == 422


def test_a_freshly_enrolled_admin_must_supply_a_code_to_sign_in(api_client, make_user):
    """The observable consequence of enrolment, and the reason the whole flow exists."""
    user = make_user(role=UserRole.ADMIN)
    secret = _enrol(api_client, _tokens(api_client, user))

    without_code = _login(api_client, user)
    assert without_code.status_code == 401
    assert _code(without_code) == "totp_required"
    assert _login(api_client, user, totp_code=pyotp.TOTP(secret).now()).status_code == 200


# --------------------------------------------------------------------------- re-enrolment


def test_re_enrolment_leaves_the_existing_code_working_until_the_new_one_is_proven(api_client, make_user):
    """The window between the two calls is where a user closes the tab. It must cost them nothing."""
    user = make_user(role=UserRole.ADMIN)
    existing = _enrol(api_client, _tokens(api_client, user))
    tokens = _login(api_client, user, totp_code=pyotp.TOTP(existing).now()).json()

    _setup_two_factor(api_client, tokens)
    assert _login(api_client, user, totp_code=pyotp.TOTP(existing).now()).status_code == 200


def test_completing_re_enrolment_switches_which_code_signs_in(api_client, make_user):
    user = make_user(role=UserRole.ADMIN)
    existing = _enrol(api_client, _tokens(api_client, user))
    tokens = _login(api_client, user, totp_code=pyotp.TOTP(existing).now()).json()

    replacement = _enrol(api_client, tokens)
    assert _login(api_client, user, totp_code=pyotp.TOTP(replacement).now()).status_code == 200
    assert _login(api_client, user, totp_code=pyotp.TOTP(existing).now()).status_code == 401


# --------------------------------------------------------------------------- me


def test_me_describes_the_caller(api_client, make_user):
    user = make_user(role=UserRole.ACCOUNTING, language=AppLanguage.ENGLISH)
    response = api_client.get("/api/auth/me", headers=_auth(_tokens(api_client, user)))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(user.id)
    assert body["username"] == user.username
    assert body["role"] == "accounting"
    assert body["language"] == "en"
    assert body["is_2fa_enabled"] is False
    assert body["last_login_at"] is not None


@pytest.mark.parametrize(
    ("role", "required", "prompted"),
    [
        (UserRole.ADMIN, True, True),
        (UserRole.SITE_MANAGER, False, True),
        (UserRole.ACCOUNTING, False, True),
        (UserRole.EMPLOYEE, False, False),
    ],
)
def test_me_reports_the_2fa_obligation_of_each_role(api_client, make_user, role, required, prompted):
    """Requirement 1.6 as the front end sees it. Served here so a client can route straight to the
    enrolment screen instead of inferring the state from a request that failed."""
    tokens = _tokens(api_client, make_user(role=role))
    body = api_client.get("/api/auth/me", headers=_auth(tokens)).json()

    assert body["is_2fa_enrolment_required"] is required
    assert body["is_2fa_enrolment_prompted"] is prompted


def test_me_carries_no_credential_fields(api_client, make_user):
    """This response goes to a browser. Anything in it that is not needed is a field that can leak.

    The user is deliberately one with a TOTP secret and a non-zero failure counter, so the response
    has something to leak if the schema ever grows to include the whole row.
    """
    secret = pyotp.random_base32()
    user = make_user(is_2fa_enabled=True, totp_secret=secret)
    tokens = _login(api_client, user, totp_code=pyotp.TOTP(secret).now()).json()
    body = api_client.get("/api/auth/me", headers=_auth(tokens)).json()

    assert set(body) == {
        "id",
        "username",
        "role",
        "employee_id",
        "language",
        "is_2fa_enabled",
        "is_2fa_enrolment_required",
        "is_2fa_enrolment_prompted",
        "must_change_password",
        "last_login_at",
    }


def test_me_without_a_token_is_refused(api_client):
    response = api_client.get("/api/auth/me")
    assert response.status_code == 401
    assert _code(response) == "not_authenticated"


# --------------------------------------------------------------------------- language preference


def test_setting_the_language_persists_it_on_the_account(api_client, make_user):
    """Requirement 21.2: the toggle lives in the browser, but the preference belongs to the user, so
    it has to survive a fresh sign-in on another device — which is what reading it back proves."""
    user = make_user(language=AppLanguage.HEBREW)
    tokens = _tokens(api_client, user)

    response = api_client.patch("/api/auth/me/language", headers=_auth(tokens), json={"language": "en"})
    assert response.status_code == 200, response.text
    assert response.json()["language"] == "en"

    # A separate read, standing in for the next device: the preference is on the account, not the tab.
    assert api_client.get("/api/auth/me", headers=_auth(tokens)).json()["language"] == "en"


def test_setting_the_language_writes_one_audit_row(api_client, make_user, session: Session):
    from app.models.change_log import ChangeLog

    user = make_user(language=AppLanguage.HEBREW)
    tokens = _tokens(api_client, user)
    api_client.patch("/api/auth/me/language", headers=_auth(tokens), json={"language": "en"})

    rows = session.query(ChangeLog).filter_by(entity_id=user.id, field="language").all()
    assert len(rows) == 1
    assert rows[0].old_value == "he"
    assert rows[0].new_value == "en"


def test_setting_the_same_language_writes_no_audit_row(api_client, make_user, session: Session):
    """The diff writer only records a field that actually moved, so a no-op toggle is silent."""
    from app.models.change_log import ChangeLog

    user = make_user(language=AppLanguage.HEBREW)
    tokens = _tokens(api_client, user)
    api_client.patch("/api/auth/me/language", headers=_auth(tokens), json={"language": "he"})

    rows = session.query(ChangeLog).filter_by(entity_id=user.id, field="language").all()
    assert rows == []


@pytest.mark.parametrize("payload", [{}, {"language": "fr"}, {"language": ""}, {"language": "he", "x": 1}])
def test_a_malformed_language_request_is_a_validation_error(api_client, make_user, payload):
    tokens = _tokens(api_client, make_user())
    response = api_client.patch("/api/auth/me/language", headers=_auth(tokens), json=payload)
    assert response.status_code == 422


def test_setting_the_language_without_a_token_is_refused(api_client):
    response = api_client.patch("/api/auth/me/language", json={"language": "en"})
    assert response.status_code == 401
    assert _code(response) == "not_authenticated"


def test_me_with_a_rubbish_token_is_refused(api_client):
    response = api_client.get("/api/auth/me", headers={"Authorization": "Bearer nonsense"})
    assert response.status_code == 401
    assert _code(response) == "invalid_token"


def test_me_with_a_refresh_token_is_refused(api_client, make_user):
    """A refresh token lives 7 days; accepting it here would make it a 7-day access token."""
    tokens = _tokens(api_client, make_user())
    response = api_client.get("/api/auth/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"})
    assert response.status_code == 401


def test_deactivating_a_user_invalidates_their_access_token_immediately(
    api_client, make_user, session: Session
):
    """Requirement 1.4 and 20.8 as an administrator would observe them: no waiting for expiry."""
    user = make_user()
    tokens = _tokens(api_client, user)
    assert api_client.get("/api/auth/me", headers=_auth(tokens)).status_code == 200

    user.is_active = False
    user.token_version += 1
    session.commit()

    assert api_client.get("/api/auth/me", headers=_auth(tokens)).status_code == 401


# --------------------------------------------------------------------------- logout


def test_logout_returns_no_content_and_ends_the_session(api_client, make_user):
    tokens = _tokens(api_client, make_user())
    assert api_client.post("/api/auth/logout", headers=_auth(tokens)).status_code == 204
    assert api_client.get("/api/auth/me", headers=_auth(tokens)).status_code == 401


def test_logout_invalidates_the_refresh_token(api_client, make_user):
    tokens = _tokens(api_client, make_user())
    api_client.post("/api/auth/logout", headers=_auth(tokens))

    response = api_client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 401


def test_logout_without_a_token_is_refused(api_client):
    assert api_client.post("/api/auth/logout").status_code == 401


# --------------------------------------------------------------------------- self-service password change

NEW_PASSWORD = "a-brand-new-passphrase"


def _change_password(api_client, tokens, current: str, new: str):
    return api_client.post(
        "/api/auth/change-password",
        headers=_auth(tokens),
        json={"current_password": current, "new_password": new},
    )


def test_me_reports_the_password_change_obligation(api_client, make_user):
    """Requirement 1 as the front end sees it: the flag is served so the client can route to the
    change screen rather than inferring the block from a failed request."""
    tokens = _tokens(api_client, make_user(role=UserRole.SITE_MANAGER, must_change_password=True))
    body = api_client.get("/api/auth/me", headers=_auth(tokens)).json()
    assert body["must_change_password"] is True

    clear = _tokens(api_client, make_user(role=UserRole.SITE_MANAGER))
    assert api_client.get("/api/auth/me", headers=_auth(clear)).json()["must_change_password"] is False


def test_changing_the_password_clears_the_flag(api_client, make_user):
    user = make_user(role=UserRole.SITE_MANAGER, must_change_password=True)
    tokens = _tokens(api_client, user)

    response = _change_password(api_client, tokens, DEFAULT_PASSWORD, NEW_PASSWORD)
    assert response.status_code == 200, response.text
    assert response.json()["must_change_password"] is False


def test_after_changing_the_password_the_old_access_token_stops_working(api_client, make_user):
    """The change bumps the token version, so every *other* session ends. The token used to make the
    call keeps working long enough to return the response, but its next use is refused."""
    user = make_user(role=UserRole.SITE_MANAGER, must_change_password=True)
    tokens = _tokens(api_client, user)
    assert _change_password(api_client, tokens, DEFAULT_PASSWORD, NEW_PASSWORD).status_code == 200

    assert api_client.get("/api/auth/me", headers=_auth(tokens)).status_code == 401
    # The new password is the one that now signs in.
    assert _login(api_client, user, password=NEW_PASSWORD).status_code == 200


def test_change_password_with_a_wrong_current_password_is_refused(api_client, make_user):
    tokens = _tokens(api_client, make_user(role=UserRole.SITE_MANAGER, must_change_password=True))
    response = _change_password(api_client, tokens, "not-my-password", NEW_PASSWORD)
    assert response.status_code == 400
    assert _code(response) == "current_password_incorrect"


def test_change_password_to_the_same_password_is_refused(api_client, make_user):
    tokens = _tokens(api_client, make_user(role=UserRole.SITE_MANAGER, must_change_password=True))
    response = _change_password(api_client, tokens, DEFAULT_PASSWORD, DEFAULT_PASSWORD)
    assert response.status_code == 400
    assert _code(response) == "new_password_must_differ"


@pytest.mark.parametrize(
    "payload",
    [
        {"current_password": DEFAULT_PASSWORD},
        {"new_password": NEW_PASSWORD},
        {"current_password": DEFAULT_PASSWORD, "new_password": "short"},
        {"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD, "extra": 1},
    ],
)
def test_a_malformed_change_password_is_a_validation_error(api_client, make_user, payload):
    tokens = _tokens(api_client, make_user(role=UserRole.SITE_MANAGER))
    response = api_client.post("/api/auth/change-password", headers=_auth(tokens), json=payload)
    assert response.status_code == 422


def test_change_password_without_a_token_is_refused(api_client):
    response = api_client.post(
        "/api/auth/change-password",
        json={"current_password": "x", "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 401


def test_me_carries_the_password_change_flag_in_its_field_set(api_client, make_user):
    """The response shape grew by exactly one field; nothing else leaked in with it."""
    tokens = _tokens(api_client, make_user(role=UserRole.ACCOUNTING))
    body = api_client.get("/api/auth/me", headers=_auth(tokens)).json()
    assert set(body) == {
        "id",
        "username",
        "role",
        "employee_id",
        "language",
        "is_2fa_enabled",
        "is_2fa_enrolment_required",
        "is_2fa_enrolment_prompted",
        "must_change_password",
        "last_login_at",
    }
