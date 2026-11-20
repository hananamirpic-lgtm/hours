"""Feature discovery endpoint, preflight guard and conditional mount (Requirements 20.5, 21.1, 21.3)."""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass, field

import boto3
import boto3.session
import pytest
import redis
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, text
from starlette.middleware.cors import CORSMiddleware

from app.core import redis as app_redis
from app.core import storage as app_storage
from app.core.config import get_settings
from app.core.security import create_access_token
from app.core.security_headers import SecurityHeadersMiddleware
from app.db import session as db_session
from app.main import create_app
from app.repositories import health as health_repository
from app.service_tickets import mount as mount_module
from app.service_tickets.api.preflight_guard import PreflightGuard
from app.service_tickets.customer_token import mint_customer_token

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


# =========================================================================== task 5.2 additions
# Requirements 20.5, 21.1, 21.2, 21.3 and 21.4.

BODY_METHODS = {"POST", "PUT", "PATCH"}
TICKET_PATHS = ["/api/service", "/api/service/auth/sign-in", "/api/service/tickets/10000"]
UNKNOWN_PATH = "/api/nothing-here/x"


def _expected_features(enabled: bool) -> dict[str, object]:
    if enabled:
        return {"service_tickets": True, "accessibility_contact": CONTACT}
    return {"service_tickets": False}


def _auth_headers() -> dict[str, dict[str, str]]:
    """One header set per token state: none, a valid customer token, a valid staff token, garbage."""
    customer = mint_customer_token(uuid.uuid4(), 1)
    staff = create_access_token(user_id=uuid.uuid4(), token_version=1, role="admin")
    return {
        "no token": {},
        "customer token": {"Authorization": f"Bearer {customer}"},
        "staff token": {"Authorization": f"Bearer {staff}"},
        "garbage": {"Authorization": "Bearer not.a.token"},
    }


# --------------------------------------------------------------------------- the resource spy


@dataclass
class ResourceSpy:
    """Records every use of the database, Redis and object storage while it is installed."""

    #: Names of the tripped hooks, in order. Empty means nothing was touched.
    calls: list[str] = field(default_factory=list)
    #: Every SQL statement seen by a `before_cursor_execute` listener.
    statements: list[str] = field(default_factory=list)
    #: `module.attribute` of every name replaced by a tripping stub, for the report.
    patched: list[str] = field(default_factory=list)
    original_get_db: object = None

    def trip(self, name: str):
        def tripped(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"{name} must not be used")

        return tripped

    def override_get_db(self):
        """Dependency override for `get_db`: a route that asks for a session trips the spy."""
        self.calls.append("dependency get_db")
        raise AssertionError("get_db must not be used")

    def assert_untouched(self) -> None:
        assert self.calls == []
        assert self.statements == []


@pytest.fixture
def spy(monkeypatch, sqlite_engine):
    """Make any database, Redis or object-store use raise, and count SQL on the test engine.

    Patched names (every module in `app` that holds a reference to the original is rebound, so
    `from x import name` copies are covered too):

    * `app.db.session`: `get_engine`, `get_session_factory`, `get_db`
    * `app.core.redis.get_redis_client`, plus `redis.Redis.from_url` and `redis.Redis.execute_command`
    * `app.repositories.health`: `get_object_storage_client`, `get_object_storage_presign_client`
    * `app.core.storage`: `get_object_storage` and `ObjectStorage.__init__`
    * `boto3.client` and `boto3.session.Session.client`

    A `before_cursor_execute` listener on `sqlite_engine` (the test engine) and on the `Engine` class
    appends every statement to `spy.statements`.
    """
    recorder = ResourceSpy(original_get_db=db_session.get_db)

    targets = {
        "get_engine": db_session.get_engine,
        "get_session_factory": db_session.get_session_factory,
        "get_db": db_session.get_db,
        "get_redis_client": app_redis.get_redis_client,
        "get_object_storage_client": health_repository.get_object_storage_client,
        "get_object_storage_presign_client": health_repository.get_object_storage_presign_client,
        "get_object_storage": app_storage.get_object_storage,
    }
    stubs = {original: recorder.trip(name) for name, original in targets.items()}
    for module_name, module in list(sys.modules.items()):
        if module is None or not (module_name == "app" or module_name.startswith("app.")):
            continue
        for attribute, value in list(vars(module).items()):
            for original, stub in stubs.items():
                if value is original:
                    monkeypatch.setattr(module, attribute, stub)
                    recorder.patched.append(f"{module_name}.{attribute}")

    monkeypatch.setattr(redis.Redis, "from_url", recorder.trip("redis.Redis.from_url"))
    monkeypatch.setattr(redis.Redis, "execute_command", recorder.trip("redis.Redis.execute_command"))
    monkeypatch.setattr(app_storage.ObjectStorage, "__init__", recorder.trip("ObjectStorage.__init__"))
    monkeypatch.setattr(boto3, "client", recorder.trip("boto3.client"))
    monkeypatch.setattr(boto3.session.Session, "client", recorder.trip("boto3.session.Session.client"))
    recorder.patched += [
        "redis.Redis.from_url",
        "redis.Redis.execute_command",
        "app.core.storage.ObjectStorage.__init__",
        "boto3.client",
        "boto3.session.Session.client",
    ]

    def count(connection, cursor, statement, parameters, context, executemany):
        recorder.statements.append(statement)

    event.listen(sqlite_engine, "before_cursor_execute", count)
    event.listen(Engine, "before_cursor_execute", count)
    try:
        yield recorder
    finally:
        event.remove(Engine, "before_cursor_execute", count)
        event.remove(sqlite_engine, "before_cursor_execute", count)


