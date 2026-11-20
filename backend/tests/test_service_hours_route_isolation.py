"""Customer and technician credentials are useless against Hours (Requirements 7.1 to 7.10, Property 9).

The routes are enumerated from the application itself, once with the service-ticket feature flag off
and once with it on, so a route added later is covered without editing the assertions. Every route
outside the Ticket API (`/api/service`), the discovery endpoint (`/api/features`) and the docs pages
must be classified, and the classification is explicit:

* **authenticated**: the dependant tree contains `get_current_user`. Derived from the app.
* **Shared_Auth_Endpoints**: an explicit list below. They are authenticated, and a technician may use
  them.
* **body-token route**: `/auth/refresh`, which reads its token from the request body, not the header.
* **no-auth**: an explicit allowlist below.

A route that is none of these fails `test_every_hours_route_is_classified`, so a new route forces a
decision. The flag has no effect on the Hours routes yet (the conditional mount is task 5.1), so the
two parametrizations see the same routes today. Both stay in so the test keeps holding once the mount
exists.

How the technician's success on the Shared_Auth_Endpoints is decided: every endpoint is called with
valid input and must answer its success status (`/auth/2fa/verify` gets a real code computed from the
secret that `/auth/2fa/setup` just issued). A status that merely differs from 403 would also pass a
broken endpoint, so none is accepted in place of the success status.

The second half of Requirement 7.5 (a customer token whose subject is a staff user id is a customer
on the Ticket API side too) belongs to the staff-router tests; here the Hours side is asserted.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import jwt
import pyotp
import pytest
from fastapi.routing import APIRoute
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.security import create_access_token, create_refresh_token
from app.main import create_app
from app.models.change_log import ChangeLog
from app.models.user import UserRole
from app.service_tickets import customer_token
from app.service_tickets.models import Customer
from auth_support import DEFAULT_PASSWORD

REJECTED = {"detail": {"error": {"code": "customer_token_not_accepted"}}}
INVALID_TOKEN = {"detail": {"error": {"code": "invalid_token"}}}

#: The failure-path headers that are fixed. Anything else (date, request id) varies per request.
_FIXED_HEADERS = ("www-authenticate", "content-type")

# --------------------------------------------------------------------------- explicit classification

AUTHENTICATED = "authenticated"
SHARED = "shared-auth"
BODY_TOKEN = "body-token"
NO_AUTH = "no-auth"

#: The existing endpoints that establish or maintain the caller's own staff session, and that depend on
#: `get_current_user` rather than on `get_enrolled_user`: sign-out, current-user, change-own-password.
#: The language preference and the two 2FA enrolment endpoints are in the same router and take the same
#: dependency (the router's own comment calls that the whole exception list), so a technician reaches
#: them too; they are listed here so a change to that is a decision and not an accident.
SHARED_AUTH_ENDPOINTS = frozenset(
    {
        ("POST", "/api/auth/logout"),
        ("GET", "/api/auth/me"),
        ("POST", "/api/auth/change-password"),
        ("PATCH", "/api/auth/me/language"),
        ("POST", "/api/auth/2fa/setup"),
        ("POST", "/api/auth/2fa/verify"),
    }
)

#: A Shared_Auth_Endpoint whose token is in the body: no `get_current_user`, but a customer token there
#: still has to be a 403 (Requirement 7.2).
BODY_TOKEN_ROUTES = frozenset({("POST", "/api/auth/refresh")})

#: Routes that need no token. Sign-in is a Shared_Auth_Endpoint too, and a customer token in its
#: header must change nothing (Requirement 7.10).
NO_AUTH_ALLOWLIST = frozenset(
    {
        ("GET", "/api/health"),
        ("GET", "/api/health/ready"),
        ("POST", "/api/auth/login"),
    }
)

#: A fixed body for the routes that would otherwise answer 422 for a missing body, so that the response
#: with a token and without one is the response to the same request.
_FIXED_BODIES = {
    ("POST", "/api/auth/login"): {"username": "nobody", "password": "wrong"},
    ("POST", "/api/auth/refresh"): {"refresh_token": "not-a-token"},
}

#: Outside the Hours isolation: the Ticket API, its discovery endpoint and the docs pages.
_EXCLUDED_PREFIXES = ("/api/service", "/api/features")
_DOCS_PATHS = ("/docs", "/openapi.json", "/redoc")

_PATH_VALUES = {"year": "2025", "month": "8"}

FLAGS = (("false", "flag-off"), ("true", "flag-on"))


class RouteCase(NamedTuple):
    flag: str
    method: str
    path: str
    kind: str


def _requires_authentication(dependant) -> bool:
    return any(
        sub.call is get_current_user or _requires_authentication(sub) for sub in dependant.dependencies
    )


def _is_excluded(path: str) -> bool:
    under_prefix = any(path == prefix or path.startswith(prefix + "/") for prefix in _EXCLUDED_PREFIXES)
    return under_prefix or path.startswith(_DOCS_PATHS)


def _concrete(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: _PATH_VALUES.get(m.group(1), str(uuid.uuid4())), path)


def _build_app(flag: str):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("SERVICE_TICKETS_ENABLED", flag)
        get_settings.cache_clear()
        try:
            return create_app(get_settings())
        finally:
            get_settings.cache_clear()


def _classify(method: str, path: str, authenticated: bool) -> tuple[str | None, str | None]:
    """The class of one (method, path) and, when it has none, what the developer has to do."""
    key = (method, path)
    where = "tests/test_service_hours_route_isolation.py"
    if key in SHARED_AUTH_ENDPOINTS:
        if authenticated:
            return SHARED, None
        return None, f"{method} {path} is in SHARED_AUTH_ENDPOINTS but no longer needs get_current_user"
    if key in BODY_TOKEN_ROUTES or key in NO_AUTH_ALLOWLIST:
        if authenticated:
            return None, f"{method} {path} is in a no-token list in {where} but now needs get_current_user"
        return (BODY_TOKEN if key in BODY_TOKEN_ROUTES else NO_AUTH), None
    if authenticated:
        return AUTHENTICATED, None
    return None, (
        f"{method} {path} requires no authentication and is not classified. Decide whether it should "
        f"need a staff token (add get_current_user, or EnrolledUser/a Caller) or add it to "
        f"NO_AUTH_ALLOWLIST (or BODY_TOKEN_ROUTES) in {where}."
    )


def _enumerate(flag: str) -> tuple[list[RouteCase], list[str]]:
    """Every (method, path) outside the Ticket API and the docs, with its class, and the problems."""
    cases: list[RouteCase] = []
    problems: list[str] = []
    for route in _build_app(flag).routes:
        path = getattr(route, "path", None)
        if path is None:
            problems.append(f"route {route!r} has no path and cannot be classified")
            continue
        if _is_excluded(path):
            continue
        if not isinstance(route, APIRoute):
            problems.append(f"{path} is a {type(route).__name__}, not an API route; classify it by hand")
            continue
        authenticated = _requires_authentication(route.dependant)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            kind, problem = _classify(method, path, authenticated)
            if problem is not None:
                problems.append(problem)
            else:
                cases.append(RouteCase(flag, method, path, kind))
    return cases, problems


_ENUMERATED = {flag: _enumerate(flag) for flag, _ in FLAGS}
_FLAG_IDS = dict(FLAGS)


def _params(*kinds: str):
    return [
        pytest.param(*case, id=f"{_FLAG_IDS[case.flag]}:{case.method} {case.path}")
        for flag, _ in FLAGS
        for case in _ENUMERATED[flag][0]
        if case.kind in kinds
    ]


_FIELDS = "flag,method,path,kind"
_FLAG_PARAMS = [pytest.param(flag, id=label) for flag, label in FLAGS]


# --------------------------------------------------------------------------- tokens and requests


def _valid_customer_token(customer_id: uuid.UUID | None = None) -> str:
    return customer_token.mint_customer_token(customer_id or uuid.uuid4(), 0)


def _expired_customer_token() -> str:
    return customer_token.mint_customer_token(
        uuid.uuid4(), 0, now=datetime.now(UTC) - timedelta(hours=9), ttl=28800
    )


def _badly_signed_customer_token() -> str:
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
    return jwt.encode(claims, get_settings().jwt_secret_key.get_secret_value(), algorithm="HS256")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _call(client, method: str, path: str, token: str | None = None):
    headers = _bearer(token) if token is not None else {}
    return client.request(method, _concrete(path), headers=headers, json=_FIXED_BODIES.get((method, path)))


def _view(response) -> tuple[int, object, dict[str, str]]:
    """Status, body and fixed headers, with the readiness latencies (which vary) taken out."""
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if isinstance(body, dict) and isinstance(body.get("checks"), list):
        body = {**body, "checks": [{k: v for k, v in c.items() if k != "latency_ms"} for c in body["checks"]]}
    fixed = {name: response.headers[name] for name in _FIXED_HEADERS if name in response.headers}
    return response.status_code, body, fixed


def _change_log_rows(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(ChangeLog))


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def flagged_client(request, settings, monkeypatch, session: Session) -> Iterator:
    """An app built with the service-tickets flag patched to the parametrized value.

    `settings` is requested before `monkeypatch` so that on teardown the environment is restored first
    and the settings cache is cleared after, leaving no flag-on settings behind.
    """
    from fastapi.testclient import TestClient

    from app.db.session import get_db

    flag = request.node.callspec.params["flag"]
    monkeypatch.setenv("SERVICE_TICKETS_ENABLED", flag)
    get_settings.cache_clear()
    assert get_settings().service_tickets_enabled == flag
    app = create_app(get_settings())
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def staff_user(make_user):
    return make_user()


@pytest.fixture
def technician(make_user):
    return make_user(role=UserRole.TECHNICIAN, must_change_password=False)


def _access(user, *, now: datetime | None = None) -> str:
    return create_access_token(user_id=user.id, token_version=user.token_version, now=now)


# --------------------------------------------------------------------------- the classification itself


@pytest.mark.parametrize("flag", _FLAG_PARAMS)
def test_every_hours_route_is_classified(flag):
    """A route in no list fails here, with the instruction to classify it."""
    cases, problems = _ENUMERATED[flag]

    assert not problems, "\n" + "\n".join(problems)
    assert len([c for c in cases if c.kind == AUTHENTICATED]) > 50


@pytest.mark.parametrize("flag", _FLAG_PARAMS)
def test_the_explicit_lists_name_routes_that_exist(flag):
    """A renamed or removed route must not leave a stale entry that quietly exempts nothing."""
    found = {(case.method, case.path) for case in _ENUMERATED[flag][0]}
    listed = SHARED_AUTH_ENDPOINTS | BODY_TOKEN_ROUTES | NO_AUTH_ALLOWLIST

    assert listed <= found, f"listed but not registered: {sorted(listed - found)}"


# --------------------------------------------------------------------------- the per-route test


@pytest.mark.parametrize(_FIELDS, _params(AUTHENTICATED, SHARED, BODY_TOKEN, NO_AUTH))
def test_hours_routes_reject_customer_and_technician_tokens(
    flagged_client, session: Session, staff_user, technician, flag, method, path, kind
):
    """Requirements 7.1, 7.2, 7.3, 7.5, 7.8, 7.9 and 7.10 for one route."""
    random_customer = _valid_customer_token()
    staff_id_customer = _valid_customer_token(staff_user.id)
    expired_customer = _expired_customer_token()
    forged_customer = _badly_signed_customer_token()
    rows_before = _change_log_rows(session)
    version_before = staff_user.token_version

    if kind in (AUTHENTICATED, SHARED):
        for token in (random_customer, staff_id_customer):
            response = _call(flagged_client, method, path, token)
            assert response.status_code == 403, response.text
            assert response.json() == REJECTED
            assert "www-authenticate" not in response.headers

        expired_staff = _view(_call(flagged_client, method, path, _access(staff_user, now=_hours_ago(1))))
        assert expired_staff[0] == 401
        assert expired_staff[1] == INVALID_TOKEN
        for token in (expired_customer, forged_customer):
            assert _view(_call(flagged_client, method, path, token)) == expired_staff

        if kind == AUTHENTICATED:
            response = _call(flagged_client, method, path, _access(technician))
            assert response.status_code == 403, response.text
            assert response.json()["detail"]["error"]["code"] == "insufficient_role"
    else:
        # No token needed: a customer token, valid or not, changes nothing about the response.
        baseline = _view(_call(flagged_client, method, path))
        for token in (random_customer, staff_id_customer, expired_customer, forged_customer):
            assert _view(_call(flagged_client, method, path, token)) == baseline

    assert _change_log_rows(session) == rows_before
    session.refresh(staff_user)
    assert staff_user.token_version == version_before


def _hours_ago(hours: int) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


# --------------------------------------------------------------------------- the technician on shared routes


def _technician_me(client, user):
    response = client.get("/api/auth/me", headers=_bearer(_access(user)))
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "technician"


def _technician_logout(client, user):
    response = client.post("/api/auth/logout", headers=_bearer(_access(user)))
    assert response.status_code == 204, response.text


def _technician_change_password(client, user):
    response = client.post(
        "/api/auth/change-password",
        json={"current_password": DEFAULT_PASSWORD, "new_password": "a-different-long-password-1"},
        headers=_bearer(_access(user)),
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "technician"


def _technician_language(client, user):
    response = client.patch("/api/auth/me/language", json={"language": "en"}, headers=_bearer(_access(user)))
    assert response.status_code == 200, response.text
    assert response.json()["language"] == "en"


def _technician_two_factor(client, user):
    headers = _bearer(_access(user))
    setup = client.post("/api/auth/2fa/setup", headers=headers)
    assert setup.status_code == 200, setup.text
    code = pyotp.TOTP(setup.json()["secret"]).now()
    verify = client.post("/api/auth/2fa/verify", json={"totp_code": code}, headers=headers)
    assert verify.status_code == 200, verify.text


def _technician_two_factor_setup(client, user):
    response = client.post("/api/auth/2fa/setup", headers=_bearer(_access(user)))
    assert response.status_code == 200, response.text


def _technician_refresh(client, user):
    refresh, _ = create_refresh_token(user_id=user.id, token_version=user.token_version)
    response = client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert response.status_code == 200, response.text
    assert response.json()["access_token"]


def _technician_login(client, user):
    response = client.post("/api/auth/login", json={"username": user.username, "password": DEFAULT_PASSWORD})
    assert response.status_code == 200, response.text
    assert client.get("/api/auth/me", headers=_bearer(response.json()["access_token"])).status_code == 200


#: What a technician does on each Shared_Auth_Endpoint, with valid input and its success status.
#: `/auth/2fa/setup` and `/auth/2fa/verify` are one flow, so the verify case runs setup first.
_TECHNICIAN_CALLS: dict[tuple[str, str], Callable] = {
    ("GET", "/api/auth/me"): _technician_me,
    ("POST", "/api/auth/logout"): _technician_logout,
    ("POST", "/api/auth/change-password"): _technician_change_password,
    ("PATCH", "/api/auth/me/language"): _technician_language,
    ("POST", "/api/auth/2fa/setup"): _technician_two_factor_setup,
    ("POST", "/api/auth/2fa/verify"): _technician_two_factor,
    ("POST", "/api/auth/refresh"): _technician_refresh,
    ("POST", "/api/auth/login"): _technician_login,
}


def test_every_shared_auth_endpoint_has_a_technician_call():
    assert set(_TECHNICIAN_CALLS) == SHARED_AUTH_ENDPOINTS | BODY_TOKEN_ROUTES | {("POST", "/api/auth/login")}


@pytest.mark.parametrize(
    "flag,method,path",
    [
        pytest.param(flag, method, path, id=f"{label}:{method} {path}")
        for flag, label in FLAGS
        for method, path in sorted(_TECHNICIAN_CALLS)
    ],
)
def test_a_technician_succeeds_on_the_shared_auth_endpoints(flagged_client, technician, flag, method, path):
    """Requirement 7.7, whatever the flag says."""
    _TECHNICIAN_CALLS[(method, path)](flagged_client, technician)


# ------------------------------------------------------------------------- customer token in the refresh body


@pytest.mark.parametrize("flag", _FLAG_PARAMS)
def test_a_customer_token_in_the_refresh_body_gets_403(flagged_client, session: Session, staff_user, flag):
    rows_before = _change_log_rows(session)
    version_before = staff_user.token_version

    for token in (_valid_customer_token(), _valid_customer_token(staff_user.id)):
        response = flagged_client.post("/api/auth/refresh", json={"refresh_token": token})

        assert response.status_code == 403, response.text
        assert response.json() == REJECTED
        assert "www-authenticate" not in response.headers

    garbage = _view(flagged_client.post("/api/auth/refresh", json={"refresh_token": "not-a-token"}))
    assert garbage[0] == 401
    for token in (_expired_customer_token(), _badly_signed_customer_token()):
        response = flagged_client.post("/api/auth/refresh", json={"refresh_token": token})
        assert _view(response) == garbage
    assert _change_log_rows(session) == rows_before
    session.refresh(staff_user)
    assert staff_user.token_version == version_before


# ----------------------------------------------------------------------- staff sign-in, customer credentials

#: Requirement 4.8: seven spellings of one Normalized_Phone.
PHONE_SPELLINGS = [
    "0505556677",
    "050-555-6677",
    "050 555 6677",
    "(050) 555-6677",
    "+972505556677",
    "+972 50-555-6677",
    "972505556677",
]


@pytest.mark.parametrize("spelling", PHONE_SPELLINGS)
@pytest.mark.parametrize("flag", _FLAG_PARAMS)
def test_staff_login_with_a_customers_phone_and_password_is_the_unknown_username_response(
    flagged_client, session: Session, default_password_hash, flag, spelling
):
    """Requirement 7.6: the customer's phone and password are no staff credential and no oracle."""
    customer = Customer(
        phone="0505556677",
        full_name="Test Customer",
        full_name_key="test customer",
        password_hash=default_password_hash,
    )
    session.add(customer)
    session.commit()
    rows_before = _change_log_rows(session)

    def sign_in(username: str):
        payload = {"username": username, "password": DEFAULT_PASSWORD}
        return flagged_client.post("/api/auth/login", json=payload)

    unknown = sign_in("0509998877")
    response = sign_in(spelling)

    assert unknown.status_code == 401
    assert _view(response) == _view(unknown)
    assert response.json() == {"detail": {"error": {"code": "invalid_credentials"}}}
    assert "access_token" not in response.text
    assert _change_log_rows(session) == rows_before
    session.refresh(customer)
    assert customer.token_version == 1
