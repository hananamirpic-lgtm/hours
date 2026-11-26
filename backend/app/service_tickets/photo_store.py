"""Ticket photo storage and the decode semaphore (R11.8, R20.11, R24.5; design.md, "Photo pipeline").

`photo.py` is pure. This module is the only code of the package that talks to the object store.

The store
---------
`PhotoStore` has three operations, `put`, `get` and `delete`. It has no list operation and creates no
presigned URL, so nothing outside the `service-tickets/` prefix can be read, written or deleted, and the
photo is never reachable except through the API's delivery route (R20.11).

* **Its own client.** The ticket store builds a separate boto3 client over the same endpoint,
  credentials, region, bucket and signature version as the Hours client, but with its own timeouts:
  3 s connect, 10 s read and one retry (finding F19: the shared Hours client has 2 s timeouts and no
  retries, tuned for readiness probes). It never calls `app.repositories.health` or `app.core.storage`
  and shares no object with them. The client is built on first use, never at import time, and a
  process builds one (clients are thread safe).
* **Retry.** `retries={"max_attempts": 1, "mode": "legacy"}`. In legacy mode `max_attempts` counts
  retries (Hours measured this: standard mode with `max_attempts=1` still made two attempts), so this
  means one try plus one retry.
* **No ACL, no encryption arguments.** The Hours store sets neither `ACL` nor
  `ServerSideEncryption` on any call, so this store does not either; the bucket stays private by its
  own policy. If Hours ever adds server-side encryption, mirror it here.
* **Keys.** `new_key()` gives `service-tickets/{uuid4().hex}.jpg`. No part of the client's file name or
  of any ticket data is in it (R11.8). `new_logo_key()` gives `service-tickets/branding/{uuid4().hex}.png`
  for the one company logo (R28.6). Every method checks the key first (`check_key`): a non-string, an
  empty key, a key outside the prefix, a key with an empty, `.` or `..` path segment, a backslash, a
  control character, or any shape other than those two raises `ValueError`, before any network call.
* **Errors.** `PhotoNotFound` is a missing object (the router answers 404 `ticket_not_found` and logs an
  error with the ticket number). `PhotoStorageUnavailable` is any other failure of `put` or `get` (the
  router answers 503 `photo_storage_unavailable`). `delete` is best effort: the router calls it only
  to undo a `put` after a failed insert or commit, and a failure there must not hide the original error,
  so `delete` logs it and returns `False`. A photo left behind is found by the orphan sweep (task 16.6).
  A bad key still raises `ValueError`, because that is a programming error, not a store failure.

The decode semaphore
--------------------
`process_photo_limited` runs `process_photo` while holding one of three process-wide slots, so at most
three images are decoded at the same time (worst case about 600 MB, R24.5). A request waits up to
`DECODE_WAIT_SECONDS` (30 s) for a slot, then raises the package's 503 `service_unavailable` error. The
slot is released in a `finally`, so a decode error, an out-of-memory error or a cancelled thread never
leaks it. The function is synchronous and CPU bound; the router calls it in a worker thread.

The WebP check
--------------
`check_webp_support` logs an error when Pillow was built without WebP and never raises. `mount` runs it
once at start, only with the flag on (see `mount.py`).
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from functools import lru_cache
from http import HTTPStatus

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings
from app.service_tickets.api.errors import service_error
from app.service_tickets.error_codes import (
    CODE_PHOTO_STORAGE_UNAVAILABLE,
    CODE_SERVICE_UNAVAILABLE,
    CODE_TICKET_NOT_FOUND,
)
from app.service_tickets.logo import CONTENT_TYPE as LOGO_CONTENT_TYPE
from app.service_tickets.logo import ProcessedLogo, process_logo
from app.service_tickets.photo import CONTENT_TYPE, ProcessedPhoto, process_photo

logger = logging.getLogger(__name__)

#: Every key of this store starts with this prefix, and nothing outside it can be touched (R20.11).
KEY_PREFIX = "service-tickets/"

#: The sub-prefix of the one company logo (R28.6): `service-tickets/branding/<uuid4 hex>.png`.
LOGO_KEY_PREFIX = f"{KEY_PREFIX}branding/"

#: The two shapes of a key. A ticket photo is `<name>.jpg` directly under the prefix; the name is
#: lowercase letters and digits (`new_key` makes 32 hex characters). A logo is 32 lowercase hex
#: characters and `.png` under `branding/`. Nothing else is a key of this store.
_PHOTO_KEY_SHAPE = re.compile(re.escape(KEY_PREFIX) + r"[0-9a-z]{1,64}\.jpg")
_LOGO_KEY_SHAPE = re.compile(re.escape(LOGO_KEY_PREFIX) + r"[0-9a-f]{32}\.png")

#: Client timeouts and retries (finding F19).
CONNECT_TIMEOUT_SECONDS = 3
READ_TIMEOUT_SECONDS = 10
RETRIES = 1

#: How many photos are decoded at the same time in one process (R24.5).
MAX_CONCURRENT_DECODES = 3

#: How long a request waits for a decode slot before it answers 503 `service_unavailable`.
DECODE_WAIT_SECONDS = 30.0

#: Error codes S3-compatible stores use for a missing object.
_MISSING_OBJECT_CODES = frozenset({"NoSuchKey", "404", "NotFound"})

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


# --------------------------------------------------------------------------- errors


class PhotoStoreError(Exception):
    """Base of the store's failures. `code` is the API error code the router answers with."""

    code: str = CODE_PHOTO_STORAGE_UNAVAILABLE


