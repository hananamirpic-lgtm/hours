"""The rate limits on the real routes (task 17.5; R22.1 to R22.3, R22.5, R22.7, R22.9, R5.13, R1.14).

The application is the real one (`create_app` with the flag on), so the body guard, the route class and
the security headers are in the path. Redis is real (`TEST_REDIS_URL`; the tests `conftest.py` forces
`REDIS_URL` to a closed port, so the real URL comes in under another name) and the clock is injected
through the `get_clock` dependency, so nothing here sleeps. The database is the in-memory SQLite of the
other ticket API tests (one session per request, as in production) and the photo store is
`FakePhotoStore`. Only `svc:rl:` keys are removed from Redis, before and after each test.

What is under test is the wiring: where each limit sits in the order of checks, what is and is not
counted, and what each rejected request leaves behind. The limiter itself is covered by
`test_service_rate_limit_examples.py`.

Skips without `TEST_REDIS_URL` or without a reachable server.
"""

from __future__ import annotations

import itertools
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from test_service_api_photo import FakePhotoStore, jpeg_bytes

from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.main import create_app
from app.models.user import UserRole
from app.service_tickets import rate_limit
from app.service_tickets.api import customer_router, limits
from app.service_tickets.config import ServiceConfig
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.models import Category, Customer, ServiceSettings, SigninFailure, Ticket
from app.service_tickets.photo import MAX_PHOTO_BYTES
from app.service_tickets.photo_store import get_photo_store
from app.service_tickets.rate_limit import KEY_ROOT, request_key, signin_key, submit_key
from app.service_tickets.services import signin as signin_service
from app.service_tickets.services.numbering import TicketNumbersExhausted

API = "/api/service"
PASSWORD = "4321"
WRONG_PASSWORD = "7351"
PHONE = "0505556677"
BROWSER = "203.0.113.9"  # an address outside every trusted range: the key is the peer itself
PROXY = "10.0.0.5"  # inside the default trusted proxy ranges
BAD_PHONE = "abc"  # fails the phone grammar: 422 `invalid_phone`, no verification, no failure row
T0 = 1_700_000_000_000  # the injected clock starts here, in milliseconds
MAX_BODY = 12 * 1024 * 1024


# --------------------------------------------------------------------------- the world


class Clock:
    """The injected clock. A test moves it by assigning `now`."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> int:
        return self.now


class Numbers:
    """The ticket-number allocator of the tests: counts the numbers it hands out."""

    def __init__(self) -> None:
        self.next = 10000
        self.calls = 0
        self.exhausted = False

    def __call__(self, _session: Session) -> int:
        self.calls += 1
        if self.exhausted:
            raise TicketNumbersExhausted
        number = self.next
        self.next += 1
        return number


class Faults:
    commit_fails = False


def _remove_limiter_keys(client: redis.Redis) -> None:
    for key in client.scan_iter(match=f"{KEY_ROOT}*", count=500):
        client.delete(key)


@pytest.fixture
def redis_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[redis.Redis]:
    """A real client, with the limiter pointed at it and the `svc:rl:` keys cleared around the test."""
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL is not set; these tests need a live Redis")
    client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1, decode_responses=True)
    try:
        client.ping()
    except redis.RedisError as error:
        pytest.skip(f"Redis at TEST_REDIS_URL is not reachable: {error.__class__.__name__}")
    _remove_limiter_keys(client)
    monkeypatch.setattr(rate_limit, "get_redis_client", lambda: client)
    try:
        yield client
    finally:
        _remove_limiter_keys(client)
        client.close()


@pytest.fixture(autouse=True)
def numbered() -> Iterator[None]:
    """Number `Category.seq` on insert, as PostgreSQL's identity column does."""
    counter = itertools.count(1)

    def assign(_mapper: Any, _connection: Any, target: Category) -> None:
        if target.seq is None:
            target.seq = next(counter)

    event.listen(Category, "before_insert", assign)
    try:
        yield
    finally:
        event.remove(Category, "before_insert", assign)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def numbers() -> Numbers:
    return Numbers()


@pytest.fixture
def photos() -> FakePhotoStore:
    return FakePhotoStore()


@pytest.fixture
def faults() -> Faults:
    return Faults()


