"""The anonymous branding reads: `GET /public/branding` and `.../logo` (task 30.16; R28.8, R22.11).

These are the first routes of the ticket API, other than customer sign-in, that need no token. The tests
therefore look at what they expose and what they touch, not only at the happy path:

* the JSON is exactly `{company_name, has_logo}`, the logo is the stored PNG, and nothing else is read: the
  SQL of a request names `svc_branding` and no other table, and the object store is asked for the logo key
  and nothing else;
* a branding row that names anything but a logo object (a ticket photo, a path outside the prefix) is never
  read for an anonymous caller;
* the headers (`Cache-Control`, `nosniff` once, `Content-Type`);
* flag off: both paths are the framework's unknown-path 404, byte for byte;
* the per-address limit: its own bucket, shared by the two routes, not counted when rejected, `Retry-After`
  from the window, keyed by the viewer address behind a trusted proxy, skipped without a usable address,
  allowed when Redis fails, and independent of the sign-in window.

The application is the real one (`create_app` with the flag on), so the real middleware stack is in the path.
The limiter runs its real Lua-script calls against a small in-memory stand-in for Redis (`FakeRedis`, which
implements the one `take` script), and the clock is injected through `get_clock`. One test at the end runs
against a real Redis when `TEST_REDIS_URL` is set.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker
from test_service_api_photo import FakePhotoStore, jpeg_bytes, png_bytes

from app.db.session import get_db
from app.main import create_app
from app.service_tickets import rate_limit
from app.service_tickets.api import customer_router, limits, public_router
from app.service_tickets.config import ServiceConfig
from app.service_tickets.models import (
    FIELD_LABEL_KEYS,
    Branding,
    Category,
    Customer,
    FieldLabel,
    ServiceSettings,
)
from app.service_tickets.photo_store import (
    KEY_PREFIX,
    get_photo_store,
    is_logo_key,
    new_key,
    new_logo_key,
)

API = "/api/service"
BRANDING = f"{API}/public/branding"
LOGO = f"{API}/public/branding/logo"
BROWSER = "203.0.113.9"  # an address outside every trusted range: the key is the peer itself
OTHER_BROWSER = "203.0.113.10"
PROXY = "10.0.0.5"  # inside the default trusted proxy ranges
T0 = 1_700_000_000_000  # the injected clock starts here, in milliseconds
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
LIMIT = 3  # the per-address limit used by the limiter tests (the default is 120 per 60 seconds)


# --------------------------------------------------------------------------- harness


class FakeRedis:
    """The one Redis call the limiter makes, `EVALSHA`/`EVAL` of the `take` script, done in Python.

    It follows the Lua of `rate_limit._TAKE_LUA` line by line, so the window semantics under test are the
    real ones: a fixed window that opens at the first counted request and never counts a rejected one.
    """

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, int]] = {}

    def _take(self, keys: tuple[str, ...], args: tuple[int, ...]) -> list[int]:
        (key,) = keys
        maximum, window, now = args
        state = self.hashes.setdefault(key, {})
        start, count = state.get("s", 0), state.get("c", 0)
        if start == 0 or now - start >= window:
            start, count = now, 0
        left = start + window - now
        if count >= maximum:
            return [0, left]
        self.hashes[key] = {"s": start, "c": count + 1}
        return [1, left]

    def evalsha(self, sha: str, numkeys: int, *args: Any) -> list[int]:
        assert sha == rate_limit._TAKE.sha, "only the take script is expected"
        return self._take(tuple(args[:numkeys]), tuple(int(a) for a in args[numkeys:]))

    def eval(self, source: str, numkeys: int, *args: Any) -> list[int]:  # pragma: no cover - EVALSHA works
        return self.evalsha(rate_limit._TAKE.sha, numkeys, *args)

    def count(self, key: str) -> int:
        return self.hashes.get(key, {}).get("c", 0)


class BrokenRedis:
    """A Redis that is down: every call raises, as a refused connection does."""

    def evalsha(self, *_args: Any, **_kwargs: Any) -> Any:
        raise redis.ConnectionError("down")

    eval = evalsha


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> int:
        return self.now


class _FromPeer:
    """ASGI wrapper that sets the TCP peer of every request, as uvicorn does (`request.client.host`)."""

    def __init__(self, app: FastAPI, peer: str) -> None:
        self.app = app
        self.peer = peer

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "client": (self.peer, 50000)}
        await self.app(scope, receive, send)


def _client(app: FastAPI, peer: str) -> TestClient:
    return TestClient(_FromPeer(app, peer), raise_server_exceptions=False)


def _default_config() -> ServiceConfig:
    names = (
        "service_tickets_enabled service_customer_token_ttl_seconds service_signin_rate_max "
        "service_signin_rate_window_seconds service_submit_rate_max service_submit_rate_window_seconds "
        "service_request_rate_max service_request_rate_window_seconds service_signin_lock_attempts "
        "service_signin_lock_seconds service_client_ip_header service_trusted_proxy_cidrs "
        "service_accessibility_contact"
    ).split()
    return ServiceConfig.from_settings(SimpleNamespace(**dict.fromkeys(names, "")))  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _usable_loggers(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Alembic run earlier in the process (`fileConfig`) can leave a logger disabled."""
    for owner in (public_router, rate_limit):
        monkeypatch.setattr(owner.logger, "disabled", False)
        monkeypatch.setattr(owner.logger, "propagate", True)


