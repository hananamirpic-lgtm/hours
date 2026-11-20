"""A technician is refused on every Hours route (Requirements 7.3, 7.4, 20.7).

`get_enrolled_user` answers 403 `insufficient_role` for the technician role before anything else, and
every Hours router depends on it, directly or through a `Caller`. The routes are enumerated from the
application itself, so a router added later is covered without editing this file. The auth router is
the one exception: it depends on `CurrentUser`, which is what lets a technician sign in, refresh, sign
out and change their own password.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.main import create_app
from app.models.change_log import ChangeLog
from app.models.user import UserRole
from auth_support import DEFAULT_PASSWORD

_PATH_VALUES = {"year": "2025", "month": "8"}
_NOT_HOURS_PATHS = ("/docs", "/openapi.json", "/redoc")
# The service-ticket discovery endpoint (Requirement 20.5) is not an Hours route.
_DISCOVERY_PATH = "/api/features"


def _requires_authentication(dependant) -> bool:
    return any(
        sub.call is get_current_user or _requires_authentication(sub) for sub in dependant.dependencies
    )


def _hours_routes() -> list[tuple[str, str, bool]]:
    """(method, path, requires authentication) for every route outside the auth router and the docs."""
    app = create_app(get_settings())
    auth_prefix = f"{get_settings().api_prefix}/auth"
    found = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path.startswith(auth_prefix) or route.path.startswith(_NOT_HOURS_PATHS):
            continue
        if route.path == _DISCOVERY_PATH:
            continue
        authenticated = _requires_authentication(route.dependant)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            found.append((method, route.path, authenticated))
    return found


def _concrete(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: _PATH_VALUES.get(m.group(1), str(uuid.uuid4())), path)


AUTHENTICATED_ROUTES = [(m, p) for m, p, authenticated in _hours_routes() if authenticated]


@pytest.fixture
def technician_client(settings, session: Session, make_user) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    technician = make_user(role=UserRole.TECHNICIAN, must_change_password=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login", json={"username": technician.username, "password": DEFAULT_PASSWORD}
        )
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        yield client
    app.dependency_overrides.clear()


def test_the_route_enumeration_finds_the_hours_routes():
    """Guards the parametrization below against silently collapsing to nothing."""
    assert len(AUTHENTICATED_ROUTES) > 50


def test_every_unauthenticated_hours_route_is_a_health_probe():
    """A route that needs no token would escape the technician check, so any new one must be looked at."""
    open_paths = {path for _, path, authenticated in _hours_routes() if not authenticated}
    assert open_paths == {"/api/health", "/api/health/ready"}


@pytest.mark.parametrize(("method", "path"), AUTHENTICATED_ROUTES, ids=lambda value: str(value))
def test_a_technician_gets_403_insufficient_role_and_no_audit_row(
    technician_client, session: Session, method: str, path: str
):
    rows_before = session.scalar(select(func.count()).select_from(ChangeLog))

    response = technician_client.request(method, _concrete(path))

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"]["code"] == "insufficient_role"
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == rows_before