def _spied_client(settings, recorder: ResourceSpy, *, enabled: bool, **updates) -> TestClient:
    """A client (no lifespan) whose `get_db` dependency trips the spy."""
    built = _app(settings, enabled=enabled, **updates)
    built.dependency_overrides[recorder.original_get_db] = recorder.override_get_db
    return TestClient(built)


def test_the_spy_trips_on_every_resource_it_guards(spy):
    for use in (
        db_session.get_engine,
        db_session.get_session_factory,
        app_redis.get_redis_client,
        health_repository.get_object_storage_client,
        health_repository.get_object_storage_presign_client,
        app_storage.get_object_storage,
        lambda: redis.Redis.from_url("redis://127.0.0.1:1/0"),
        lambda: boto3.client("s3"),
    ):
        with pytest.raises(AssertionError):
            use()
    assert len(spy.calls) == 8

    assert spy.statements == []
    # The listener works: a statement on the test engine is counted.
    from sqlalchemy import create_engine

    probe = create_engine("sqlite+pysqlite:///:memory:")
    with probe.connect() as connection:
        connection.execute(text("SELECT 1"))
    assert spy.statements == ["SELECT 1"]

    for name in ("get_engine", "get_session_factory", "get_db"):
        assert f"app.db.session.{name}" in spy.patched
    assert "app.core.redis.get_redis_client" in spy.patched
    assert "app.repositories.health.get_object_storage_client" in spy.patched
    assert "app.core.storage.get_object_storage_client" in spy.patched


# --------------------------------------------------------------------------- /api/features: no resources


@pytest.mark.parametrize("enabled", [False, True])
def test_features_touches_no_database_redis_or_object_store(settings, spy, enabled):
    response = _spied_client(settings, spy, enabled=enabled).get("/api/features")

    assert response.status_code == 200
    assert response.json() == _expected_features(enabled)
    assert spy.calls == []
    assert spy.statements == []


# --------------------------------------------------------------------------- preflight: other paths, flag on


def _app_without_the_guard(settings):
    """The flag-off application with the guard removed: what answered before the guard existed."""
    built = _app(settings, enabled=False)
    built.user_middleware = [m for m in built.user_middleware if m.cls is not PreflightGuard]
    assert [m.cls for m in built.user_middleware] == [CORSMiddleware, SecurityHeadersMiddleware]
    return built


@pytest.mark.parametrize("path", ["/api/services", "/api/auth/login"])
@pytest.mark.parametrize("headers", [PREFLIGHT, {}], ids=["preflight", "plain-options"])
def test_flag_off_options_on_other_paths_is_answered_as_before_the_guard(settings, path, headers):
    guarded = TestClient(_app(settings, enabled=False)).options(path, headers=headers)
    before_the_guard = TestClient(_app_without_the_guard(settings)).options(path, headers=headers)

    assert _snapshot(guarded) == _snapshot(before_the_guard)
    if headers:
        assert guarded.status_code == 200
        assert guarded.content == b"OK"
        assert guarded.headers["access-control-allow-origin"] == PREFLIGHT["Origin"]
        assert guarded.headers["access-control-allow-credentials"] == "true"
        assert "access-control-allow-methods" in guarded.headers


