"""Photo store, decode semaphore and WebP start-up check (R11.8, R20.11, R24.5; task 16.3).

The store is tested with a fake S3 client, so nothing reaches an object store. The semaphore tests use a
tiny wait instead of the real 30 seconds and hold the slots themselves.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from http import HTTPStatus

import boto3
import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import main as app_main
from app.repositories import health as health_repository
from app.service_tickets import photo_store
from app.service_tickets.config import ServiceConfig
from app.service_tickets.error_codes import (
    CODE_PHOTO_STORAGE_UNAVAILABLE,
    CODE_SERVICE_UNAVAILABLE,
    CODE_TICKET_NOT_FOUND,
)
from app.service_tickets.mount import add_startup_check, mount
from app.service_tickets.photo import InvalidPhotoType, ProcessedPhoto
from app.service_tickets.photo_store import (
    KEY_PREFIX,
    PhotoNotFound,
    PhotoStorageUnavailable,
    PhotoStore,
    check_key,
    check_webp_support,
    get_photo_store,
    new_key,
    process_photo_limited,
)

BUCKET = "ticket-test-bucket"
KEY_PATTERN = re.compile(r"^service-tickets/[0-9a-f]{32}\.jpg$")


# --------------------------------------------------------------------------- fake client


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        self.closed = True


def _client_error(code: str, operation: str = "GetObject") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, operation)


class FakeClient:
    """Records every call and keeps objects in a dict. `fail` raises on any call."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict]] = []
        self.bodies: list[FakeBody] = []
        self.fail = fail

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        if self.fail:
            raise self.fail
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {}

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        if self.fail:
            raise self.fail
        if kwargs["Key"] not in self.objects:
            raise _client_error("NoSuchKey")
        body = FakeBody(self.objects[kwargs["Key"]])
        self.bodies.append(body)
        return {"Body": body}

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        if self.fail:
            raise self.fail
        self.objects.pop(kwargs["Key"], None)
        return {}


