"""Shared Redis client.

One pooled client for the process, built lazily on first use. `redis.Redis.from_url` does not open a
connection until a command runs, so building the client at import time would be safe — but it is
cached here rather than at import so the URL is read through `get_settings`, which the test suite
overrides, rather than frozen at module load.

The client is created with short socket timeouts so a Redis outage surfaces as a fast error the
caller can decide to ignore (the rate limiter fails open) rather than a hang that ties up a worker.
"""

from __future__ import annotations

from functools import lru_cache

import redis

from app.core.config import get_settings

#: Socket timeout for rate-limit commands. Short: the limiter must not add measurable latency to a
#: request, and a slow Redis should fail open quickly rather than block the request behind it.
_SOCKET_TIMEOUT_SECONDS = 0.25


@lru_cache
def get_redis_client() -> redis.Redis:
    """Process-wide Redis client. Cached so the connection pool is shared across requests."""
    settings = get_settings()
    return redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
        decode_responses=True,
    )
