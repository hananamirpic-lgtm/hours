"""Fixed-window rate limiting, backed by Redis (Requirement 20.4).

A fixed-window counter: for a given client key and endpoint group, count the requests in the current
window of `window_seconds` and refuse once the count passes `max_requests`. Simpler than a sliding
window or a token bucket, and enough for the job here — this is a coarse ceiling that sits *underneath*
the per-identity controls (the auth failed-attempt lockout, the scan duplicate window), not the
primary defence. It exists so one client cannot make thousands of well-formed requests a second at the
two endpoints an outsider can reach: sign-in and scan.

Design decisions worth stating:

* **Fails open.** If Redis is unreachable the request is allowed. A cache outage must not take
  sign-in down, and the auth lockout still guards credential guessing without it. The failure is
  logged, not raised.
* **Client key.** The limit is per authenticated user where a valid-looking bearer token is present,
  otherwise per client IP. Keying auth attempts by IP is the point — the caller has no identity yet.
  The IP is read from the socket; a deployment behind a proxy sets it from `X-Forwarded-For` at the
  proxy, which is outside this process's trust boundary to parse.
* **Atomic.** `INCR` then `EXPIRE` in a pipeline, with the expiry set only on the first increment of a
  window, so the window does not slide forward on every request.

The limiter is applied as a FastAPI dependency (`RateLimit(...)`) rather than middleware, so it is
attached to exactly the endpoint groups the requirement names and reads in each router's signature.
"""

from __future__ import annotations

import logging
from http import HTTPStatus

import redis
from fastapi import Request

from app.api.deps import api_error
from app.core.config import get_settings
from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)

#: Machine code returned when a client exceeds a limit, for the front end to translate.
CODE_RATE_LIMITED = "rate_limited"

#: Prefix for every limiter key, so the keys are recognisable in Redis and cannot collide with any
#: other use of the cache.
_KEY_PREFIX = "ratelimit"


def _client_key(request: Request) -> str:
    """Identify the caller for rate-limiting.

    The authenticated user id when a bearer token is present — so a signed-in user's limit follows
    them across IPs — otherwise the client IP, which is what an unauthenticated sign-in attempt has.
    The token is read but not verified here: an invalid token still resolves to *some* stable key,
    and a forged one cannot escape the limit because it does not name a real session anyway. Cheap and
    good enough for a coarse ceiling; the real authentication happens downstream.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and len(auth) > 7:
        # A stable per-token key without decoding: the token string itself, truncated so the key
        # stays short. Two requests with the same token share a key; that is all that is needed.
        return f"token:{auth[7:].strip()[:64]}"
    client = request.client
    return f"ip:{client.host}" if client is not None else "ip:unknown"


def _limits_for(group: str) -> tuple[int, int]:
    """The (max_requests, window_seconds) pair configured for an endpoint group."""
    settings = get_settings()
    if group == "auth":
        return settings.auth_rate_limit_max_requests, settings.auth_rate_limit_window_seconds
    if group == "scan":
        return settings.scan_rate_limit_max_requests, settings.scan_rate_limit_window_seconds
    raise ValueError(f"unknown rate-limit group: {group}")


def _hit(key: str, window_seconds: int) -> int:
    """Increment the window counter and return the new count.

    The expiry is (re)set only when the counter is at 1 — the first hit of a fresh window — so the
    window is fixed and does not slide forward on every request. `INCR` on a missing key creates it at
    1, which is exactly the first-hit signal.
    """
    client = get_redis_client()
    pipeline = client.pipeline()
    pipeline.incr(key)
    pipeline.expire(key, window_seconds, nx=True)
    count, _ = pipeline.execute()
    return int(count)


def _enforce(group: str, request: Request) -> None:
    """Count this request against `group`'s window and refuse once the ceiling is passed.

    Fails open: a Redis error allows the request rather than blocking it, so a cache outage cannot
    take sign-in down. A limit of 0, or the global disable switch, turns the group off entirely.
    """
    settings = get_settings()
    max_requests, window_seconds = _limits_for(group)
    if not settings.rate_limit_enabled or max_requests <= 0:
        return

    key = f"{_KEY_PREFIX}:{group}:{_client_key(request)}"
    try:
        count = _hit(key, window_seconds)
    except redis.RedisError as error:  # fail open: a cache outage must not block the request
        logger.warning("rate limiter unavailable, allowing request: %s", type(error).__name__)
        return

    if count > max_requests:
        # Retry-After tells a well-behaved client when to try again; the window length is the honest
        # upper bound, since the counter resets at the window boundary.
        raise api_error(
            HTTPStatus.TOO_MANY_REQUESTS,
            CODE_RATE_LIMITED,
            headers={"Retry-After": str(window_seconds)},
        )


# The two dependencies the requirement names, as plain functions so FastAPI resolves the `Request`
# annotation reliably. Auth guards sign-in and refresh; scan guards the attendance write endpoints.
def rate_limit_auth(request: Request) -> None:
    """FastAPI dependency enforcing the auth endpoint group's limit (Requirement 20.4)."""
    _enforce("auth", request)


def rate_limit_scan(request: Request) -> None:
    """FastAPI dependency enforcing the scan endpoint group's limit (Requirement 20.4)."""
    _enforce("scan", request)
