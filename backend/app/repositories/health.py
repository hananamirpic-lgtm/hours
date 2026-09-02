"""Dependency probes.

Each probe answers one question: can the application reach this dependency right now. Failures are
reported as the exception's type name only — never its message — because driver errors routinely
embed connection strings, and a health endpoint is the last place a credential should surface.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import boto3
import redis
from botocore.config import Config as BotoConfig
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.db.session import get_engine

ProbeStatus = Literal["up", "down"]

DATABASE = "database"
REDIS = "redis"
OBJECT_STORAGE = "object_storage"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of a single dependency probe."""

    name: str
    status: ProbeStatus
    latency_ms: float
    detail: str | None = None

    @property
    def is_up(self) -> bool:
        return self.status == "up"


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _failure(name: str, started: float, error: BaseException) -> ProbeResult:
    return ProbeResult(name=name, status="down", latency_ms=_elapsed_ms(started), detail=type(error).__name__)


def check_database(settings: Settings) -> ProbeResult:
    """Round-trip the simplest possible statement through the connection pool."""
    started = time.perf_counter()
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:  # noqa: BLE001 - any failure means the dependency is unusable
        return _failure(DATABASE, started, error)
    return ProbeResult(name=DATABASE, status="up", latency_ms=_elapsed_ms(started))


def check_redis(settings: Settings) -> ProbeResult:
    started = time.perf_counter()
    client = None
    try:
        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=settings.probe_timeout_seconds,
            socket_timeout=settings.probe_timeout_seconds,
        )
        client.ping()
    except Exception as error:  # noqa: BLE001
        return _failure(REDIS, started, error)
    finally:
        if client is not None:
            client.close()
    return ProbeResult(name=REDIS, status="up", latency_ms=_elapsed_ms(started))


@lru_cache
def get_object_storage_client():  # noqa: ANN201 - botocore builds the client type at runtime
    """
    Cached S3 client.

    Building one costs a noticeable amount of time because botocore loads its service model, and
    paying that on every readiness call was enough to push the probe past its own deadline. Clients
    are safe to share across threads; it is the higher-level resource objects that are not.
    """
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        region_name=settings.s3_region,
        config=BotoConfig(
            signature_version="s3v4",
            connect_timeout=max(int(settings.probe_timeout_seconds), 1),
            read_timeout=max(int(settings.probe_timeout_seconds), 1),
            # Legacy mode counts retries rather than total attempts, so zero means exactly one
            # try. Measured, because standard mode with max_attempts=1 still made two attempts
            # and so took twice the connect timeout — long enough to trip the readiness deadline
            # and report "timeout" instead of the real cause.
            retries={"max_attempts": 0},
        ),
    )


@lru_cache
def get_object_storage_presign_client():  # noqa: ANN201 - botocore builds the client type at runtime
    """Cached S3 client used only to *sign* presigned URLs, built against the public endpoint.

    A presigned URL embeds the endpoint host it was signed for, and it is followed by the browser —
    which, behind Docker, cannot resolve the internal service name the API talks to. So the signing
    client points at `s3_presign_endpoint_url` (the public host when configured, else the same
    internal endpoint). Only URL generation uses this client; every real S3 operation the API
    performs — head_bucket, get_object, put_object — stays on the internal client above. Same
    credentials and signing version, so the signature the public host validates is identical.
    """
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_presign_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        region_name=settings.s3_region,
        config=BotoConfig(signature_version="s3v4"),
    )


def check_object_storage(settings: Settings) -> ProbeResult:
    """Confirm the documents bucket is reachable and the credentials are accepted."""
    started = time.perf_counter()
    try:
        get_object_storage_client().head_bucket(Bucket=settings.s3_bucket_documents)
    except Exception as error:  # noqa: BLE001
        return _failure(OBJECT_STORAGE, started, error)
    return ProbeResult(name=OBJECT_STORAGE, status="up", latency_ms=_elapsed_ms(started))
