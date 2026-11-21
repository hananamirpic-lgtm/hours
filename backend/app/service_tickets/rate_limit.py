"""Atomic fixed-window rate limiter for the service ticket API (R22; design.md, "Abuse protection").

Two Lua scripts run inside Redis, so the check and the increment are one atomic step:

* ``take`` opens a window at the first counted request, counts a request only when it is allowed, and
  never counts a rejected one (R22.1 to R22.3). The caller passes ``now`` in milliseconds, so tests
  drive the clock and nothing here reads time.
* ``release`` gives one slot back and deletes the key when the count reaches zero. The submission
  window uses it so that only a submission that created a ticket stays counted (R22.2).

A Redis error allows the request and logs a warning (R22.5). The warning names the key group and the
error class only: a key carries a customer id or an address key, and none of that belongs in a log.

The existing ``app/core/rate_limit.py`` is a different limiter (increment-then-check, full-window
``Retry-After``) and is not touched (F7, R20.6).
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass

import redis

from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)

# Key groups. A key is `<prefix><identity>`, where the identity is an address key (sign-in) or a
# customer id (the other two). Never put a phone number or a token in a key.
KEY_ROOT = "svc:rl:"
SIGNIN_PREFIX = "svc:rl:signin:"
REQUEST_PREFIX = "svc:rl:req:"
SUBMIT_PREFIX = "svc:rl:submit:"


def signin_key(address_key: str) -> str:
    """Key of the per-address sign-in window (R22.1)."""
    return f"{SIGNIN_PREFIX}{address_key}"


def request_key(customer_id: object) -> str:
    """Key of the per-customer request window (R22.3)."""
    return f"{REQUEST_PREFIX}{customer_id}"


def submit_key(customer_id: object) -> str:
    """Key of the per-customer ticket-submission window (R22.2)."""
    return f"{SUBMIT_PREFIX}{customer_id}"


# KEYS[1] = key; ARGV[1] = max, ARGV[2] = window ms, ARGV[3] = now ms (the caller's clock).
# Returns {allowed, ms_left}. The window is fixed: `s` is written only when no window is open.
_TAKE_LUA = """
local start = tonumber(redis.call('HGET', KEYS[1], 's') or '0')
local count = tonumber(redis.call('HGET', KEYS[1], 'c') or '0')
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
if start == 0 or now - start >= window then
  start = now
  count = 0
end
local left = start + window - now
if count >= tonumber(ARGV[1]) then
  return {0, left}
end
redis.call('HSET', KEYS[1], 's', start, 'c', count + 1)
redis.call('PEXPIRE', KEYS[1], left)
return {1, left}
"""

# KEYS[1] = key. Gives one slot back; deletes the key when the count reaches zero. The TTL of a key
# that stays is unchanged, so the window still ends when it was going to. Returns the count left.
_RELEASE_LUA = """
local count = tonumber(redis.call('HGET', KEYS[1], 'c') or '0')
if count <= 1 then
  redis.call('DEL', KEYS[1])
  return 0
end
redis.call('HSET', KEYS[1], 'c', count - 1)
return count - 1
"""


@dataclass(frozen=True)
class _Script:
    """A Lua script with its SHA1, run by EVALSHA and loaded with EVAL when the server lacks it."""

    source: str
    sha: str

    @classmethod
    def of(cls, source: str) -> _Script:
        return cls(source=source, sha=hashlib.sha1(source.encode("utf-8")).hexdigest())  # noqa: S324

    def run(self, client: redis.Redis, keys: Sequence[str], args: Sequence[int]) -> list[int]:
        try:
            result = client.evalsha(self.sha, len(keys), *keys, *args)
        except redis.exceptions.NoScriptError:
            # EVAL runs the script and caches it on the server, so the next call can use EVALSHA.
            result = client.eval(self.source, len(keys), *keys, *args)
        return result


_TAKE = _Script.of(_TAKE_LUA)
_RELEASE = _Script.of(_RELEASE_LUA)


@dataclass(frozen=True)
class TakeResult:
    """Outcome of `take`. `retry_after` is whole seconds for a `Retry-After` header; 0 when allowed."""

    allowed: bool
    retry_after: int = 0


def retry_after_seconds(ms_left: int, window_ms: int) -> int:
    """clamp(ceil(ms_left / 1000), 1, window_seconds) (R22.9)."""
    window_seconds = max(1, math.ceil(window_ms / 1000))
    return min(max(math.ceil(ms_left / 1000), 1), window_seconds)


def _group(key: str) -> str:
    """The key group for a log line, never the identity part of the key."""
    if key.startswith(KEY_ROOT):
        return key[len(KEY_ROOT) :].split(":", 1)[0] or "unknown"
    return "unknown"


def take(key: str, max: int, window_ms: int, now_ms: int) -> TakeResult:  # noqa: A002 - the API names it `max`
    """Count one request against the window of `key`, unless the window is full.

    Allowed: the request is counted, and the first counted request of a window opens it. Rejected:
    nothing is counted and `retry_after` is the time left in the window, rounded up to whole seconds
    and kept between 1 and the window length. If Redis fails, the request is allowed.
    """
    try:
        allowed, ms_left = _TAKE.run(get_redis_client(), [key], [max, window_ms, now_ms])
    except redis.RedisError as error:
        logger.warning(
            "service rate limiter unavailable, allowing request: group=%s error=%s",
            _group(key),
            type(error).__name__,
        )
        return TakeResult(allowed=True)
    if int(allowed) == 1:
        return TakeResult(allowed=True)
    return TakeResult(allowed=False, retry_after=retry_after_seconds(int(ms_left), window_ms))


def release(key: str) -> None:
    """Give back one slot taken by `take`; delete the key when the count reaches zero.

    A Redis error is logged and ignored: the slot then lapses with the window.
    """
    try:
        _RELEASE.run(get_redis_client(), [key], [])
    except redis.RedisError as error:
        logger.warning(
            "service rate limiter unavailable, slot not released: group=%s error=%s",
            _group(key),
            type(error).__name__,
        )