def _default_config() -> ServiceConfig:
    """Every service setting unset, so every default applies (10/60 s, 120/60 s, 10/3600 s)."""
    names = (
        "service_tickets_enabled service_customer_token_ttl_seconds service_signin_rate_max "
        "service_signin_rate_window_seconds service_submit_rate_max service_submit_rate_window_seconds "
        "service_request_rate_max service_request_rate_window_seconds service_signin_lock_attempts "
        "service_signin_lock_seconds service_client_ip_header service_trusted_proxy_cidrs "
        "service_accessibility_contact"
    ).split()
    return ServiceConfig.from_settings(SimpleNamespace(**dict.fromkeys(names, "")))  # type: ignore[arg-type]


@pytest.fixture
def app(
    settings: Any,
    session: Session,
    redis_client: redis.Redis,
    clock: Clock,
    numbers: Numbers,
    photos: FakePhotoStore,
    faults: Faults,
) -> FastAPI:
    changes = {"service_tickets_enabled": "true", "service_accessibility_contact": "a@example.test"}
    application = create_app(settings.model_copy(update={"api_prefix": "/api", **changes}))
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True)

    def request_session() -> Iterator[Session]:
        with factory() as per_request:
            if faults.commit_fails:

                def failing_commit() -> None:
                    raise DBAPIError("COMMIT", {}, Exception("connection lost"))

                per_request.commit = failing_commit  # type: ignore[method-assign]
            yield per_request

    config = _default_config()
    application.dependency_overrides[get_db] = request_session
    application.dependency_overrides[customer_router.get_ticket_allocator] = lambda: numbers
    application.dependency_overrides[customer_router.get_service_config] = lambda: config
    application.dependency_overrides[get_photo_store] = lambda: photos
    application.dependency_overrides[limits.get_clock] = lambda: clock
    return application


class _FromPeer:
    """ASGI wrapper that sets the TCP peer of every request, as uvicorn does (`request.client.host`).

    This Starlette's `TestClient` has no `client=` argument, and its default peer, `testclient`, is not
    an IP address (so it never gets a per-address key).
    """

    def __init__(self, app: FastAPI, peer: str) -> None:
        self.app = app
        self.peer = peer

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "client": (self.peer, 50000)}
        await self.app(scope, receive, send)


def _client(app: FastAPI, peer: str) -> TestClient:
    return TestClient(_FromPeer(app, peer), raise_server_exceptions=False)


@pytest.fixture
def http(app: FastAPI) -> TestClient:
    """A browser that reaches the application directly from `BROWSER`."""
    return _client(app, BROWSER)


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture
def make_customer(session: Session, password_hash: str):
    made = itertools.count()

    def _make(**overrides: Any) -> Customer:
        """The first customer of a test has `PHONE`, the next ones get numbers of their own."""
        index = next(made)
        fields: dict[str, Any] = {
            "phone": PHONE if index == 0 else f"05055500{index:02d}",
            "full_name": "Avi Cohen",
            "full_name_key": "avi cohen",
            "city": "Haifa",
            "apartment": "12",
            "password_hash": password_hash,
            "must_change_password": False,
            "token_version": 1,
        }
        fields.update(overrides)
        created = Customer(**fields)
        session.add(created)
        session.commit()
        return created

    return _make


@pytest.fixture
def customer(make_customer) -> Customer:
    return make_customer()


@pytest.fixture
def category(session: Session) -> Category:
    created = Category(
        name_he="Plumbing-he", name_en="Plumbing", name_he_key="plumbing-he", name_en_key="plumbing"
    )
    session.add(created)
    session.commit()
    return created


@pytest.fixture
def uploads_on(session: Session) -> None:
    session.add(ServiceSettings(id=1, description_required=True, photo_upload_enabled=True))
    session.commit()


# --------------------------------------------------------------------------- helpers


def _bearer(owner: Customer, **kwargs: Any) -> dict[str, str]:
    version = kwargs.pop("version", owner.token_version)
    return {"Authorization": f"Bearer {mint_customer_token(owner.id, version, **kwargs)}"}


def _code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


def _sign_in(http: TestClient, phone: Any = PHONE, password: Any = PASSWORD, **headers: str) -> Any:
    return http.post(f"{API}/auth/sign-in", json={"phone": phone, "password": password}, headers=headers)


def _wire(response: Any) -> tuple[int, dict[str, str], bytes]:
    """Status, headers other than `date`, and body bytes: what "the same answer" means."""
    headers = {name: value for name, value in response.headers.items() if name != "date"}
    return response.status_code, headers, response.content


def _count(client: redis.Redis, key: str) -> int:
    """The counted requests in the window of `key`, 0 when there is no window."""
    return int(client.hget(key, "c") or 0)


def _signin_keys(client: redis.Redis) -> list[str]:
    return sorted(client.scan_iter(match="svc:rl:signin:*"))


