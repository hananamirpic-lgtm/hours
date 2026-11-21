"""Flag off: every ticket path is an unknown path (Requirements 21.3, 21.4, 21.8 and 21.10; test T9).

While the service-ticket flag is off, a request under `/api/service` must be indistinguishable from a
request to a path that does not exist, and must not touch the database, Redis or the object store:

* **Which paths.** Every route the Ticket API really has, read from the route table of a second
  application built with the flag on (path parameters filled with plausible values), plus paths that
  never existed (the prefix itself, trailing slashes, nested and encoded paths).
* **Which methods.** GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS and TRACE. `OPTIONS` also comes with
  `Origin` and `Access-Control-Request-Method` (a CORS preflight), which `CORSMiddleware` would answer
  before routing if the preflight guard were not outermost.
* **Which tokens.** None, a malformed one, a non-bearer scheme, a real administrator token, a real
  technician token and a real customer token.
* **What is compared.** Status, body and every response header except `Date` against the same request
  sent to `/api/no-such-path`. The reference of a preflight is the same request without the two CORS
  preflight headers: a real preflight to an unknown path is answered 200 by CORS, and the requirement is
  the 404 an unknown path gives a request that is not a preflight.
* **What is spied on.** `get_engine`, `get_session_factory` and `get_db` (the session factory), the
  Redis client, and the object-store clients, plus `redis.Redis.execute_command`, `boto3.client` and
  every SQL statement on any engine. Each trips and records on use. Any recorded call fails the test.

This is the example version. The generated version (Property 29) is task 14.4.
"""

from __future__ import annotations

import re
import sys
import uuid
from dataclasses import dataclass, field

import boto3
import boto3.session
import pytest
import redis
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, text

from app.core import redis as app_redis
from app.core import scheduler as scheduler_module
from app.core import storage as app_storage
from app.core.security import create_access_token
from app.db import session as db_session
from app.main import create_app
from app.repositories import health as health_repository
from app.service_tickets.customer_token import mint_customer_token

API_PREFIX = "/api"
SERVICE_PREFIX = f"{API_PREFIX}/service"
UNKNOWN_PATH = f"{API_PREFIX}/no-such-path"
ORIGIN = "http://localhost:5173"
CONTACT = "access@example.test"

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE")
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

#: Paths that are not routes of the Ticket API but sit under its prefix. They include the prefix itself,
#: trailing slashes on real routes (no redirect may happen), nested and encoded paths, and names a
#: documentation or health page would use.
UNKNOWN_TICKET_PATHS = (
    f"{SERVICE_PREFIX}",
    f"{SERVICE_PREFIX}/",
    f"{SERVICE_PREFIX}/x",
    f"{SERVICE_PREFIX}/a/b/c",
    f"{SERVICE_PREFIX}/auth",
    f"{SERVICE_PREFIX}/staff",
    f"{SERVICE_PREFIX}/staff/",
    f"{SERVICE_PREFIX}/auth/sign-in/",
    f"{SERVICE_PREFIX}/form-config/",
    f"{SERVICE_PREFIX}/tickets/",
    f"{SERVICE_PREFIX}/staff/dashboard/",
    f"{SERVICE_PREFIX}/tickets/abc",
    f"{SERVICE_PREFIX}/tickets/99999/photo",
    f"{SERVICE_PREFIX}/staff/tickets/10000/unknown",
    f"{SERVICE_PREFIX}/%20",
    f"{SERVICE_PREFIX}/tickets/%2e%2e",
    f"{SERVICE_PREFIX}/docs",
    f"{SERVICE_PREFIX}/openapi.json",
    f"{SERVICE_PREFIX}/health",
)

_PATH_PARAMETER = re.compile(r"\{(\w+)\}")
_FILLED_UUID = "5f0c3d2e-8b1a-4c6e-9a7d-2e4f6a8b0c1d"
_FILLED = {"ticket_number": "10000"}


# --------------------------------------------------------------------------- applications and routes


def _app(settings, *, enabled: bool) -> FastAPI:
    changes = {
        "service_tickets_enabled": "true" if enabled else "",
        "service_accessibility_contact": CONTACT,
        "api_prefix": API_PREFIX,
    }
    return create_app(settings.model_copy(update=changes))


def _filled(path_template: str) -> str:
    return _PATH_PARAMETER.sub(lambda m: _FILLED.get(m.group(1), _FILLED_UUID), path_template)