@pytest.fixture
def photos() -> FakePhotoStore:
    return FakePhotoStore()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(rate_limit, "get_redis_client", lambda: fake)
    return fake


@pytest.fixture
def config() -> ServiceConfig:
    """The deployment defaults with a small per-address limit, so a test can reach it quickly."""
    return dataclasses.replace(_default_config(), request_rate_max=LIMIT, request_rate_window_seconds=60)


@pytest.fixture
def app(
    settings: Any, session: Session, photos: FakePhotoStore, clock: Clock, config: ServiceConfig
) -> FastAPI:
    changes = {"service_tickets_enabled": "true", "service_accessibility_contact": "a@example.test"}
    application = create_app(settings.model_copy(update={"api_prefix": "/api", **changes}))
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True)

    def request_session() -> Iterator[Session]:
        with factory() as per_request:
            yield per_request

    application.dependency_overrides[get_db] = request_session
    application.dependency_overrides[get_photo_store] = lambda: photos
    application.dependency_overrides[customer_router.get_service_config] = lambda: config
    application.dependency_overrides[limits.get_clock] = lambda: clock
    return application


@pytest.fixture
def http(app: FastAPI, fake_redis: FakeRedis) -> TestClient:
    """A browser that reaches the application directly from `BROWSER`, with the in-memory limiter."""
    return _client(app, BROWSER)


def set_branding(session: Session, *, name: str | None, logo_key: str | None) -> None:
    session.rollback()
    row = session.get(Branding, 1, populate_existing=True)
    if row is None:
        row = Branding(id=1)
        session.add(row)
    row.company_name, row.logo_key = name, logo_key
    session.commit()


def seed_logo(
    session: Session, photos: FakePhotoStore, *, name: str | None = "Acme Service"
) -> tuple[str, bytes]:
    key = new_logo_key()
    data = png_bytes((40, 20))
    photos.seed(key, data)
    set_branding(session, name=name, logo_key=key)
    return key, data


def code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


# --------------------------------------------------------------------------- GET /public/branding


def test_anonymous_gets_the_name_and_whether_a_logo_exists(
    http: TestClient, session: Session, photos
) -> None:
    key, _ = seed_logo(session, photos)

    response = http.get(BRANDING)

    assert response.status_code == 200
    assert response.json() == {"company_name": "Acme Service", "has_logo": True}
    assert key not in response.text and "service-tickets" not in response.text
    assert photos.ops == []  # the JSON read touches no object


def test_the_empty_branding_reads_as_no_name_and_no_logo(http: TestClient) -> None:
    response = http.get(BRANDING)  # no `svc_branding` row at all

    assert response.status_code == 200
    assert response.json() == {"company_name": None, "has_logo": False}


def test_the_json_is_not_stored_and_carries_the_security_headers_once(
    http: TestClient, session: Session, photos
) -> None:
    seed_logo(session, photos)

    response = http.get(BRANDING)

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]  # one value, not two
    assert "www-authenticate" not in response.headers