def _fill(key: str, max_requests: int, window_seconds: int, at_ms: int = T0) -> None:
    """Take every slot of a window directly, so an HTTP test can start from a full window."""
    for _ in range(max_requests):
        assert rate_limit.take(key, max_requests, window_seconds * 1000, at_ms).allowed


def _failure_rows(session: Session) -> list[tuple[str, datetime]]:
    rows = session.execute(select(SigninFailure.phone, SigninFailure.failed_at).order_by(SigninFailure.id))
    return [tuple(row) for row in rows]  # type: ignore[misc]


def _add_failures(session: Session, phone: str, count: int) -> None:
    for _ in range(count):
        session.add(SigninFailure(phone=phone, failed_at=datetime.now(UTC) - timedelta(seconds=5)))
    session.commit()


class Verifications:
    """Counts the password-hash verifications the sign-in service performs."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        original = signin_service.verify_password

        def counting(password: str, password_hash: str) -> bool:
            self.calls += 1
            return original(password, password_hash)

        monkeypatch.setattr(signin_service, "verify_password", counting)


@pytest.fixture
def verifications(monkeypatch: pytest.MonkeyPatch) -> Verifications:
    return Verifications(monkeypatch)


class Releases:
    """Records every `rate_limit.release` call, then runs it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.keys: list[str] = []
        original = rate_limit.release

        def recording(key: str) -> None:
            self.keys.append(key)
            original(key)

        monkeypatch.setattr(rate_limit, "release", recording)

    def of(self, prefix: str) -> list[str]:
        return [key for key in self.keys if key.startswith(prefix)]


@pytest.fixture
def releases(monkeypatch: pytest.MonkeyPatch) -> Releases:
    return Releases(monkeypatch)


def _form(fields: dict[str, Any], photo: bytes | None = None) -> list[Any]:
    parts: list[Any] = [(name, (None, value)) for name, value in fields.items()]
    if photo is not None:
        parts.append(("photo", ("photo.jpg", photo, "image/jpeg")))
    return parts


def _good(category: Category, **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "city": "Haifa",
        "apartment": "12",
        "phone": "050-555-6677",
        "category_id": str(category.id),
        "description": "The kitchen tap leaks.",
        "urgent": "false",
    }
    fields.update(overrides)
    return fields


def _submit(
    http: TestClient, owner: Customer, category: Category, photo: bytes | None = None, **fields: Any
) -> Any:
    return http.post(f"{API}/tickets", files=_form(_good(category, **fields), photo), headers=_bearer(owner))


def _unparseable_submission(http: TestClient, owner: Customer) -> Any:
    """A multipart body that cannot be parsed: 422 `invalid_request` if the handler gets to read it."""
    headers = {**_bearer(owner), "Content-Type": "multipart/form-data; boundary=zzz"}
    return http.post(f"{API}/tickets", content=b"this is not a multipart body", headers=headers)


def _ticket_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(Ticket)).scalar_one()


# ------------------------------------------------------------------------ sign-in: per address (R22.1, R22.7)


def test_signin_over_the_limit_is_rate_limited_not_too_many_attempts_even_for_a_locked_phone(
    http: TestClient,
    session: Session,
    customer: Customer,
    redis_client: redis.Redis,
    verifications: Verifications,
) -> None:
    """Ten requests from one address: 5 failures lock the phone, then 5 are `too_many_attempts`."""
    for _ in range(5):
        assert _sign_in(http, password=WRONG_PASSWORD).status_code == 401
    for _ in range(5):
        locked = _sign_in(http)
        assert (locked.status_code, _code(locked)) == (429, "too_many_attempts")
    rows_before = _failure_rows(session)
    assert len(rows_before) == 5
    verified_before = verifications.calls

    eleventh = _sign_in(http)  # the same locked phone, the correct password

    assert eleventh.status_code == 429
    assert _code(eleventh) == "rate_limited"
    assert _code(eleventh) != "too_many_attempts"
    assert eleventh.headers["Retry-After"].isdigit()
    assert 1 <= int(eleventh.headers["Retry-After"]) <= 60
    assert verifications.calls == verified_before  # no password-hash verification
    assert _failure_rows(session) == rows_before  # no failure recorded, the lock's end is unchanged
    assert _count(redis_client, signin_key(BROWSER)) == 10  # the rejected request is not counted