@pytest.fixture(autouse=True)
def _usable_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Alembic run earlier in the process (`fileConfig`) can leave this logger disabled."""
    monkeypatch.setattr(photo_store.logger, "disabled", False)
    monkeypatch.setattr(photo_store.logger, "propagate", True)


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
def store(fake: FakeClient) -> PhotoStore:
    return PhotoStore(fake, bucket=BUCKET)


# --------------------------------------------------------------------------- keys

BAD_KEYS = [
    pytest.param("other/x.jpg", id="wrong-prefix"),
    pytest.param("Service-Tickets/x.jpg", id="wrong-case"),
    pytest.param("/service-tickets/x.jpg", id="leading-slash"),
    pytest.param("employees/1/x.jpg", id="hours-prefix"),
    pytest.param("exports/1/x.pdf", id="hours-exports"),
    pytest.param("service-tickets", id="no-slash"),
    pytest.param("service-tickets/", id="prefix-only"),
    pytest.param("service-tickets/../x", id="traversal"),
    pytest.param("service-tickets/a/../../x", id="traversal-nested"),
    pytest.param("service-tickets/./x", id="dot-segment"),
    pytest.param("service-tickets//x", id="empty-segment"),
    pytest.param("service-tickets/x/", id="trailing-slash"),
    pytest.param("service-tickets/a\\b.jpg", id="backslash"),
    pytest.param("service-tickets/a\nb.jpg", id="newline"),
    pytest.param("service-tickets/a\x00b.jpg", id="nul"),
    pytest.param("", id="empty"),
    pytest.param(None, id="none"),
    pytest.param(b"service-tickets/x.jpg", id="bytes"),
    pytest.param(5, id="int"),
]


@pytest.mark.parametrize("key", BAD_KEYS)
@pytest.mark.parametrize("method", ["put", "get", "delete"])
def test_every_method_rejects_a_bad_key_before_any_call(
    store: PhotoStore, fake: FakeClient, method: str, key: object
) -> None:
    with pytest.raises(ValueError):
        if method == "put":
            store.put(key, b"data")  # type: ignore[arg-type]
        else:
            getattr(store, method)(key)
    assert fake.calls == []


@pytest.mark.parametrize("key", ["service-tickets/abc.jpg", "service-tickets/" + "a" * 32 + ".jpg"])
def test_valid_keys_are_accepted(key: str) -> None:
    assert check_key(key) == key


def test_new_key_is_valid_for_the_store() -> None:
    key = new_key()
    assert check_key(key) == key


def test_new_key_pattern_and_uniqueness() -> None:
    keys = [new_key() for _ in range(500)]
    assert all(KEY_PATTERN.match(key) for key in keys)
    assert len(set(keys)) == 500
    assert all(key.startswith(KEY_PREFIX) for key in keys)


# --------------------------------------------------------------------------- put, get, delete


def test_put_sends_bucket_key_body_and_content_type_only(store: PhotoStore, fake: FakeClient) -> None:
    key = new_key()
    store.put(key, b"\xff\xd8jpeg")
    assert fake.calls == [
        ("put_object", {"Bucket": BUCKET, "Key": key, "Body": b"\xff\xd8jpeg", "ContentType": "image/jpeg"})
    ]
    # No public ACL and no encryption argument: the Hours store sets neither.
    sent = fake.calls[0][1]
    assert "ACL" not in sent
    assert not any(name.startswith("ServerSideEncryption") or name.startswith("SSE") for name in sent)


def test_put_rejects_non_bytes(store: PhotoStore, fake: FakeClient) -> None:
    with pytest.raises(TypeError):
        store.put(new_key(), "text")  # type: ignore[arg-type]
    assert fake.calls == []


def test_get_returns_the_stored_bytes_and_closes_the_body(store: PhotoStore, fake: FakeClient) -> None:
    key = new_key()
    store.put(key, b"jpeg-bytes")
    assert store.get(key) == b"jpeg-bytes"
    assert fake.calls[-1] == ("get_object", {"Bucket": BUCKET, "Key": key})
    assert fake.bodies[-1].closed is True


@pytest.mark.parametrize("code", ["NoSuchKey", "404", "NotFound"])
def test_get_of_a_missing_object_is_photo_not_found(code: str) -> None:
    client = FakeClient(fail=_client_error(code))
    with pytest.raises(PhotoNotFound) as info:
        PhotoStore(client, bucket=BUCKET).get(new_key())
    assert info.value.code == CODE_TICKET_NOT_FOUND
    assert not isinstance(info.value, PhotoStorageUnavailable)


@pytest.mark.parametrize(
    "failure",
    [_client_error("AccessDenied"), _client_error("NoSuchBucket"), ConnectTimeoutError(endpoint_url="http://x")],
)
def test_other_get_failures_are_storage_unavailable(failure: Exception) -> None:
    store = PhotoStore(FakeClient(fail=failure), bucket=BUCKET)
    with pytest.raises(PhotoStorageUnavailable) as info:
        store.get(new_key())
    assert info.value.code == CODE_PHOTO_STORAGE_UNAVAILABLE
    assert not isinstance(info.value, PhotoNotFound)


def test_put_failure_is_storage_unavailable() -> None:
    store = PhotoStore(FakeClient(fail=_client_error("InternalError", "PutObject")), bucket=BUCKET)
    with pytest.raises(PhotoStorageUnavailable):
        store.put(new_key(), b"x")


def test_delete_removes_the_object(store: PhotoStore, fake: FakeClient) -> None:
    key = new_key()
    store.put(key, b"x")
    assert store.delete(key) is True
    assert key not in fake.objects
    assert fake.calls[-1] == ("delete_object", {"Bucket": BUCKET, "Key": key})


def test_delete_is_best_effort_and_logs_instead_of_raising(caplog: pytest.LogCaptureFixture) -> None:
    store = PhotoStore(FakeClient(fail=_client_error("InternalError", "DeleteObject")), bucket=BUCKET)
    with caplog.at_level(logging.ERROR, logger=photo_store.logger.name):
        assert store.delete(new_key()) is False
    assert any("delete failed" in record.getMessage() for record in caplog.records)


def test_the_store_offers_no_listing_and_no_presigning() -> None:
    public = {name for name in dir(PhotoStore) if not name.startswith("_")}
    assert public == {"put", "get", "delete", "bucket"}
    assert not [name for name in dir(photo_store) if "presign" in name.lower() or name.startswith("list")]


# --------------------------------------------------------------------------- the client


class _Spy:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.client = FakeClient()

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.client


def test_client_is_built_with_the_ticket_timeouts_and_one_retry(
    monkeypatch: pytest.MonkeyPatch, settings
) -> None:
    spy = _Spy()
    monkeypatch.setattr(boto3, "client", spy)
    # The Hours client and its helpers must never be used.
    for name in ("get_object_storage_client", "get_object_storage_presign_client"):
        monkeypatch.setattr(health_repository, name, lambda: pytest.fail("Hours client touched"))

    store = PhotoStore()
    assert spy.calls == []  # lazy: nothing is built by the constructor
    key = new_key()
    store.put(key, b"x")
    assert store.get(key) == b"x"
    assert len(spy.calls) == 1  # one client, reused

    (args, kwargs) = spy.calls[0]
    assert args == ("s3",)
    assert kwargs["endpoint_url"] == settings.s3_endpoint_url
    assert kwargs["aws_access_key_id"] == settings.s3_access_key_id.get_secret_value()
    assert kwargs["aws_secret_access_key"] == settings.s3_secret_access_key.get_secret_value()
    assert kwargs["region_name"] == settings.s3_region
    config = kwargs["config"]
    assert config.connect_timeout == 3
    assert config.read_timeout == 10
    assert config.signature_version == "s3v4"
    # Legacy mode counts retries, so max_attempts=1 is one try plus one retry.
    assert config.retries == {"max_attempts": 1, "mode": "legacy"}
    assert spy.client.calls[0][1]["Bucket"] == settings.s3_bucket_documents


def test_the_client_is_not_shared_with_hours(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    spy = _Spy()
    monkeypatch.setattr(boto3, "client", spy)
    hours_client = object()
    monkeypatch.setattr(health_repository, "get_object_storage_client", lambda: hours_client)
    store = PhotoStore()
    store.put(new_key(), b"x")
    assert store._client is spy.client  # noqa: SLF001
    assert store._client is not hours_client  # noqa: SLF001


def test_nothing_is_built_at_import_or_by_the_dependency(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("boto3 client built too early")

    monkeypatch.setattr(boto3, "client", fail)
    get_photo_store.cache_clear()
    try:
        first = get_photo_store()
        assert first._client is None  # noqa: SLF001
        assert get_photo_store() is first  # singleton
    finally:
        get_photo_store.cache_clear()


def test_dependency_can_be_overridden(fake: FakeClient) -> None:
    from fastapi import Depends

    app = FastAPI()

    @app.get("/probe")
    def probe(store: PhotoStore = Depends(get_photo_store)) -> dict[str, str]:  # noqa: B008
        return {"bucket": store.bucket}

    app.dependency_overrides[get_photo_store] = lambda: PhotoStore(fake, bucket="overridden")
    assert TestClient(app).get("/probe").json() == {"bucket": "overridden"}


# --------------------------------------------------------------------------- the decode semaphore


def _free_slots() -> int:
    """Number of slots that can be taken right now (and gives them back)."""
    taken = 0
    try:
        while photo_store._DECODE_SLOTS.acquire(blocking=False):  # noqa: SLF001
            taken += 1
    finally:
        for _ in range(taken):
            photo_store._DECODE_SLOTS.release()  # noqa: SLF001
    return taken


def _fake_result() -> ProcessedPhoto:
    return ProcessedPhoto(data=b"jpeg", width=1, height=1)


def test_all_three_slots_are_free_at_rest() -> None:
    assert _free_slots() == photo_store.MAX_CONCURRENT_DECODES == 3


def test_the_default_wait_is_thirty_seconds() -> None:
    assert photo_store.DECODE_WAIT_SECONDS == 30


def test_a_free_slot_runs_process_photo_and_returns_its_result(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bytes] = []
    monkeypatch.setattr(photo_store, "process_photo", lambda data: (seen.append(data), _fake_result())[1])
    assert process_photo_limited(b"abc") == _fake_result()
    assert seen == [b"abc"]
    assert _free_slots() == 3


def test_timeout_when_all_slots_are_held_gives_service_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(photo_store, "process_photo", lambda data: pytest.fail("must not decode"))
    held = 0
    try:
        for _ in range(3):
            assert photo_store._DECODE_SLOTS.acquire(blocking=False)  # noqa: SLF001
            held += 1
        started = time.monotonic()
        with pytest.raises(HTTPException) as info:
            process_photo_limited(b"abc", wait_seconds=0.05)
        assert time.monotonic() - started < 5
    finally:
        for _ in range(held):
            photo_store._DECODE_SLOTS.release()  # noqa: SLF001
    assert info.value.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert info.value.detail == {"error": {"code": CODE_SERVICE_UNAVAILABLE}}
    assert _free_slots() == 3  # the failed wait took no slot


def test_a_request_gets_the_slot_when_one_is_released_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(photo_store, "process_photo", lambda data: _fake_result())
    for _ in range(3):
        assert photo_store._DECODE_SLOTS.acquire(blocking=False)  # noqa: SLF001
    releaser = threading.Timer(0.1, photo_store._DECODE_SLOTS.release)  # noqa: SLF001
    releaser.start()
    try:
        assert process_photo_limited(b"x", wait_seconds=5) == _fake_result()
    finally:
        releaser.join()
        for _ in range(2):
            photo_store._DECODE_SLOTS.release()  # noqa: SLF001
    assert _free_slots() == 3


def test_the_slot_is_released_after_any_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(data: bytes):
        raise RuntimeError("decoder crashed")

    monkeypatch.setattr(photo_store, "process_photo", boom)
    for _ in range(10):  # more calls than slots: a leak would exhaust them
        with pytest.raises(RuntimeError):
            process_photo_limited(b"x", wait_seconds=0.5)
    assert _free_slots() == 3


def test_the_slot_is_released_after_a_photo_error_from_real_processing() -> None:
    for _ in range(5):
        with pytest.raises(InvalidPhotoType):
            process_photo_limited(b"not an image", wait_seconds=0.5)
    assert _free_slots() == 3


def test_at_most_three_decodes_run_at_the_same_time(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}
    # Three threads must be inside at once for any of them to leave, so the peak is exactly three.
    barrier = threading.Barrier(3, timeout=10)

    def fake_process(data: bytes) -> ProcessedPhoto:
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        try:
            barrier.wait()
            time.sleep(0.02)
        finally:
            with lock:
                state["now"] -= 1
        return _fake_result()

    monkeypatch.setattr(photo_store, "process_photo", fake_process)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            process_photo_limited(b"x", wait_seconds=20)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert state["peak"] == 3
    assert state["now"] == 0
    assert _free_slots() == 3


# --------------------------------------------------------------------------- WebP check


def _patch_webp(monkeypatch: pytest.MonkeyPatch, result) -> None:
    from PIL import features

    def check(name: str):
        assert name == "webp"
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(features, "check", check)


def test_webp_check_logs_an_error_when_unsupported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_webp(monkeypatch, False)
    with caplog.at_level(logging.DEBUG, logger=photo_store.logger.name):
        assert check_webp_support() is False
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "WebP" in errors[0].getMessage()


def test_webp_check_is_silent_when_supported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_webp(monkeypatch, True)
    with caplog.at_level(logging.DEBUG, logger=photo_store.logger.name):
        assert check_webp_support() is True
    assert caplog.records == []


def test_webp_check_never_raises(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _patch_webp(monkeypatch, RuntimeError("pillow broke"))
    with caplog.at_level(logging.ERROR, logger=photo_store.logger.name):
        assert check_webp_support() is False
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_the_real_pillow_build_supports_webp() -> None:
    assert check_webp_support() is True


# --------------------------------------------------------------------------- start-up hook through mount


def _config(settings, *, enabled: bool) -> ServiceConfig:
    return ServiceConfig.from_settings(
        settings.model_copy(update={"service_tickets_enabled": "true" if enabled else ""})
    )


def _bare_app(events: list[str]) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        events.append("lifespan-start")
        yield {"marker": 1}
        events.append("lifespan-stop")

    return FastAPI(lifespan=lifespan)


def test_flag_on_mount_runs_the_webp_check_before_the_existing_lifespan(
    monkeypatch: pytest.MonkeyPatch, settings
) -> None:
    events: list[str] = []
    monkeypatch.setattr(photo_store, "check_webp_support", lambda: events.append("webp-check"))
    app = _bare_app(events)
    mount(app, _config(settings, enabled=True), "/api")
    with TestClient(app):
        assert events == ["webp-check", "lifespan-start"]
    assert events == ["webp-check", "lifespan-start", "lifespan-stop"]


def test_flag_off_mount_registers_no_startup_hook(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    events: list[str] = []
    monkeypatch.setattr(photo_store, "check_webp_support", lambda: events.append("webp-check"))
    app = _bare_app(events)
    original = app.router.lifespan_context
    mount(app, _config(settings, enabled=False), "/api")
    assert app.router.lifespan_context is original
    with TestClient(app):
        pass
    assert events == ["lifespan-start", "lifespan-stop"]


def _start_real_app(settings, flag: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []
    monkeypatch.setattr(photo_store, "check_webp_support", lambda: events.append("webp-check"))
    # `create_app` also sets the process-wide 2FA flag; the autouse fixture restores it.
    app = app_main.create_app(settings.model_copy(update={"service_tickets_enabled": flag}))
    assert app.router.on_startup == []
    with TestClient(app):  # runs the real Hours lifespan: no network call, scheduler disabled
        pass
    return events


def test_flag_off_application_runs_no_ticket_startup_hook(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _start_real_app(settings, "", monkeypatch) == []


def test_flag_on_application_runs_the_webp_check_once(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _start_real_app(settings, "true", monkeypatch) == ["webp-check"]


def test_add_startup_check_passes_the_lifespan_state_through() -> None:
    events: list[str] = []
    app = _bare_app(events)
    add_startup_check(app, lambda: events.append("check"))
    with TestClient(app):
        assert events == ["check", "lifespan-start"]