class PhotoNotFound(PhotoStoreError):
    """No object exists at the key. The router answers 404 and logs an error with the ticket number."""

    code = CODE_TICKET_NOT_FOUND


class PhotoStorageUnavailable(PhotoStoreError):
    """The object store could not be reached or refused the call. The router answers 503."""

    code = CODE_PHOTO_STORAGE_UNAVAILABLE


# --------------------------------------------------------------------------- keys


def new_key() -> str:
    """A new unique photo key: `service-tickets/` plus 32 hex characters plus `.jpg` (R11.8)."""
    return f"{KEY_PREFIX}{uuid.uuid4().hex}.jpg"


def new_logo_key() -> str:
    """A new unique logo key: `service-tickets/branding/` plus 32 hex characters plus `.png` (R28.6)."""
    return f"{LOGO_KEY_PREFIX}{uuid.uuid4().hex}.png"


def is_logo_key(key: object) -> bool:
    """True only for a key of the logo shape: `service-tickets/branding/<32 lowercase hex>.png`.

    A ticket photo key is a valid key of the store but not a logo key. The anonymous logo route calls this
    before `get`, so a branding row that points at anything else is never read (R28.8).
    """
    return isinstance(key, str) and _LOGO_KEY_SHAPE.fullmatch(key) is not None


def check_key(key: object) -> str:
    """Return `key` if it is a safe key of this store, else raise `ValueError`.

    Rejects: anything that is not a `str`, the empty string, a key that does not start with
    `service-tickets/`, a key with nothing after the prefix, an empty, `.` or `..` path segment
    (`service-tickets/../x`, `service-tickets//x`), a backslash and control characters. What is left must
    have one of the two shapes of the store: `service-tickets/<lowercase letters and digits>.jpg` (a
    ticket photo) or `service-tickets/branding/<32 lowercase hex characters>.png` (the logo). Uppercase,
    another extension, another sub-folder or another depth is a `ValueError`.
    """
    if not isinstance(key, str):
        raise ValueError("photo key must be a string")
    if not key.startswith(KEY_PREFIX):
        raise ValueError(f"photo key must start with {KEY_PREFIX!r}")
    rest = key[len(KEY_PREFIX) :]
    if not rest:
        raise ValueError("photo key has nothing after the prefix")
    if "\\" in key or _CONTROL_CHARACTERS.search(key):
        raise ValueError("photo key has a forbidden character")
    if any(segment in ("", ".", "..") for segment in rest.split("/")):
        raise ValueError("photo key has an empty or relative path segment")
    if not (_PHOTO_KEY_SHAPE.fullmatch(key) or _LOGO_KEY_SHAPE.fullmatch(key)):
        raise ValueError("photo key does not have the shape of a photo or a logo key")
    return key


# --------------------------------------------------------------------------- the store


def build_client():  # noqa: ANN201 - botocore builds the client type at runtime
    """A new boto3 S3 client for ticket photos. Not shared with the Hours client or its helpers."""
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        region_name=settings.s3_region,
        config=BotoConfig(
            signature_version="s3v4",
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            read_timeout=READ_TIMEOUT_SECONDS,
            retries={"max_attempts": RETRIES, "mode": "legacy"},
        ),
    )