def test_nothing_but_the_name_and_the_flag_is_returned_whatever_else_is_stored(
    http: TestClient, session: Session, photos
) -> None:
    session.add(
        Customer(
            phone="0505556677",
            full_name="Avi Cohen",
            full_name_key="avi cohen",
            city="Haifa",
            apartment="12",
            password_hash="x",
            token_version=1,
        )
    )
    session.add(
        Category(seq=1, name_he="צנרת", name_en="Plumbing", name_he_key="צנרת", name_en_key="plumbing")
    )
    session.add(ServiceSettings(id=1, description_required=True, photo_upload_enabled=True))
    session.add_all(FieldLabel(field_key=k, label_he=f"{k}-he", label_en=f"{k}-en") for k in FIELD_LABEL_KEYS)
    session.commit()
    seed_logo(session, photos)

    response = http.get(BRANDING)

    assert set(response.json()) == {"company_name", "has_logo"}
    for secret in ("Avi Cohen", "0505556677", "Haifa", "Plumbing", "צנרת", "city-he", "photo_upload"):
        assert secret not in response.text


# --------------------------------------------------------------------------- GET /public/branding/logo


def test_anonymous_gets_the_stored_logo_with_the_public_cache_headers(
    http: TestClient, session: Session, photos
) -> None:
    key, data = seed_logo(session, photos)

    response = http.get(LOGO)

    assert response.status_code == 200
    assert response.content == data and response.content.startswith(PNG_SIGNATURE)
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]
    assert photos.ops == [("get", key)]  # the one object the row names, read once, nothing written


def test_no_logo_is_a_plain_404_with_an_empty_body_and_no_code(
    http: TestClient, session: Session, photos
) -> None:
    set_branding(session, name="Acme", logo_key=None)

    response = http.get(LOGO)

    assert response.status_code == 404
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"  # a miss is never kept
    assert photos.ops == []


def test_with_no_branding_row_the_logo_is_the_same_404(http: TestClient, photos) -> None:
    response = http.get(LOGO)

    assert (response.status_code, response.content) == (404, b"")
    assert photos.ops == []


def test_a_missing_object_is_a_404_and_the_key_is_not_logged(
    http: TestClient, session: Session, photos, caplog: pytest.LogCaptureFixture
) -> None:
    key, _ = seed_logo(session, photos)
    photos.objects.clear()
    caplog.set_level(logging.ERROR, logger=public_router.logger.name)

    response = http.get(LOGO)

    assert (response.status_code, response.content) == (404, b"")
    assert response.headers["cache-control"] == "no-store"
    assert caplog.records, "the failure is logged"
    assert key not in caplog.text and KEY_PREFIX not in caplog.text


def test_an_unreachable_object_store_is_503_photo_storage_unavailable(
    http: TestClient, session: Session, photos
) -> None:
    seed_logo(session, photos)
    photos.fail_get_once = True

    response = http.get(LOGO)

    assert response.status_code == 503
    assert code(response) == "photo_storage_unavailable"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "stored_key",
    [
        new_key(),  # a ticket photo key: valid for the store, not a logo
        f"{KEY_PREFIX}branding/{'A' * 32}.png",  # wrong characters
        f"{KEY_PREFIX}branding/{'a' * 31}.png",  # wrong length
        f"{KEY_PREFIX}branding/{'a' * 32}.jpg",  # wrong extension
        f"{KEY_PREFIX}branding/../{'a' * 32}.png",  # path escape
        "etc/passwd",
        "",
    ],
)
def test_a_row_that_names_anything_but_a_logo_object_is_never_read(
    http: TestClient, session: Session, photos, caplog: pytest.LogCaptureFixture, stored_key: str
) -> None:
    """R28.8: an anonymous caller can only ever make the API read an object of the logo shape.

    The ticket photo is in the store, so a route that only checked the key against the store's own rule
    would deliver it. The foreign key is not logged either.
    """
    if stored_key.startswith(KEY_PREFIX) and stored_key.endswith(".jpg") and "branding" not in stored_key:
        photos.seed(stored_key, jpeg_bytes())
    set_branding(session, name="Acme", logo_key=stored_key)
    caplog.set_level(logging.ERROR, logger=public_router.logger.name)

    response = http.get(LOGO)

    assert (response.status_code, response.content) == (404, b"")
    assert photos.ops == []  # nothing was read, nothing was even asked
    assert stored_key not in caplog.text or stored_key == ""


def test_is_logo_key_accepts_only_the_exact_logo_shape() -> None:
    assert is_logo_key(new_logo_key())
    assert not is_logo_key(new_key())
    assert not is_logo_key(new_logo_key() + "\n")
    assert not is_logo_key(None)
    assert not is_logo_key(b"service-tickets/branding/" + b"a" * 32 + b".png")


# --------------------------------------------------------------------------- what the routes touch