def test_a_rejected_signin_is_the_same_bytes_whether_the_phone_is_registered_unregistered_inactive_or_locked(
    http: TestClient,
    session: Session,
    make_customer,
    redis_client: redis.Redis,
    verifications: Verifications,
) -> None:
    registered = make_customer()
    inactive = make_customer(is_active=False)
    locked = make_customer()
    _add_failures(session, locked.phone, 5)
    cases = {
        "registered": (registered.phone, PASSWORD),
        "unregistered": ("0509999999", PASSWORD),
        "inactive": (inactive.phone, PASSWORD),
        "locked": (locked.phone, PASSWORD),
    }
    rows_before = _failure_rows(session)
    answers: dict[str, tuple[int, dict[str, str], bytes]] = {}

    for name, (phone, password) in cases.items():
        _remove_limiter_keys(redis_client)
        _fill(signin_key(BROWSER), 10, 60)
        response = _sign_in(http, phone, password)
        assert _code(response) == "rate_limited", name
        answers[name] = _wire(response)

    assert (
        len({(status, tuple(sorted(headers.items())), body) for status, headers, body in answers.values()})
        == 1
    )
    status, headers, _ = answers["registered"]
    assert status == 429
    assert headers["retry-after"] == "60"
    assert headers["cache-control"] == "no-store"
    assert verifications.calls == 0
    assert _failure_rows(session) == rows_before


def test_the_address_limit_is_checked_before_the_body_is_validated(
    http: TestClient, redis_client: redis.Redis, verifications: Verifications
) -> None:
    """A body with an unknown field is 422 `unknown_field` under the limit and 429 `rate_limited` over it."""
    under = http.post(f"{API}/auth/sign-in", json={"phone": PHONE, "password": PASSWORD, "extra": 1})
    assert (under.status_code, _code(under)) == (422, "unknown_field")
    _remove_limiter_keys(redis_client)
    _fill(signin_key(BROWSER), 10, 60)

    over = http.post(f"{API}/auth/sign-in", json={"phone": PHONE, "password": PASSWORD, "extra": 1})
    invalid_phone = _sign_in(http, BAD_PHONE)

    assert (over.status_code, _code(over)) == (429, "rate_limited")
    assert (invalid_phone.status_code, _code(invalid_phone)) == (429, "rate_limited")
    assert verifications.calls == 0


def test_signin_failures_and_validation_errors_all_count_toward_the_address_window(
    http: TestClient, redis_client: redis.Redis
) -> None:
    for _ in range(10):
        assert _code(_sign_in(http, BAD_PHONE)) == "invalid_phone"

    assert _code(_sign_in(http, BAD_PHONE)) == "rate_limited"
    assert _count(redis_client, signin_key(BROWSER)) == 10


def test_a_token_sent_with_signin_is_ignored_and_a_signin_is_never_counted_as_a_customer_request(
    http: TestClient, customer: Customer, redis_client: redis.Redis
) -> None:
    for _ in range(3):
        response = _sign_in(http, **_bearer(customer))
        assert response.status_code == 200

    assert _count(redis_client, signin_key(BROWSER)) == 3
    assert redis_client.exists(request_key(customer.id)) == 0


# ------------------------------------------------------------------------ sign-in: the client address (R22.1)


def test_no_usable_client_address_skips_the_signin_limit(app: FastAPI, redis_client: redis.Redis) -> None:
    """A trusted proxy that sends no viewer header gives no address, so there is no per-address limit."""
    behind_proxy = _client(app, PROXY)

    answers = [_sign_in(behind_proxy, BAD_PHONE) for _ in range(15)]

    assert {(r.status_code, _code(r)) for r in answers} == {(422, "invalid_phone")}
    assert _signin_keys(redis_client) == []

    garbage = [
        _sign_in(behind_proxy, BAD_PHONE, **{"CloudFront-Viewer-Address": "not-an-address"})
        for _ in range(12)
    ]
    assert {r.status_code for r in garbage} == {422}
    assert _signin_keys(redis_client) == []


def test_a_trusted_proxy_gives_the_viewer_address_as_the_key(app: FastAPI, redis_client: redis.Redis) -> None:
    behind_proxy = _client(app, PROXY)
    viewer = {"CloudFront-Viewer-Address": "198.51.100.7:44321"}

    for _ in range(10):
        assert _sign_in(behind_proxy, BAD_PHONE, **viewer).status_code == 422
    over = _sign_in(behind_proxy, BAD_PHONE, **viewer)
    other = _sign_in(behind_proxy, BAD_PHONE, **{"CloudFront-Viewer-Address": "198.51.100.8:1"})

    assert (over.status_code, _code(over)) == (429, "rate_limited")
    assert other.status_code == 422  # another viewer behind the same proxy has a window of its own
    assert _signin_keys(redis_client) == [signin_key("198.51.100.7"), signin_key("198.51.100.8")]