class PhotoStore:
    """Put, get and delete ticket photos under `service-tickets/`. Nothing else.

    `client` and `bucket` can be given (tests pass a fake client); by default the client is built on
    first use by `build_client` and the bucket is `s3_bucket_documents`, the Hours bucket.
    """

    def __init__(self, client=None, *, bucket: str | None = None) -> None:  # noqa: ANN001
        self._client = client
        self._bucket = bucket
        self._lock = threading.Lock()

    @property
    def bucket(self) -> str:
        if self._bucket is None:
            self._bucket = get_settings().s3_bucket_documents
        return self._bucket

    def _get_client(self):  # noqa: ANN202
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = build_client()
        return self._client

    def put(self, key: str, data: bytes) -> None:
        """Store the bytes under `key` with no ACL. A photo key gets `ContentType: image/jpeg`, a logo key
        (`.png`) gets `image/png`.

        Raises `ValueError` for a bad key, `TypeError` for non-bytes, `PhotoStorageUnavailable` when
        the store fails.
        """
        check_key(key)
        if not isinstance(data, bytes | bytearray | memoryview):
            raise TypeError("data must be bytes")
        content_type = LOGO_CONTENT_TYPE if key.endswith(".png") else CONTENT_TYPE
        try:
            self._get_client().put_object(
                Bucket=self.bucket, Key=key, Body=bytes(data), ContentType=content_type
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("photo store put failed: %s", type(exc).__name__)
            raise PhotoStorageUnavailable from exc

    def get(self, key: str) -> bytes:
        """Return the bytes stored under `key`.

        Raises `ValueError` for a bad key, `PhotoNotFound` when the object does not exist and
        `PhotoStorageUnavailable` for any other store failure.
        """
        check_key(key)
        try:
            response = self._get_client().get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                return body.read()
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        except ClientError as exc:
            if _error_code(exc) in _MISSING_OBJECT_CODES:
                raise PhotoNotFound from exc
            logger.error("photo store get failed: %s", type(exc).__name__)
            raise PhotoStorageUnavailable from exc
        except BotoCoreError as exc:
            logger.error("photo store get failed: %s", type(exc).__name__)
            raise PhotoStorageUnavailable from exc

    def delete(self, key: str) -> bool:
        """Best-effort removal. Returns True when the call succeeded, False when the store failed.

        A store failure is logged, not raised: this runs while undoing a failed ticket insert, and
        the original error must reach the caller. The orphan sweep removes what is left. A bad key
        raises `ValueError`.
        """
        check_key(key)
        try:
            self._get_client().delete_object(Bucket=self.bucket, Key=key)
        except (ClientError, BotoCoreError) as exc:
            logger.error("photo store delete failed: %s", type(exc).__name__)
            return False
        return True


def _error_code(exc: ClientError) -> str:
    response = exc.response if isinstance(exc.response, dict) else {}
    error = response.get("Error") or {}
    return str(error.get("Code", ""))


@lru_cache(maxsize=1)
def get_photo_store() -> PhotoStore:
    """The process-wide store, built on first call. A FastAPI dependency; tests override it.

    Building a `PhotoStore` makes no network call and builds no boto3 client (that waits for the first
    `put`, `get` or `delete`), so this is cheap and safe to call from a request.
    `get_photo_store.cache_clear()` drops the instance.
    """
    return PhotoStore()


# --------------------------------------------------------------------------- the decode semaphore

#: The process-wide decode slots (R24.5). Module level so every request thread shares them.
_DECODE_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_DECODES)


def process_photo_limited(data: bytes, *, wait_seconds: float = DECODE_WAIT_SECONDS) -> ProcessedPhoto:
    """`process_photo(data)` while holding a decode slot.

    Waits up to `wait_seconds` for a slot, then raises the 503 `service_unavailable` error (an
    `HTTPException` in the package envelope, the same one the route wrapper gives for a database
    failure). The slot is released on every path, including an exception from `process_photo`.
    `PhotoError` from `process_photo` passes through unchanged for the router to map to 422.
    """
    if not _DECODE_SLOTS.acquire(timeout=max(wait_seconds, 0.0)):
        raise service_error(HTTPStatus.SERVICE_UNAVAILABLE, CODE_SERVICE_UNAVAILABLE)
    try:
        return process_photo(data)
    finally:
        _DECODE_SLOTS.release()


def process_logo_limited(data: bytes, *, wait_seconds: float = DECODE_WAIT_SECONDS) -> ProcessedLogo:
    """`process_logo(data)` while holding a decode slot of the same pool as the photos (R24.5).

    Same wait, same 503 `service_unavailable` and same release on every path as `process_photo_limited`.
    `LogoError` from `process_logo` passes through unchanged for the router to map to 422 or 413.
    """
    if not _DECODE_SLOTS.acquire(timeout=max(wait_seconds, 0.0)):
        raise service_error(HTTPStatus.SERVICE_UNAVAILABLE, CODE_SERVICE_UNAVAILABLE)
    try:
        return process_logo(data)
    finally:
        _DECODE_SLOTS.release()


# --------------------------------------------------------------------------- start-up check


def check_webp_support() -> bool:
    """Log an error when Pillow has no WebP support. Never raises. Returns whether WebP is supported.

    Without WebP every WebP upload would be rejected as `invalid_photo_type`, which would look like a
    customer mistake, so the operator is told at start (design.md, "Photo pipeline").
    """
    try:
        from PIL import features

        supported = bool(features.check("webp"))
    except Exception:
        logger.exception("could not determine Pillow WebP support; WebP photos may be rejected")
        return False
    if not supported:
        logger.error("Pillow has no WebP support; WebP photo uploads will be rejected as invalid_photo_type")
    return supported