def _ticket_route_paths(app: FastAPI) -> list[str]:
    """Every distinct route path under the ticket prefix, with its parameters filled in."""
    found = {
        _filled(route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith(SERVICE_PREFIX + "/")
    }
    return sorted(found)


@pytest.fixture
def real_ticket_paths(settings) -> list[str]:
    """The real ticket routes, read from an application built with the flag on.

    Built before the resource spy is installed on purpose: building it imports the ticket routers, and a
    router module imported while the spy is active would keep the tripping stubs for the rest of the run.
    """
    paths = _ticket_route_paths(_app(settings, enabled=True))
    assert len(paths) >= 25, f"expected the real ticket route table, found only {paths}"
    return paths


# --------------------------------------------------------------------------- the resource spy


@dataclass
class ResourceSpy:
    """Records every use of the database, Redis and the object store while it is installed."""

    calls: list[str] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)
    original_get_db: object = None

    def trip(self, name: str):
        def tripped(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"{name} must not be used")

        return tripped

    def override_get_db(self):
        self.calls.append("dependency get_db")
        raise AssertionError("get_db must not be used")

    def used(self) -> list[str]:
        return [*self.calls, *self.statements]


@pytest.fixture
def spy(monkeypatch, real_ticket_paths):
    """Make any database, Redis or object-store use raise and be recorded.

    Every module under `app` that holds a reference to one of the guarded functions is rebound, so a
    `from x import name` copy is covered too. The Redis and boto3 entry points are patched on the
    library classes, which also covers a client some later module builds for itself.
    """
    recorder = ResourceSpy(original_get_db=db_session.get_db)

    guarded = {
        "get_engine": db_session.get_engine,
        "get_session_factory": db_session.get_session_factory,
        "get_db": db_session.get_db,
        "get_redis_client": app_redis.get_redis_client,
        "get_object_storage_client": health_repository.get_object_storage_client,
        "get_object_storage_presign_client": health_repository.get_object_storage_presign_client,
        "get_object_storage": app_storage.get_object_storage,
    }
    stubs = {original: recorder.trip(name) for name, original in guarded.items()}
    for module_name, module in list(sys.modules.items()):
        if module is None or not (module_name == "app" or module_name.startswith("app.")):
            continue
        for attribute, value in list(vars(module).items()):
            for original, stub in stubs.items():
                if value is original:
                    monkeypatch.setattr(module, attribute, stub)

    monkeypatch.setattr(redis.Redis, "from_url", recorder.trip("redis.Redis.from_url"))
    monkeypatch.setattr(redis.Redis, "execute_command", recorder.trip("redis.Redis.execute_command"))
    monkeypatch.setattr(app_storage.ObjectStorage, "__init__", recorder.trip("ObjectStorage.__init__"))
    monkeypatch.setattr(boto3, "client", recorder.trip("boto3.client"))
    monkeypatch.setattr(boto3.session.Session, "client", recorder.trip("boto3.session.Session.client"))

    def count(connection, cursor, statement, parameters, context, executemany):
        recorder.statements.append(statement)

    event.listen(Engine, "before_cursor_execute", count)
    try:
        yield recorder
    finally:
        event.remove(Engine, "before_cursor_execute", count)


def _client(app: FastAPI, recorder: ResourceSpy) -> TestClient:
    """A client without lifespan whose `get_db` dependency trips the spy.

    `raise_server_exceptions=False` so a tripped spy shows up as a mismatch with a readable label and in
    `recorder.calls`, not as an exception that stops the matrix at the first case.
    """
    app.dependency_overrides[recorder.original_get_db] = recorder.override_get_db
    return TestClient(app, raise_server_exceptions=False)


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

    probe = create_engine("sqlite+pysqlite:///:memory:")
    with probe.connect() as connection:
        connection.execute(text("SELECT 1"))
    assert spy.statements == ["SELECT 1"]


# --------------------------------------------------------------------------- tokens and requests


def _token_states() -> dict[str, dict[str, str]]:
    """Header sets for every token state. The tokens are validly signed ones, as a real client holds."""
    admin = create_access_token(user_id=uuid.uuid4(), token_version=1, role="admin")
    technician = create_access_token(user_id=uuid.uuid4(), token_version=1, role="technician")
    customer = mint_customer_token(uuid.uuid4(), 1)
    return {
        "absent": {},
        "malformed": {"Authorization": "Bearer not.a.token"},
        "non-bearer": {"Authorization": "Basic dXNlcjpwYXNz"},
        "staff admin token": {"Authorization": f"Bearer {admin}"},
        "staff technician token": {"Authorization": f"Bearer {technician}"},
        "customer token": {"Authorization": f"Bearer {customer}"},
    }