def test_a_viewer_address_header_from_an_untrusted_peer_does_not_change_the_key(
    http: TestClient, redis_client: redis.Redis
) -> None:
    statuses = [
        _sign_in(http, BAD_PHONE, **{"CloudFront-Viewer-Address": f"192.0.2.{n}:5000"}).status_code
        for n in range(1, 13)
    ]

    assert statuses == [422] * 10 + [429, 429]
    assert _signin_keys(redis_client) == [signin_key(BROWSER)]


def test_an_ipv6_client_shares_one_window_per_64(app: FastAPI, redis_client: redis.Redis) -> None:
    v6 = _client(app, "2001:db8:1:2:aaaa::1")
    same_64 = _client(app, "2001:db8:1:2:bbbb::9")

    for _ in range(10):
        assert _sign_in(v6, BAD_PHONE).status_code == 422

    assert _code(_sign_in(same_64, BAD_PHONE)) == "rate_limited"
    assert _signin_keys(redis_client) == [signin_key("2001:db8:1:2::/64")]


# --------------------------------------------------------------------------- customer requests (R22.3)

ENDPOINTS = {
    "ungated": "/auth/me",
    "gated": "/form-config",
    "photo-customer-branch": "/tickets/10000/photo",
}


@pytest.mark.parametrize("path", ENDPOINTS.values(), ids=ENDPOINTS.keys())
def test_the_121st_customer_request_in_60_seconds_is_429_and_sign_in_is_not_counted(
    http: TestClient, customer: Customer, redis_client: redis.Redis, clock: Clock, path: str
) -> None:
    # Sign-ins of the same customer are not part of the count.
    for _ in range(3):
        assert _sign_in(http).status_code == 200
    assert redis_client.exists(request_key(customer.id)) == 0

    first_status = http.get(f"{API}{path}", headers=_bearer(customer)).status_code
    assert first_status != 429
    clock.now = T0 + 15_500
    # Several open sessions of the customer (distinct tokens) share one window.
    others = [http.get(f"{API}{path}", headers=_bearer(customer)).status_code for _ in range(119)]
    assert 429 not in others
    assert _count(redis_client, request_key(customer.id)) == 120

    over = http.get(f"{API}{path}", headers=_bearer(customer))
    again = http.get(f"{API}{path}", headers=_bearer(customer))

    assert (over.status_code, _code(over)) == (429, "rate_limited")
    assert over.headers["Retry-After"] == "45"  # 44.5 s left in a window that opened at T0
    assert _wire(over) == _wire(again)
    assert _count(redis_client, request_key(customer.id)) == 120  # the 429s were not counted

    clock.now = T0 + 60_000  # the window has ended
    assert http.get(f"{API}{path}", headers=_bearer(customer)).status_code != 429
    assert _count(redis_client, request_key(customer.id)) == 1


def test_each_customer_has_a_window_of_their_own(
    http: TestClient, make_customer, redis_client: redis.Redis
) -> None:
    busy, quiet = make_customer(), make_customer()
    _fill(request_key(busy.id), 120, 60)

    assert http.get(f"{API}/auth/me", headers=_bearer(busy)).status_code == 429
    assert http.get(f"{API}/auth/me", headers=_bearer(quiet)).status_code == 200


def test_tokens_that_do_not_verify_are_not_counted(
    http: TestClient, customer: Customer, redis_client: redis.Redis, make_user
) -> None:
    valid = mint_customer_token(customer.id, 1)
    forged = jwt.encode(
        {"sub": str(customer.id), "typ": "customer", "aud": "hours-service-customer", "ver": 1,
         "jti": "x", "iat": 1, "exp": 4_000_000_000},
        "another-key-entirely-not-the-derived-one",
        algorithm="HS256",
    )  # fmt: skip
    technician = make_user(role=UserRole.TECHNICIAN, is_2fa_enabled=True, must_change_password=False)
    staff = create_access_token(
        user_id=technician.id, token_version=technician.token_version, role="technician"
    )
    tokens = {
        "bad signature": valid[:-4] + ("AAAA" if not valid.endswith("AAAA") else "BBBB"),
        "wrong key": forged,
        "expired": mint_customer_token(customer.id, 1, now=datetime(2020, 1, 1, tzinfo=UTC)),
        "staff token": staff,
        "garbage": "not-a-jwt",
    }
    expected = {"bad signature": 401, "wrong key": 401, "expired": 401, "staff token": 403, "garbage": 401}

    for name, token in tokens.items():
        for path in ("/auth/me", "/form-config", "/tickets"):
            for _ in range(41):  # 123 requests per kind: more than the 120 allowed
                response = http.get(f"{API}{path}", headers={"Authorization": f"Bearer {token}"})
                assert response.status_code == expected[name], (name, path)
    missing = [http.get(f"{API}/auth/me").status_code for _ in range(125)]

    assert set(missing) == {401}
    assert list(redis_client.scan_iter(match="svc:rl:req:*")) == []  # nothing was counted
    assert http.get(f"{API}/auth/me", headers=_bearer(customer)).status_code == 200