class Statements:
    """Every SQL statement a request sends: which tables were read, and that nothing was written."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.sql: list[str] = []

    def _record(self, _conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        self.sql.append(statement)

    def __enter__(self) -> Statements:
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)


@pytest.mark.parametrize("path", [BRANDING, LOGO])
def test_a_request_reads_only_the_branding_row_and_writes_nothing(
    http: TestClient, session: Session, photos, path: str
) -> None:
    """R28.8, R20: no `users`, no customer, no ticket: one SELECT of `svc_branding` and no write."""
    seed_logo(session, photos)

    with Statements(session.get_bind()) as seen:
        response = http.get(path)

    assert response.status_code == 200
    assert seen.sql, "the request read the database"
    for sql in seen.sql:
        assert sql.lstrip().upper().startswith("SELECT"), sql
        assert set(re.findall(r"(?:FROM|JOIN)\s+(\w+)", sql, flags=re.IGNORECASE)) == {"svc_branding"}, sql
    assert all(operation == "get" for operation, _ in photos.ops)


def test_a_token_in_the_header_is_ignored_and_not_counted_as_a_customer_request(
    http: TestClient, session: Session, photos, fake_redis: FakeRedis
) -> None:
    from app.service_tickets.customer_token import mint_customer_token

    seed_logo(session, photos)
    headers = {"Authorization": f"Bearer {mint_customer_token(uuid.uuid4(), 1)}"}

    response = http.get(BRANDING, headers=headers)

    assert response.status_code == 200
    assert not any(key.startswith(rate_limit.REQUEST_PREFIX) for key in fake_redis.hashes)


# --------------------------------------------------------------------------- the flag


def test_with_the_flag_off_both_paths_are_the_same_404_as_any_unknown_path(settings: Any) -> None:
    changes = {"service_tickets_enabled": "", "api_prefix": "/api"}
    off = TestClient(create_app(settings.model_copy(update=changes)), raise_server_exceptions=False)

    unknown = off.get(f"{API}/auth/me")
    for path in (BRANDING, LOGO):
        response = off.get(path)

        assert response.status_code == 404
        assert response.content == unknown.content == b'{"detail":"Not Found"}'
        assert {k: v for k, v in response.headers.items() if k != "date"} == {
            k: v for k, v in unknown.headers.items() if k != "date"
        }


# --------------------------------------------------------------------------- the per-address limit


def _statuses(client: TestClient, paths: list[str], **headers: str) -> list[int]:
    return [client.get(path, headers=headers).status_code for path in paths]


def test_the_two_routes_share_one_per_address_window_and_the_next_request_is_429(
    http: TestClient, session: Session, photos, fake_redis: FakeRedis
) -> None:
    seed_logo(session, photos)

    first = _statuses(http, [BRANDING, LOGO, BRANDING])
    over = http.get(LOGO)

    assert first == [200, 200, 200]
    assert over.status_code == 429
    assert code(over) == "rate_limited"
    assert over.headers["retry-after"] == "60"
    assert over.headers["cache-control"] == "no-store"
    assert over.content != b"" and b"company_name" not in over.content
    assert set(fake_redis.hashes) == {rate_limit.public_key(BROWSER)}  # an address key, nothing else
    assert len(photos.ops) == 1  # the one logo read of the three; the limited request read no object


def test_a_rejected_request_is_not_counted_and_retry_after_is_the_time_left(
    http: TestClient, session: Session, photos, fake_redis: FakeRedis, clock: Clock
) -> None:
    seed_logo(session, photos)
    _statuses(http, [BRANDING] * LIMIT)
    clock.now = T0 + 25_000

    over = [http.get(BRANDING) for _ in range(5)]

    assert {r.status_code for r in over} == {429}
    assert {r.headers["retry-after"] for r in over} == {"35"}
    assert fake_redis.count(rate_limit.public_key(BROWSER)) == LIMIT  # 5 rejections added nothing
    clock.now = T0 + 60_000
    assert http.get(BRANDING).status_code == 200  # the window has ended


def test_another_address_has_its_own_window(app: FastAPI, fake_redis: FakeRedis) -> None:
    one, other = _client(app, BROWSER), _client(app, OTHER_BROWSER)

    _statuses(one, [BRANDING] * LIMIT)

    assert one.get(BRANDING).status_code == 429
    assert other.get(BRANDING).status_code == 200


def test_the_public_window_and_the_sign_in_window_are_separate_buckets(
    app: FastAPI, fake_redis: FakeRedis, clock: Clock
) -> None:
    sign_in = {"phone": "abc", "password": "1234"}  # fails the phone grammar: 422 once past the limit
    one, other = _client(app, BROWSER), _client(app, OTHER_BROWSER)

    # A full sign-in window does not block the branding reads.
    for _ in range(10):
        assert rate_limit.take(rate_limit.signin_key(BROWSER), 10, 60_000, clock.now).allowed
    assert one.post(f"{API}/auth/sign-in", json=sign_in).status_code == 429
    assert _statuses(one, [BRANDING] * LIMIT) == [200] * LIMIT

    # A full public window does not block sign-in.
    assert _statuses(other, [BRANDING] * (LIMIT + 2)) == [200] * LIMIT + [429] * 2
    assert other.post(f"{API}/auth/sign-in", json=sign_in).status_code == 422
    assert fake_redis.count(rate_limit.signin_key(OTHER_BROWSER)) == 1
    assert fake_redis.count(rate_limit.public_key(OTHER_BROWSER)) == LIMIT


def test_behind_a_trusted_proxy_the_viewer_address_is_the_key(app: FastAPI, fake_redis: FakeRedis) -> None:
    behind_proxy = _client(app, PROXY)
    viewer = {"CloudFront-Viewer-Address": "198.51.100.7:44321"}
    other_viewer = {"CloudFront-Viewer-Address": "198.51.100.8:1"}

    assert _statuses(behind_proxy, [BRANDING] * LIMIT, **viewer) == [200] * LIMIT
    assert behind_proxy.get(BRANDING, headers=viewer).status_code == 429
    assert behind_proxy.get(BRANDING, headers=other_viewer).status_code == 200
    assert set(fake_redis.hashes) == {
        rate_limit.public_key("198.51.100.7"),
        rate_limit.public_key("198.51.100.8"),
    }


def test_a_forged_viewer_header_from_an_untrusted_peer_is_ignored(
    app: FastAPI, fake_redis: FakeRedis
) -> None:
    direct = _client(app, BROWSER)

    forged = [
        direct.get(BRANDING, headers={"CloudFront-Viewer-Address": f"192.0.2.{n}:1"})
        for n in range(LIMIT + 2)
    ]

    assert [r.status_code for r in forged] == [200] * LIMIT + [429] * 2  # one key, the peer's own
    assert set(fake_redis.hashes) == {rate_limit.public_key(BROWSER)}


def test_a_trusted_proxy_without_a_usable_header_skips_the_limit(app: FastAPI, fake_redis: FakeRedis) -> None:
    """As for sign-in: one shared bucket for the whole edge would lock everybody out, so no limit applies."""
    behind_proxy = _client(app, PROXY)

    assert _statuses(behind_proxy, [BRANDING] * (LIMIT + 4)) == [200] * (LIMIT + 4)
    assert fake_redis.hashes == {}


def test_a_redis_failure_allows_the_request_and_logs_no_address(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(rate_limit, "get_redis_client", lambda: BrokenRedis())
    caplog.set_level(logging.WARNING, logger=rate_limit.logger.name)
    client = _client(app, BROWSER)

    assert _statuses(client, [BRANDING] * (LIMIT + 3)) == [200] * (LIMIT + 3)
    assert "public" in caplog.text and BROWSER not in caplog.text


@pytest.fixture
def real_redis(monkeypatch: pytest.MonkeyPatch) -> Iterator[redis.Redis]:
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL is not set; this test needs a live Redis")
    client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1, decode_responses=True)
    try:
        client.ping()
    except redis.RedisError as error:
        pytest.skip(f"Redis at TEST_REDIS_URL is not reachable: {error.__class__.__name__}")

    def clear() -> None:
        for key in client.scan_iter(match=f"{rate_limit.PUBLIC_PREFIX}*", count=500):
            client.delete(key)

    clear()
    monkeypatch.setattr(rate_limit, "get_redis_client", lambda: client)
    try:
        yield client
    finally:
        clear()
        client.close()


def test_the_limit_holds_against_a_real_redis(app: FastAPI, real_redis: redis.Redis) -> None:
    client = _client(app, BROWSER)

    statuses = _statuses(client, [BRANDING] * (LIMIT + 1))

    assert statuses == [200] * LIMIT + [429]
    assert int(real_redis.hget(rate_limit.public_key(BROWSER), "c")) == LIMIT
    assert 0 < real_redis.pttl(rate_limit.public_key(BROWSER)) <= 60_000