@pytest.mark.parametrize("headers", [PREFLIGHT, {}], ids=["preflight", "plain-options"])
def test_flag_on_options_under_the_ticket_prefix_is_handled_like_any_other_path(settings, headers):
    client = _client(settings, enabled=True)

    ticket = client.options("/api/service/x", headers=headers)
    neighbour = client.options(UNKNOWN_PATH, headers=headers)

    assert _snapshot(ticket) == _snapshot(neighbour)
    if headers:
        assert ticket.status_code == 200
        assert ticket.headers["access-control-allow-origin"] == PREFLIGHT["Origin"]


# --------------------------------------------------------------------------- /api/features ignores tokens


@pytest.mark.parametrize("enabled", [False, True])
def test_features_ignores_the_token(settings, spy, enabled):
    client = _spied_client(settings, spy, enabled=enabled)
    responses = {
        label: client.get("/api/features", headers=headers) for label, headers in _auth_headers().items()
    }

    for label, response in responses.items():
        assert response.status_code == 200, label
        assert response.json() == _expected_features(enabled), label
        assert response.headers.get_list("cache-control") == ["no-store"], label
    assert len({response.content for response in responses.values()}) == 1
    spy.assert_untouched()


@pytest.mark.parametrize("enabled", [False, True])
def test_post_features_is_what_the_app_gives_today(settings, enabled):
    response = _client(settings, enabled=enabled).post("/api/features", json={})

    # Recorded, not designed: the discovery route is GET only, so the router answers 405 with an
    # `Allow` header, and no `Cache-Control` is added.
    assert response.status_code == 405
    assert response.json() == {"detail": "Method Not Allowed"}
    assert response.headers["allow"] == "GET"
    assert "cache-control" not in response.headers


# --------------------------------------------------------------------------- the flag is read once


def test_the_flag_is_read_once_at_application_start(monkeypatch):
    monkeypatch.setenv("SERVICE_ACCESSIBILITY_CONTACT", CONTACT)
    monkeypatch.setenv("SERVICE_TICKETS_ENABLED", "true")
    get_settings.cache_clear()
    try:
        built_on = create_app()

        monkeypatch.setenv("SERVICE_TICKETS_ENABLED", "false")
        get_settings.cache_clear()
        assert get_settings().service_tickets_enabled == "false"
        built_off = create_app()
    finally:
        get_settings.cache_clear()

    still_on = TestClient(built_on).get("/api/features")
    now_off = TestClient(built_off).get("/api/features")

    assert still_on.json() == {"service_tickets": True, "accessibility_contact": CONTACT}
    assert now_off.json() == {"service_tickets": False}
    # The guard follows the same single read.
    assert PreflightGuard not in [m.cls for m in built_on.user_middleware]
    assert PreflightGuard in [m.cls for m in built_off.user_middleware]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", False), ("yes", False), ("TRUE ", True), (" true", True)],
)
def test_the_flag_value_is_compared_after_trimming_and_ignoring_case(settings, raw, expected):
    built = create_app(
        settings.model_copy(update={"service_tickets_enabled": raw, "service_accessibility_contact": CONTACT})
    )

    body = TestClient(built).get("/api/features").json()

    assert body["service_tickets"] is expected
    assert ("accessibility_contact" in body) is expected


# --------------------------------------------------------------------------- flag off: prefix is unknown


@pytest.mark.parametrize("path", TICKET_PATHS)
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_flag_off_every_method_and_token_gets_the_unknown_path_answer(settings, spy, method, path):
    client = _spied_client(settings, spy, enabled=False)

    for label, headers in _auth_headers().items():
        options = {"headers": headers}
        if method in BODY_METHODS:
            options["headers"] = {**headers, "Content-Type": "application/json"}
            options["content"] = b'{"phone": "0505556677", "password": "x"}'
        unknown = client.request(method, UNKNOWN_PATH, **options)
        actual = client.request(method, path, **options)

        assert unknown.status_code == 404, (method, label)
        assert unknown.content == b'{"detail":"Not Found"}', (method, label)
        assert _snapshot(actual) == _snapshot(unknown), (method, path, label)

    spy.assert_untouched()


@pytest.mark.parametrize("path", TICKET_PATHS)
def test_flag_off_preflight_with_any_token_gets_the_unknown_path_answer(settings, spy, path):
    client = _spied_client(settings, spy, enabled=False)

    for label, headers in _auth_headers().items():
        # The reference is a plain OPTIONS on the unknown path: a preflight there would be answered
        # by CORS, which is exactly what the guard must not do under the ticket prefix.
        unknown = client.options(UNKNOWN_PATH, headers=headers)
        actual = client.options(path, headers={**headers, **PREFLIGHT})

        assert unknown.status_code == 404, label
        assert _snapshot(actual) == _snapshot(unknown), (path, label)

    spy.assert_untouched()