def test_a_revoked_or_inactive_token_gets_its_401_under_the_limit_and_the_same_429_over_it(
    http: TestClient, make_customer, redis_client: redis.Redis
) -> None:
    """The window sits after the signature check and before the row read: 429 never reveals account state."""
    revoked = make_customer(token_version=2)
    inactive = make_customer(is_active=False)
    live = make_customer()
    stale = {"Authorization": f"Bearer {mint_customer_token(revoked.id, 1)}"}

    # Under the limit: the 401 of the row read, and each such request is counted.
    first = http.get(f"{API}/auth/me", headers=stale)
    assert (first.status_code, _code(first)) == (401, "invalid_token")
    inactive_first = http.get(f"{API}/auth/me", headers=_bearer(inactive))
    assert (inactive_first.status_code, _code(inactive_first)) == (401, "invalid_token")
    assert _count(redis_client, request_key(revoked.id)) == 1
    assert _count(redis_client, request_key(inactive.id)) == 1

    # Fill the three windows, then compare the three rejected answers.
    _remove_limiter_keys(redis_client)
    for owner in (revoked, inactive, live):
        _fill(request_key(owner.id), 120, 60)
    answers = {
        "revoked": _wire(http.get(f"{API}/auth/me", headers=stale)),
        "inactive": _wire(http.get(f"{API}/auth/me", headers=_bearer(inactive))),
        "live": _wire(http.get(f"{API}/auth/me", headers=_bearer(live))),
    }
    assert {status for status, _, _ in answers.values()} == {429}
    assert len({(tuple(sorted(h.items())), body) for _, h, body in answers.values()}) == 1


def test_a_deactivated_customer_is_401_once_the_window_has_room_again(
    http: TestClient, customer: Customer, redis_client: redis.Redis, clock: Clock
) -> None:
    stale = _bearer(customer, version=customer.token_version + 1)
    _fill(request_key(customer.id), 120, 60)
    assert http.get(f"{API}/auth/me", headers=stale).status_code == 429

    clock.now = T0 + 60_000

    assert http.get(f"{API}/auth/me", headers=stale).status_code == 401


def test_staff_tokens_are_not_rate_limited_by_this_feature(
    http: TestClient, redis_client: redis.Redis, make_user
) -> None:
    technician = make_user(role=UserRole.TECHNICIAN, is_2fa_enabled=True, must_change_password=False)
    token = create_access_token(
        user_id=technician.id, token_version=technician.token_version, role="technician"
    )

    statuses = {
        http.get(f"{API}/tickets/10000/photo", headers={"Authorization": f"Bearer {token}"}).status_code
        for _ in range(130)
    }

    assert statuses == {404}  # `ticket_not_found`, never 429
    assert list(redis_client.scan_iter(match="svc:rl:*")) == []


def test_a_change_password_answered_too_many_attempts_is_not_counted(
    http: TestClient, session: Session, customer: Customer, redis_client: redis.Redis
) -> None:
    _add_failures(session, customer.phone, 5)

    for _ in range(3):
        response = http.post(
            f"{API}/auth/change-password",
            json={"current_password": PASSWORD, "new_password": "5566"},
            headers=_bearer(customer),
        )
        assert (response.status_code, _code(response)) == (429, "too_many_attempts")

    assert redis_client.exists(request_key(customer.id)) == 0  # counted by the principal, then given back


# --------------------------------------------------------------------------- submissions (R22.2)


