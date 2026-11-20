"""Feature discovery endpoint, preflight guard and conditional mount (Requirements 20.5, 21.1, 21.3)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from app.core.security_headers import SecurityHeadersMiddleware
from app.main import create_app
from app.service_tickets import mount as mount_module
from app.service_tickets.api.preflight_guard import PreflightGuard

PREFLIGHT = {"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"}
CONTACT = "access@example.test"


def _app(settings, *, enabled: bool, **updates):
    changes = {
        "service_tickets_enabled": "true" if enabled else "",
        "service_accessibility_contact": CONTACT,
        **updates,
    }
    return create_app(settings.model_copy(update=changes))


def _client(settings, *, enabled: bool, **updates) -> TestClient:
    return TestClient(_app(settings, enabled=enabled, **updates))


def _snapshot(response):
    return response.status_code, response.content, list(response.headers.items())


# --------------------------------------------------------------------------- /api/features


def test_features_flag_off(settings):
    response = _client(settings, enabled=False).get("/api/features")

    assert response.status_code == 200
    assert response.json() == {"service_tickets": False}
    assert response.headers["cache-control"] == "no-store"
    assert "accessibility_contact" not in response.json()


def test_features_flag_on_adds_the_contact(settings):
    response = _client(settings, enabled=True).get("/api/features")

    assert response.status_code == 200
    assert response.json() == {"service_tickets": True, "accessibility_contact": CONTACT}
    assert response.headers["cache-control"] == "no-store"


def test_features_route_has_no_dependency(settings):
    """No session, no caller: the route depends on nothing, so it can touch no table."""
    app = _app(settings, enabled=True)
    routes = [route for route in app.routes if getattr(route, "path", "") == "/api/features"]

    assert len(routes) == 1
    assert routes[0].dependant.dependencies == []


# --------------------------------------------------------------------------- preflight guard


@pytest.mark.parametrize("path", ["/api/service", "/api/service/", "/api/service/x", "/api/service/a/b"])
def test_flag_off_preflight_under_the_ticket_prefix_equals_an_unknown_path(settings, path):
    client = _client(settings, enabled=False)
    unknown = client.options("/api/nothing-here/x")
    assert unknown.status_code == 404
    assert unknown.content == b'{"detail":"Not Found"}'

    guarded = client.options(path, headers=PREFLIGHT)

    assert _snapshot(guarded) == _snapshot(unknown)
    assert "access-control-allow-origin" not in guarded.headers


def test_flag_off_guard_reply_equals_an_unknown_path_with_hsts_on(settings):
    client = _client(settings, enabled=False, hsts_enabled=True)

    guarded = client.options("/api/service/x", headers=PREFLIGHT)
    unknown = client.options("/api/nothing-here/x")

    assert "strict-transport-security" in unknown.headers
    assert _snapshot(guarded) == _snapshot(unknown)


def test_flag_off_other_methods_under_the_ticket_prefix_are_the_unknown_path_404(settings):
    client = _client(settings, enabled=False)
    unknown = client.get("/api/nothing-here/x")

    assert _snapshot(client.get("/api/service/x")) == _snapshot(unknown)
    assert _snapshot(client.post("/api/service/x", json={})) == _snapshot(unknown)


@pytest.mark.parametrize("path", ["/api/services", "/api/servicex", "/api/service-x/y", "/api/auth/login"])
def test_flag_off_preflight_elsewhere_is_unchanged(settings, path):
    off = _client(settings, enabled=False).options(path, headers=PREFLIGHT)
    on = _client(settings, enabled=True).options(path, headers=PREFLIGHT)

    assert off.status_code == 200
    assert off.headers["access-control-allow-origin"] == PREFLIGHT["Origin"]
    assert _snapshot(off) == _snapshot(on)


def test_flag_on_preflight_under_the_ticket_prefix_is_not_intercepted(settings):
    response = _client(settings, enabled=True).options("/api/service/x", headers=PREFLIGHT)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == PREFLIGHT["Origin"]


def test_flag_off_normal_hours_request_is_untouched(settings):
    response = _client(settings, enabled=False).get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_guard_is_outermost_only_while_the_flag_is_off(settings):
    off = [m.cls for m in _app(settings, enabled=False).user_middleware]
    on = [m.cls for m in _app(settings, enabled=True).user_middleware]

    # `user_middleware` lists the outermost first.
    assert off == [PreflightGuard, CORSMiddleware, SecurityHeadersMiddleware]
    assert on == [CORSMiddleware, SecurityHeadersMiddleware]


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
def test_guard_passes_non_http_scopes_through(scope_type):
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope)

    guard = PreflightGuard(inner, api_prefix="/api", hsts_enabled=False, hsts_max_age_seconds=1)
    scope = {"type": scope_type, "path": "/api/service/x", "method": "OPTIONS"}

    asyncio.run(guard(scope, None, None))

    assert seen == [scope]


# --------------------------------------------------------------------------- mount


def test_mount_includes_the_routers_only_while_the_flag_is_on(settings, monkeypatch):
    router = APIRouter()

    @router.get("/probe")
    def probe():
        return {}

    monkeypatch.setattr(mount_module, "_ticket_routers", lambda: [router])

    off_paths = {r.path for r in _app(settings, enabled=False).routes}
    on_paths = {r.path for r in _app(settings, enabled=True).routes}

    assert "/api/service/probe" not in off_paths
    assert "/api/service/probe" in on_paths


def test_mount_adds_no_route_today(settings):
    assert {r.path for r in _app(settings, enabled=True).routes} == {
        r.path for r in _app(settings, enabled=False).routes
    }