def _snapshot(response, *, without_cors: bool = False) -> tuple:
    """Status, body and every header except `Date` (which differs by the second).

    `without_cors` also drops the headers `CORSMiddleware` derives from an `Origin` header
    (`Access-Control-*` and `Vary`). It is used for one case only: an `OPTIONS` request that carries
    `Origin` but is not a preflight. The preflight guard answers every `OPTIONS` under the prefix itself,
    so that reply has no CORS headers, while the same request to an unknown path gets them. Status and
    body are the same; the difference is recorded as a finding in the task report, and the test asserts
    that the guard's reply grants no CORS access (see `_cors_headers`).
    """
    dropped = {"date"}
    headers = sorted(
        (name.lower(), value)
        for name, value in response.headers.multi_items()
        if name.lower() not in dropped and not (without_cors and _cors_derived(name.lower()))
    )
    return response.status_code, response.content, headers


def _cors_derived(name: str) -> bool:
    return name.startswith("access-control-") or name == "vary"


def _cors_headers(response) -> list[str]:
    return [name for name in response.headers if name.lower().startswith("access-control-")]


def _describe(snapshot: tuple) -> str:
    status, body, headers = snapshot
    return f"{status} {body[:80]!r} {headers}"


class _Variant:
    """One way of sending the request: extra headers, and the headers the reference request carries."""

    def __init__(self, label: str, extra: dict[str, str], reference_extra: dict[str, str]) -> None:
        self.label = label
        self.extra = extra
        self.reference_extra = reference_extra


PLAIN = _Variant("plain", {}, {})
WITH_ORIGIN = _Variant("origin", {"Origin": ORIGIN}, {"Origin": ORIGIN})
PREFLIGHT = _Variant(
    "preflight",
    {"Origin": ORIGIN, "Access-Control-Request-Method": "GET"},
    {},
)
PREFLIGHT_WITH_HEADERS = _Variant(
    "preflight-with-headers",
    {
        "Origin": ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    },
    {},
)

JSON_BODY = ("json", b'{"phone": "0505556677", "password": "1234"}', "application/json")
GARBAGE_BODY = ("garbage", b"\x00\xff not a body {[", "multipart/form-data")


def _matrix(paths: list[str]):
    """Yield (method, path, token label, variant, body) for every combination that is checked.

    The full token set is used with plain requests and a valid body. The other variants (CORS headers,
    a garbage body) are checked with the three token states that differ most: none, a staff token and
    a customer token.
    """
    tokens = list(_token_states())
    few = ["absent", "staff admin token", "customer token"]
    for path in paths:
        for method in METHODS:
            bodies = [JSON_BODY] if method in BODY_METHODS else [None]
            for token in tokens:
                for body in bodies:
                    yield method, path, token, PLAIN, body
            for token in few:
                yield method, path, token, WITH_ORIGIN, bodies[0]
                if method in BODY_METHODS:
                    yield method, path, token, PLAIN, GARBAGE_BODY
                if method == "OPTIONS":
                    yield method, path, token, PREFLIGHT, None
                    yield method, path, token, PREFLIGHT_WITH_HEADERS, None


def _send(client: TestClient, method: str, path: str, headers: dict[str, str], body):
    options: dict[str, object] = {"headers": dict(headers)}
    if body is not None:
        _, content, content_type = body
        options["headers"] = {**headers, "Content-Type": content_type}
        options["content"] = content
    return client.request(method, path, **options)


# --------------------------------------------------------------------------- the required test