def test_the_11th_created_ticket_in_an_hour_is_429_and_creates_nothing(
    http: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    uploads_on: None,
    redis_client: redis.Redis,
    photos: FakePhotoStore,
    numbers: Numbers,
    clock: Clock,
) -> None:
    for number in range(10):
        created = _submit(http, customer, category, jpeg_bytes())
        assert created.status_code == 201, number
    assert _ticket_count(session) == 10
    assert len(photos.objects) == 10
    allocated, calls, ops = numbers.next, numbers.calls, list(photos.ops)
    assert _count(redis_client, submit_key(customer.id)) == 10
    assert _count(redis_client, request_key(customer.id)) == 10
    clock.now = T0 + 600_000

    over = _submit(http, customer, category, jpeg_bytes())

    assert (over.status_code, _code(over)) == (429, "rate_limited")
    assert over.headers["Retry-After"] == "3000"  # 3,600 s window opened at T0, 600 s ago
    assert _ticket_count(session) == 10
    assert len(photos.objects) == 10
    assert photos.ops == ops  # no put, no delete: no object was stored for the rejected submission
    assert (numbers.next, numbers.calls) == (allocated, calls)  # no ticket number was taken
    assert _count(redis_client, submit_key(customer.id)) == 10  # a rejected submission holds no slot
    # The 60 s request window of the ten submissions ended 9 minutes ago. The rejected submission opened a
    # new one and, being answered 429, gave its slot back: nothing is counted.
    assert _count(redis_client, request_key(customer.id)) == 0

    clock.now = T0 + 3_600_000  # the window has ended
    assert _submit(http, customer, category, jpeg_bytes()).status_code == 201
    assert _count(redis_client, submit_key(customer.id)) == 1


def test_the_slot_is_reserved_before_the_form_is_read(
    http: TestClient, customer: Customer, redis_client: redis.Redis, releases: Releases
) -> None:
    _fill(submit_key(customer.id), 10, 3600)

    response = _unparseable_submission(http, customer)  # would be 422 `invalid_request` if it were read

    assert (response.status_code, _code(response)) == (429, "rate_limited")
    assert releases.of("svc:rl:submit:") == []  # a rejected reservation is not released


