"""Request body guard of the ticket submission route (Requirements 22.6 and 25.6; task 18.1).

Two levels, on purpose:

* **The middleware alone**, called as an ASGI application with a scripted receive channel. This is the
  only way to control how the body arrives (declared length, chunks, no length at all), to see how many
  chunks the application was given before the guard cut the stream, and to check that no second
  `http.response.start` is sent.
* **The real application** from `create_app()` with the flag on and off: the position of the middleware
  in the stack, the 413 envelope, the security and CORS headers on it, the answer without a token, and
  the paths that must not be touched.

The limit is 12,582,912 bytes (12 x 1,048,576): exactly that many pass, one more fails.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware
from starlette.types import Message, Receive, Scope, Send

from app.core.security_headers import SecurityHeadersMiddleware
from app.db.session import get_db
from app.main import create_app
from app.service_tickets.api.body_limit import MAX_BODY_BYTES, BodyLimitMiddleware
from app.service_tickets.api.deps import CustomerPrincipal, customer_principal
from app.service_tickets.api.preflight_guard import PreflightGuard
from app.service_tickets.error_codes import CODE_REQUEST_TOO_LARGE

LIMIT = 12_582_912
MIB = 1_048_576
PREFIX = "/api"
PATH = f"{PREFIX}/service/tickets"
#: Staff opening a ticket for a customer carries the same photo, so it is guarded the same way (R26.8).
STAFF_PATH = f"{PREFIX}/service/staff/tickets"
ORIGIN = "http://localhost:5173"
CHUNK = 262_144

TOO_LARGE_BODY = {"detail": {"error": {"code": "request_too_large"}}}

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "geolocation=(), camera=(), microphone=()",
}


# --------------------------------------------------------------------------- scripted ASGI calls


@dataclass
class Outcome:
    """What an ASGI call produced and how much of the body it was given."""

    messages: list[Message] = field(default_factory=list)
    delivered_bytes: int = 0
    receive_calls: int = 0

    @property
    def starts(self) -> list[Message]:
        return [m for m in self.messages if m["type"] == "http.response.start"]

    @property
    def status(self) -> int:
        return self.starts[0]["status"]

    @property
    def headers(self) -> dict[str, str]:
        return {k.decode().lower(): v.decode() for k, v in self.starts[0]["headers"]}

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")

    def json(self) -> object:
        return json.loads(self.body)


def _scope(method: str, path: str, headers: list[tuple[bytes, bytes]]) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


def _call(
    app,
    method: str,
    path: str,
    *,
    total: int,
    declared: bytes | None = None,
    chunk: int = CHUNK,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
    scope_headers: list[tuple[bytes, bytes]] | None = None,
    payload: bytes | None = None,
) -> Outcome:
    """Run `app` once with a body of `total` bytes delivered in `chunk`-sized messages.

    With `declared` set, a `Content-Length` header carries that raw value. Without it the request has
    no length at all and is marked chunked, as an HTTP/1.1 server hands over a chunked upload.
    `scope_headers`, when given, replaces the generated header list altogether. `payload`, when given,
    is the body to deliver (and `total` is ignored); otherwise the body is `total` filler bytes.
    """
    outcome = Outcome()
    if payload is not None:
        total = len(payload)
    remaining = total
    position = 0
    if scope_headers is not None:
        headers = list(scope_headers)
    else:
        headers = [(b"content-type", b"application/octet-stream"), *(extra_headers or [])]
        if declared is not None:
            headers.append((b"content-length", declared))
        else:
            headers.append((b"transfer-encoding", b"chunked"))

    async def receive() -> Message:
        nonlocal remaining, position
        outcome.receive_calls += 1
        if remaining < 0:
            return {"type": "http.disconnect"}
        size = min(chunk, remaining)
        data = payload[position : position + size] if payload is not None else b"x" * size
        position += size
        remaining -= size
        outcome.delivered_bytes += size
        more = remaining > 0
        if not more:
            remaining = -1
        return {"type": "http.request", "body": data, "more_body": more}

    async def send(message: Message) -> None:
        outcome.messages.append(message)

    asyncio.run(app(_scope(method, path, headers), receive, send))
    return outcome


async def _reader(scope: Scope, receive: Receive, send: Send) -> None:
    """A downstream application that reads the whole body and reports how many bytes it got."""
    total = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        total += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    await JSONResponse({"bytes": total})(scope, receive, send)


class _Counting:
    """A downstream application that counts how often it was called."""

    def __init__(self, app) -> None:
        self.app = app
        self.calls = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        await self.app(scope, receive, send)


def _guard(app=_reader, **kwargs) -> BodyLimitMiddleware:
    return BodyLimitMiddleware(app, api_prefix=PREFIX, **kwargs)


# --------------------------------------------------------------------------- the middleware alone


def test_the_limit_is_twelve_mebibytes():
    assert MAX_BODY_BYTES == LIMIT == 12 * MIB


def test_exactly_the_limit_passes_with_content_length():
    counted = _Counting(_reader)
    outcome = _call(_guard(counted), "POST", PATH, total=LIMIT, declared=str(LIMIT).encode())

    assert outcome.status == 200
    assert outcome.json() == {"bytes": LIMIT}
    assert counted.calls == 1


def test_one_byte_over_fails_with_content_length_before_the_body_is_read():
    counted = _Counting(_reader)
    outcome = _call(_guard(counted), "POST", PATH, total=LIMIT + 1, declared=str(LIMIT + 1).encode())

    assert outcome.status == 413
    assert outcome.json() == TOO_LARGE_BODY
    assert counted.calls == 0  # the application was never called
    assert outcome.receive_calls == 0  # and not one byte of the body was read
    assert outcome.delivered_bytes == 0


def test_exactly_the_limit_passes_chunked():
    counted = _Counting(_reader)
    outcome = _call(_guard(counted), "POST", PATH, total=LIMIT)

    assert outcome.status == 200
    assert outcome.json() == {"bytes": LIMIT}
    assert counted.calls == 1


def test_one_byte_over_fails_chunked_and_the_stream_is_cut():
    outcome = _call(_guard(), "POST", PATH, total=LIMIT + 1, chunk=CHUNK)

    assert outcome.status == 413
    assert outcome.json() == TOO_LARGE_BODY
    # The application stopped at the chunk that crossed the limit; the rest of the body was not read.
    assert outcome.delivered_bytes <= LIMIT + CHUNK
    assert len(outcome.starts) == 1


def test_a_single_huge_chunk_over_the_limit_fails():
    outcome = _call(_guard(), "POST", PATH, total=LIMIT + 1, chunk=LIMIT + 1)

    assert outcome.status == 413
    assert outcome.json() == TOO_LARGE_BODY


def test_the_stream_is_cut_even_when_the_declared_length_is_a_lie():
    """A client may declare a small length and send more. The streamed count still applies."""
    outcome = _call(_guard(), "POST", PATH, total=LIMIT + 1, declared=b"100")

    assert outcome.status == 413
    assert outcome.json() == TOO_LARGE_BODY


INVALID_LENGTHS = [b"abc", b"", b" ", b"-5", b"1e9", b"12.5", b"0x10", b"1 2", "١٢".encode()]


@pytest.mark.parametrize("declared", INVALID_LENGTHS)
def test_an_invalid_content_length_is_ignored_safely_and_the_stream_is_counted(declared):
    small = _call(_guard(), "POST", PATH, total=1000, declared=declared)
    assert small.status == 200
    assert small.json() == {"bytes": 1000}

    large = _call(_guard(), "POST", PATH, total=LIMIT + 1, declared=declared)
    assert large.status == 413
    assert large.json() == TOO_LARGE_BODY


@pytest.mark.parametrize(
    ("declared", "status"),
    [
        (b"0012582912", 200),  # leading zeros, exactly the limit
        (b"12582912 ", 200),  # surrounding whitespace is part of the header grammar
        (b"0012582913", 413),
        (b"9" * 25, 413),  # too long to be any real size
        (b"9" * 5000, 413),  # longer than int() accepts without a digit-limit error
    ],
    ids=["leading-zeros-at-limit", "padded-at-limit", "leading-zeros-over", "25-digits", "5000-digits"],
)
def test_content_length_edge_values(declared, status):
    # The body sent is small, so a "pass" is decided by the header alone.
    outcome = _call(_guard(), "POST", PATH, total=10, declared=declared)

    assert outcome.status == status
    if status == 413:
        assert outcome.json() == TOO_LARGE_BODY


def test_repeated_content_length_headers_use_the_largest_valid_value():
    headers = [(b"content-length", b"10"), (b"content-length", str(LIMIT + 1).encode())]
    outcome = _call(_guard(), "POST", PATH, total=10, scope_headers=headers)

    assert outcome.status == 413
    assert outcome.json() == TOO_LARGE_BODY


def test_the_answer_needs_no_token_and_ignores_any_authorization_header():
    for extra in ([], [(b"authorization", b"Bearer not.a.token")], [(b"authorization", b"Basic eDp5")]):
        outcome = _call(
            _guard(), "POST", PATH, total=LIMIT + 1, declared=str(LIMIT + 1).encode(), extra_headers=extra
        )
        assert outcome.status == 413
        assert outcome.json() == TOO_LARGE_BODY


def test_the_413_is_json_and_marks_the_response_uncacheable():
    outcome = _call(_guard(), "POST", PATH, total=1, declared=str(LIMIT + 1).encode())

    assert outcome.headers["content-type"] == "application/json"
    assert outcome.headers["cache-control"] == "no-store"
    assert CODE_REQUEST_TOO_LARGE == "request_too_large"


def test_no_second_response_is_sent_when_the_application_already_started_one():
    async def eager(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.body", "body": b"done"})

    outcome = _call(_guard(eager), "POST", PATH, total=LIMIT + 1)

    assert len(outcome.starts) == 1
    assert outcome.status == 200  # the application's own start; no 413 on top of it
    assert b"request_too_large" not in outcome.body
    assert outcome.delivered_bytes <= LIMIT + CHUNK  # and the stream was still cut


def test_the_guard_stops_reading_once_the_limit_is_passed():
    reads: list[int] = []

    async def greedy(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()  # raises once the limit is passed
            reads.append(len(message.get("body", b"")))
            if not message.get("more_body", False):
                break
        await JSONResponse({})(scope, receive, send)

    outcome = _call(_guard(greedy), "POST", PATH, total=10 * LIMIT)

    assert outcome.status == 413
    assert sum(reads) <= LIMIT
    assert outcome.delivered_bytes <= LIMIT + CHUNK  # 10 x the limit was on offer


def test_the_staff_submission_path_has_the_same_limit_with_and_without_content_length():
    """R26.8: exactly the limit passes, one byte over is the coded 413, declared or streamed."""
    for declared_for in (lambda total: str(total).encode(), lambda _total: None):
        counted = _Counting(_reader)
        passed = _call(_guard(counted), "POST", STAFF_PATH, total=LIMIT, declared=declared_for(LIMIT))
        assert passed.status == 200
        assert counted.calls == 1

        refused = _Counting(_reader)
        over = _call(_guard(refused), "POST", STAFF_PATH, total=LIMIT + 1, declared=declared_for(LIMIT + 1))
        assert over.status == 413
        assert over.json() == TOO_LARGE_BODY


def test_a_custom_prefix_moves_the_guarded_path():
    guard = BodyLimitMiddleware(_reader, api_prefix="/v2/")

    declared = str(LIMIT + 1).encode()

    assert _call(guard, "POST", "/v2/service/tickets", total=LIMIT + 1, declared=declared).status == 413
    assert _call(guard, "POST", PATH, total=LIMIT + 1, declared=declared).status == 200


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
def test_non_http_scopes_pass_through(scope_type):
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    asyncio.run(_guard(inner)({"type": scope_type, "path": PATH}, None, None))

    assert seen == [scope_type]


# --------------------------------------------------------------------------- only that one route


OTHER_ROUTES = [
    ("POST", f"{PREFIX}/service/auth/sign-in"),
    ("POST", f"{PREFIX}/service/tickets/10000/cancel"),  # a subpath of the guarded path
    ("POST", f"{PATH}/"),  # exact match only: a trailing slash is another path
    ("POST", f"{PATH}/x"),
    ("POST", f"{PREFIX}/service/tickets-extra"),
    ("POST", f"{PREFIX}/service"),
    ("POST", f"{PREFIX}/auth/login"),
    ("POST", f"{PREFIX}/employees"),
    ("POST", "/api/services/tickets"),
    ("POST", "/other/api/service/tickets"),
    ("POST", f"{STAFF_PATH}/"),  # the staff path is an exact match too
    ("POST", f"{STAFF_PATH}/10000/status"),
    ("POST", f"{PREFIX}/service/staff/customers"),
    ("GET", STAFF_PATH),
    ("PUT", STAFF_PATH),
    ("GET", PATH),  # the same path, another method
    ("PUT", PATH),
    ("PATCH", PATH),
    ("DELETE", PATH),
    ("OPTIONS", PATH),
]


@pytest.mark.parametrize(("method", "path"), OTHER_ROUTES)
def test_other_paths_and_methods_are_not_limited(method, path):
    """The same body size that fails on the guarded route is passed through untouched elsewhere."""
    total = LIMIT + 3 * MIB
    for declared in (str(total).encode(), None):
        counted = _Counting(_reader)
        outcome = _call(_guard(counted), method, path, total=total, declared=declared)

        assert outcome.status == 200, (method, path, declared)
        assert outcome.json() == {"bytes": total}
        assert counted.calls == 1


# --------------------------------------------------------------------------- the real application


def _app(settings, *, enabled: bool):
    changes = {"service_tickets_enabled": "true" if enabled else "", "api_prefix": PREFIX}
    app = create_app(settings.model_copy(update=changes))
    # No database may be needed to prove the guard: any use of the session is a loud failure.
    app.dependency_overrides[get_db] = lambda: None
    return app


@pytest.fixture
def on_app(settings):
    return _app(settings, enabled=True)


@pytest.fixture
def on_client(on_app) -> TestClient:
    # No redirect following: `POST .../tickets/` is answered with a redirect to the guarded path, and
    # replaying the body there would test the guarded path, not the one named.
    return TestClient(on_app, raise_server_exceptions=False, follow_redirects=False)


def _assert_security_headers(headers) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert headers.get(name) == value, name


def test_middleware_order_puts_the_guard_inside_the_header_middlewares(settings):
    on = [m.cls for m in _app(settings, enabled=True).user_middleware]
    off = [m.cls for m in _app(settings, enabled=False).user_middleware]

    # `user_middleware` lists the outermost first: a response leaves through the guard first and is
    # then stamped by the security-header middleware and CORS on the way out.
    assert on == [CORSMiddleware, SecurityHeadersMiddleware, BodyLimitMiddleware]
    assert off == [PreflightGuard, CORSMiddleware, SecurityHeadersMiddleware]
    assert BodyLimitMiddleware not in off


@pytest.mark.parametrize("size", [LIMIT + 1, LIMIT + MIB // 2, 13 * MIB])
def test_a_body_between_12_and_13_mebibytes_gets_the_coded_413(on_client, size):
    response = on_client.post(PATH, content=b"x" * size, headers={"Content-Type": "application/octet-stream"})

    assert response.status_code == 413
    assert response.json() == TOO_LARGE_BODY


@pytest.mark.parametrize("size", [LIMIT + 1, 13 * MIB])
def test_a_staff_submission_body_over_12_mebibytes_gets_the_coded_413_without_a_token(on_client, size):
    response = on_client.post(
        STAFF_PATH, content=b"x" * size, headers={"Content-Type": "application/octet-stream"}
    )

    assert response.status_code == 413
    assert response.json() == TOO_LARGE_BODY


def test_a_staff_submission_of_exactly_the_limit_reaches_authentication(on_client):
    headers = {"Content-Type": "application/octet-stream"}
    response = on_client.post(STAFF_PATH, content=b"x" * LIMIT, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"]["error"]["code"] == "not_authenticated"


def test_the_413_carries_the_security_headers_and_the_cors_headers(on_client):
    response = on_client.post(
        PATH,
        content=b"x" * (LIMIT + 1),
        headers={"Content-Type": "application/octet-stream", "Origin": ORIGIN},
    )

    assert response.status_code == 413
    _assert_security_headers(response.headers)
    assert response.headers["access-control-allow-origin"] == ORIGIN  # the browser can read the code
    assert response.headers["cache-control"] == "no-store"


def test_the_413_is_given_without_a_token_and_with_a_bad_one(on_client):
    body = b"x" * (LIMIT + 1)
    for headers in ({}, {"Authorization": "Bearer not.a.token"}, {"Authorization": "Basic eDp5"}):
        response = on_client.post(PATH, content=body, headers={"Content-Type": "text/plain", **headers})

        assert response.status_code == 413, headers
        assert response.json() == TOO_LARGE_BODY


def test_exactly_the_limit_is_not_refused_by_the_guard(on_client):
    # Without a token the route answers 401, which proves the request got past the guard.
    headers = {"Content-Type": "application/octet-stream"}
    response = on_client.post(PATH, content=b"x" * LIMIT, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"]["error"]["code"] == "not_authenticated"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", f"{PREFIX}/service/auth/sign-in"),
        ("POST", f"{PREFIX}/service/tickets/10000/cancel"),
        ("POST", f"{PATH}/"),
        ("POST", f"{STAFF_PATH}/"),
        ("POST", f"{STAFF_PATH}/10000/status"),
        ("POST", f"{PREFIX}/auth/login"),
        ("GET", PATH),
        ("GET", STAFF_PATH),
        ("PUT", PATH),
        ("DELETE", PATH),
    ],
)
def test_other_routes_of_the_real_app_never_answer_request_too_large(on_client, method, path):
    size = LIMIT + 2 * MIB
    response = on_client.request(
        method, path, content=b"x" * size, headers={"Content-Type": "application/octet-stream"}
    )

    assert response.status_code != 413, (method, path)
    assert b"request_too_large" not in response.content


def _principal() -> CustomerPrincipal:
    return CustomerPrincipal(customer_id=uuid.uuid4(), phone="0505556677", must_change_password=False)


def _multipart(total: int) -> bytes:
    """A well-formed multipart body of exactly `total` bytes: one file part padded with filler.

    A file part, because the multipart parser caps a plain text field at 1 MB and would answer its own
    422 before the guard's limit is reached.
    """
    head = (
        b'--testboundary\r\nContent-Disposition: form-data; name="photo"; filename="a.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\n"
    )
    tail = b"\r\n--testboundary--\r\n"
    return head + b"x" * (total - len(head) - len(tail)) + tail


def _stream_to_real_handler(app, total: int) -> Outcome:
    """Send a chunked multipart body (no `Content-Length`) straight into the real application."""
    return _call(
        app,
        "POST",
        PATH,
        total=0,
        payload=_multipart(total),
        scope_headers=[
            (b"content-type", b"multipart/form-data; boundary=testboundary"),
            (b"transfer-encoding", b"chunked"),
            (b"origin", ORIGIN.encode()),
        ],
    )


def test_a_streamed_oversize_body_read_by_the_real_handler_gets_the_coded_413(on_app):
    """No Content-Length: the handler's form parsing pulls the chunks and the guard cuts the stream."""
    on_app.dependency_overrides[customer_principal] = _principal

    outcome = _stream_to_real_handler(on_app, LIMIT + 1)

    assert outcome.status == 413
    assert outcome.json() == TOO_LARGE_BODY
    assert len(outcome.starts) == 1
    assert outcome.delivered_bytes <= LIMIT + CHUNK
    _assert_security_headers(outcome.headers)
    assert outcome.headers["access-control-allow-origin"] == ORIGIN


def test_a_streamed_body_within_the_limit_reaches_the_real_handler(on_app, monkeypatch):
    from app.service_tickets.api import customer_router

    # The storage step needs a database; this test is about the guard, so the step is replaced.
    monkeypatch.setattr(customer_router, "_store_ticket", lambda *args: 10000)
    on_app.dependency_overrides[customer_principal] = _principal

    outcome = _stream_to_real_handler(on_app, 2 * MIB)

    assert outcome.status == 201
    assert outcome.json() == {"ticket_number": 10000}


# --------------------------------------------------------------------------- flag off


def test_flag_off_the_guard_has_no_effect(settings):
    off = _app(settings, enabled=False)
    client = TestClient(off, raise_server_exceptions=False)
    reference = client.post(f"{PREFIX}/no-such-path", content=b"x" * (LIMIT + 1))

    headers = {"Content-Type": "application/octet-stream"}
    response = client.post(PATH, content=b"x" * (LIMIT + 1), headers=headers)

    assert BodyLimitMiddleware not in [m.cls for m in off.user_middleware]
    assert response.status_code == 404
    assert response.content == b'{"detail":"Not Found"}'
    assert response.status_code == reference.status_code
    assert response.content == reference.content
    assert b"request_too_large" not in response.content