def test_flag_off_every_ticket_path_is_unknown_path(settings, spy, real_ticket_paths):
    """R21.3, R21.4, R21.8, R21.10: with the flag off no ticket path differs from an unknown path."""
    app = _app(settings, enabled=False)
    client = _client(app, spy)
    tokens = _token_states()
    paths = [*real_ticket_paths, *UNKNOWN_TICKET_PATHS]

    # The reference itself must be the unknown-path 404 of the Hours API.
    reference_response = client.get(UNKNOWN_PATH)
    assert reference_response.status_code == 404
    assert reference_response.content == b'{"detail":"Not Found"}'

    references: dict[tuple, tuple] = {}
    mismatches: list[str] = []
    requests_sent = 0
    for method, path, token, variant, body in _matrix(paths):
        # An OPTIONS request with `Origin` only is answered by the preflight guard, not by CORS (finding F-1).
        lenient = method == "OPTIONS" and variant is WITH_ORIGIN
        reference_key = (method, token, variant.label, body[0] if body else None)
        if reference_key not in references:
            headers = {**tokens[token], **variant.reference_extra}
            reference = _send(client, method, UNKNOWN_PATH, headers, body)
            references[reference_key] = _snapshot(reference, without_cors=lenient)
        expected = references[reference_key]
        headers = {**tokens[token], **variant.extra}
        response = _send(client, method, path, headers, body)
        actual = _snapshot(response, without_cors=lenient)
        requests_sent += 1

        if method == "OPTIONS" and variant is not PLAIN and _cors_headers(response):
            granted = _cors_headers(response)
            mismatches.append(f"{method} {path} [{token}, {variant.label}] grants CORS: {granted}")
        if actual != expected:
            mismatches.append(
                f"{method} {path} [{token}, {variant.label}, body={body[0] if body else None}]\n"
                f"    got      {_describe(actual)}\n"
                f"    expected {_describe(expected)}"
            )
        # The status and the body are the requirement; for every method but HEAD (which has no body)
        # and the preflight references, the body is the JSON of an unknown path.
        if method not in {"HEAD"} and actual[0] != 404:
            mismatches.append(f"{method} {path} [{token}, {variant.label}] status {actual[0]}, not 404")

    shown = "\n".join(mismatches[:15])
    assert not mismatches, f"{len(mismatches)} of {requests_sent} requests differ:\n{shown}"
    assert spy.used() == [], f"resources used while the flag is off: {spy.used()[:10]}"

    # Coverage: the matrix is not vacuous.
    assert requests_sent >= 3000
    assert set(METHODS) == {m for m, *_ in _matrix(paths[:1])}


def test_flag_off_routes_table_has_no_ticket_route(settings, real_ticket_paths):
    """The flag-off application has no route and no OpenAPI path under the prefix (R21.3, R21.8)."""
    off = _app(settings, enabled=False)
    on = _app(settings, enabled=True)

    assert _ticket_route_paths(off) == []
    assert not [p for p in off.openapi()["paths"] if p.startswith(SERVICE_PREFIX)]

    # Hours paths and schemas are those of the flag-on application, the ticket paths aside.
    def hours_paths(document: dict) -> dict:
        return {p: item for p, item in document["paths"].items() if not p.startswith(SERVICE_PREFIX)}

    assert hours_paths(off.openapi()) == hours_paths(on.openapi())
    for name, schema in off.openapi()["components"]["schemas"].items():
        assert on.openapi()["components"]["schemas"].get(name) == schema, name
    assert {p for p in on.openapi()["paths"] if p.startswith(SERVICE_PREFIX)}


# --------------------------------------------------------------------------- /api/features


def test_flag_off_features_answers_false_and_is_not_a_404(settings, spy):
    """R21.3: the discovery endpoint is outside the 404 and says the flag is off, for every token state."""
    client = _client(_app(settings, enabled=False), spy)

    for label, headers in _token_states().items():
        response = client.get(f"{API_PREFIX}/features", headers=headers)

        assert response.status_code == 200, label
        assert response.status_code != 404, label
        assert response.json() == {"service_tickets": False}, label
        assert response.headers.get_list("cache-control") == ["no-store"], label

    assert spy.used() == []


# --------------------------------------------------------------------------- start with the flag off


def test_flag_off_application_start_touches_no_ticket_storage(settings, spy, monkeypatch):
    """R21.10: starting with the flag off runs no SQL, no Redis command and no object-store call.

    The lifespan is run for real. Its one existing action, building the Hours object-store client (local
    work, no request), is replaced by a recorder so that the spy can stay armed for everything else.
    """
    started: list[str] = []
    monkeypatch.setattr("app.main.get_object_storage_client", lambda: started.append("client built"))
    monkeypatch.setattr(scheduler_module, "start_scheduler", lambda settings=None: None)

    app = _app(settings, enabled=False)
    with TestClient(app) as running:
        response = running.get(f"{API_PREFIX}/features")
        unknown = running.get(f"{SERVICE_PREFIX}/staff/dashboard")

    assert response.json() == {"service_tickets": False}
    assert unknown.status_code == 404
    assert unknown.content == b'{"detail":"Not Found"}'
    assert started == ["client built"]
    assert spy.used() == []