def test_a_failed_or_rejected_submission_does_not_consume_a_slot_and_releases_exactly_once(
    http: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    uploads_on: None,
    redis_client: redis.Redis,
    photos: FakePhotoStore,
    numbers: Numbers,
    faults: Faults,
    releases: Releases,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exploding_put(key: str, data: bytes) -> None:
        raise RuntimeError("the store blew up")

    outcomes: list[tuple[str, Any]] = []

    outcomes.append(("422 city", _submit(http, customer, category, city="")))
    outcomes.append(("422 category", _submit(http, customer, category, category_id=str(uuid.uuid4()))))
    outcomes.append(("422 unparseable", _unparseable_submission(http, customer)))
    outcomes.append(("422 photo type", _submit(http, customer, category, b"not an image")))
    outcomes.append(("413 photo", _submit(http, customer, category, b"\0" * (MAX_PHOTO_BYTES + 1))))
    photos.fail_put = True
    outcomes.append(("503 store", _submit(http, customer, category, jpeg_bytes())))
    photos.fail_put = False
    numbers.exhausted = True
    outcomes.append(("503 numbers", _submit(http, customer, category)))
    numbers.exhausted = False
    faults.commit_fails = True
    outcomes.append(("503 commit", _submit(http, customer, category)))
    faults.commit_fails = False
    with monkeypatch.context() as patch:
        patch.setattr(photos, "put", exploding_put)
        outcomes.append(("500 raised", _submit(http, customer, category, jpeg_bytes())))
    # A body over 12 MiB is refused by the body guard before the handler, and so takes no slot at all.
    headers = {**_bearer(customer), "Content-Type": "multipart/form-data; boundary=zzz"}
    outcomes.append(("413 body", http.post(f"{API}/tickets", content=b"x" * (MAX_BODY + 1), headers=headers)))

    assert [(name, r.status_code) for name, r in outcomes] == [
        ("422 city", 422),
        ("422 category", 422),
        ("422 unparseable", 422),
        ("422 photo type", 422),
        ("413 photo", 413),
        ("503 store", 503),
        ("503 numbers", 503),
        ("503 commit", 503),
        ("500 raised", 500),
        ("413 body", 413),
    ]
    assert _code(dict(outcomes)["413 photo"]) == "photo_too_large"
    assert _code(dict(outcomes)["413 body"]) == "request_too_large"
    assert _ticket_count(session) == 0
    assert redis_client.exists(submit_key(customer.id)) == 0  # no slot is held
    # One release for each outcome that reserved a slot (all but the body guard's), none for a ticket.
    assert releases.of("svc:rl:submit:") == [submit_key(customer.id)] * 9

    # The customer can still create the full ten, and the 11th is the one that is refused.
    for _ in range(10):
        assert _submit(http, customer, category).status_code == 201
    assert releases.of("svc:rl:submit:") == [submit_key(customer.id)] * 9  # created tickets release nothing
    assert _submit(http, customer, category).status_code == 429
    assert _ticket_count(session) == 10


def test_a_submission_rejected_by_the_submit_window_is_not_counted_as_a_request(
    http: TestClient, customer: Customer, category: Category, redis_client: redis.Redis
) -> None:
    _fill(submit_key(customer.id), 10, 3600)

    for _ in range(3):
        assert _submit(http, customer, category).status_code == 429

    assert redis_client.exists(request_key(customer.id)) == 0


# -------------------------------------------------------------------- Retry-After matches the window (R22.9)

WINDOW_OFFSETS_MS = [0, 1, 999, 1_000, 30_000, 59_000, 59_001, 59_999]


def _seconds_left(window_seconds: int, elapsed_ms: int) -> int:
    return min(max(-(-(window_seconds * 1000 - elapsed_ms) // 1000), 1), window_seconds)


@pytest.mark.parametrize("elapsed_ms", WINDOW_OFFSETS_MS)
def test_retry_after_of_each_limit_is_the_time_left_rounded_up(
    app: FastAPI, make_customer, redis_client: redis.Redis, clock: Clock, elapsed_ms: int
) -> None:
    browser = _client(app, BROWSER)
    customer, submitter = make_customer(), make_customer()
    _fill(signin_key(BROWSER), 10, 60)
    _fill(request_key(customer.id), 120, 60)
    _fill(submit_key(submitter.id), 10, 3600)  # the submitter's request window has room
    clock.now = T0 + elapsed_ms

    signin = _sign_in(browser, BAD_PHONE)
    request = browser.get(f"{API}/auth/me", headers=_bearer(customer))
    submit = _unparseable_submission(browser, submitter)

    assert [r.status_code for r in (signin, request, submit)] == [429, 429, 429]
    assert [_code(r) for r in (signin, request, submit)] == ["rate_limited"] * 3
    assert int(signin.headers["Retry-After"]) == _seconds_left(60, elapsed_ms)
    assert int(request.headers["Retry-After"]) == _seconds_left(60, elapsed_ms)
    assert int(submit.headers["Retry-After"]) == _seconds_left(3600, elapsed_ms)


def test_a_request_sent_after_retry_after_is_not_rejected_by_the_same_limit(
    app: FastAPI, redis_client: redis.Redis, clock: Clock
) -> None:
    browser = _client(app, BROWSER)
    _fill(signin_key(BROWSER), 10, 60)
    clock.now = T0 + 12_300
    rejected = _sign_in(browser, BAD_PHONE)
    assert int(rejected.headers["Retry-After"]) == 48

    clock.now = T0 + 59_999  # still inside the window that opened at T0
    assert _sign_in(browser, BAD_PHONE).status_code == 429
    clock.now = T0 + 12_300 + 48 * 1000  # the request sent `Retry-After` seconds after the rejection

    assert _sign_in(browser, BAD_PHONE).status_code == 422


# --------------------------------------------------------------------------- Redis down (R22.5)


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch) -> redis.Redis:
    down = redis.Redis.from_url(
        "redis://127.0.0.1:1/0", socket_connect_timeout=0.25, socket_timeout=0.25, decode_responses=True
    )
    monkeypatch.setattr(rate_limit, "get_redis_client", lambda: down)
    return down


def test_with_redis_down_requests_are_allowed_and_the_signin_lock_still_applies(
    app: FastAPI,
    session: Session,
    customer: Customer,
    category: Category,
    redis_down: redis.Redis,
) -> None:
    browser = _client(app, BROWSER)

    # No per-address limit: 15 requests from one address, none of them 429 `rate_limited`.
    assert {_code(_sign_in(browser, BAD_PHONE)) for _ in range(15)} == {"invalid_phone"}
    # No per-customer limit, and no submission limit: 11 tickets are created.
    assert {browser.get(f"{API}/auth/me", headers=_bearer(customer)).status_code for _ in range(125)} == {200}
    assert [_submit(browser, customer, category).status_code for _ in range(11)] == [201] * 11
    assert _ticket_count(session) == 11

    # The lock does not depend on Redis: five failures, then the correct password is refused.
    for _ in range(5):
        assert _sign_in(browser, password=WRONG_PASSWORD).status_code == 401
    locked = _sign_in(browser)
    assert (locked.status_code, _code(locked)) == (429, "too_many_attempts")
    assert locked.headers["Retry-After"].isdigit()
