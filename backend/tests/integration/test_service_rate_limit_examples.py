"""Example tests for the Lua rate limiter (task 17.3; R22.1 to R22.3, R22.5, R22.9).

A real Redis, an injected clock. The limiter takes `now` from the caller, so a test steps the clock by
setting a number and never sleeps.

Needs `TEST_REDIS_URL` (the tests `conftest.py` forces `REDIS_URL` to a closed port, so the real URL
has to come in under another name). Without it, or without a reachable server, the tests skip.

Only keys under `svc:rl:` are removed, before and after each test. Nothing else in the database is
touched, so a shared Redis db is safe.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Iterator

import pytest
import redis

from app.service_tickets import rate_limit
from app.service_tickets.rate_limit import (
    KEY_ROOT,
    release,
    request_key,
    retry_after_seconds,
    signin_key,
    submit_key,
    take,
)

MAX = 10
WINDOW_MS = 60_000
T0 = 1_700_000_000_000  # an arbitrary start of the injected clock, in milliseconds


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


@pytest.fixture
def key() -> str:
    return signin_key(f"test-{uuid.uuid4().hex[:8]}")


def _fill(key: str, now_ms: int, count: int = MAX) -> None:
    for number in range(1, count + 1):
        assert take(key, MAX, WINDOW_MS, now_ms).allowed, f"request {number} was rejected"


def test_tenth_request_is_allowed_and_eleventh_is_rejected_without_counting(
    redis_client: redis.Redis, key: str
) -> None:
    for number in range(1, MAX + 1):
        result = take(key, MAX, WINDOW_MS, T0)
        assert result.allowed, f"request {number}"
        assert result.retry_after == 0

    for _ in range(3):  # every further request in the window is rejected, and none is counted
        result = take(key, MAX, WINDOW_MS, T0)
        assert not result.allowed
        assert result.retry_after == 60
        assert redis_client.hget(key, "c") == str(MAX)


def test_window_starts_at_the_first_counted_request(redis_client: redis.Redis, key: str) -> None:
    assert take(key, MAX, WINDOW_MS, T0).allowed
    _fill(key, T0 + 20_000, MAX - 1)  # the later requests do not move the start

    assert redis_client.hget(key, "s") == str(T0)
    rejected = take(key, MAX, WINDOW_MS, T0 + 20_000)
    assert (rejected.allowed, rejected.retry_after) == (False, 40)
    # the window opened at T0, so it ends at T0 + 60 s no matter when the 10th request came
    assert take(key, MAX, WINDOW_MS, T0 + WINDOW_MS).allowed


def test_retry_after_shrinks_as_time_passes_and_rounds_up(redis_client: redis.Redis, key: str) -> None:
    _fill(key, T0)

    seen = []
    for elapsed_ms in (0, 1, 10_000, 10_001, 45_500, 59_000, 59_999):
        result = take(key, MAX, WINDOW_MS, T0 + elapsed_ms)
        assert not result.allowed
        seen.append(result.retry_after)

    assert seen == [60, 60, 50, 50, 15, 1, 1]
    assert seen == sorted(seen, reverse=True)


def test_request_after_retry_after_seconds_passes(redis_client: redis.Redis, key: str) -> None:
    _fill(key, T0)
    sent_at = T0 + 12_300

    rejected = take(key, MAX, WINDOW_MS, sent_at)
    assert not rejected.allowed
    assert rejected.retry_after == 48  # 47.7 s left, rounded up

    assert take(key, MAX, WINDOW_MS, sent_at + rejected.retry_after * 1000).allowed
    # that request opened a new window, and it counts as the first of it
    assert redis_client.hget(key, "c") == "1"
    assert redis_client.hget(key, "s") == str(sent_at + 48_000)


def test_window_key_expires_with_the_window(redis_client: redis.Redis, key: str) -> None:
    assert take(key, MAX, WINDOW_MS, T0).allowed
    ttl_ms = redis_client.pttl(key)
    assert 0 < ttl_ms <= WINDOW_MS


def test_retry_after_is_clamped_to_one_through_window_length() -> None:
    assert retry_after_seconds(0, WINDOW_MS) == 1
    assert retry_after_seconds(1, WINDOW_MS) == 1
    assert retry_after_seconds(60_000, WINDOW_MS) == 60
    assert retry_after_seconds(90_000, WINDOW_MS) == 60
    assert retry_after_seconds(3_599_001, 3_600_000) == 3600


def test_release_frees_one_slot(redis_client: redis.Redis, key: str) -> None:
    _fill(key, T0)
    assert not take(key, MAX, WINDOW_MS, T0 + 1_000).allowed

    release(key)

    assert redis_client.hget(key, "c") == str(MAX - 1)
    assert take(key, MAX, WINDOW_MS, T0 + 2_000).allowed
    assert not take(key, MAX, WINDOW_MS, T0 + 3_000).allowed  # the freed slot is used again


def test_release_keeps_the_window_it_was_taken_in(redis_client: redis.Redis, key: str) -> None:
    _fill(key, T0, 3)
    ttl_before = redis_client.pttl(key)

    release(key)

    assert redis_client.hget(key, "c") == "2"
    assert redis_client.hget(key, "s") == str(T0)
    assert 0 < redis_client.pttl(key) <= ttl_before


def test_release_deletes_the_key_when_the_count_reaches_zero(redis_client: redis.Redis, key: str) -> None:
    _fill(key, T0, 2)

    release(key)
    assert redis_client.exists(key) == 1
    release(key)
    assert redis_client.exists(key) == 0

    release(key)  # nothing to give back: no error, and no key is created
    assert redis_client.exists(key) == 0

    # a later request opens a fresh window
    assert take(key, MAX, WINDOW_MS, T0 + 5_000).allowed
    assert redis_client.hget(key, "s") == str(T0 + 5_000)


def test_groups_are_independent_and_keyed_by_customer_or_address(redis_client: redis.Redis) -> None:
    customer = uuid.uuid4()
    keys = [signin_key("203.0.113.7"), request_key(customer), submit_key(customer)]
    assert keys == [
        "svc:rl:signin:203.0.113.7",
        f"svc:rl:req:{customer}",
        f"svc:rl:submit:{customer}",
    ]

    assert take(keys[2], 1, 3_600_000, T0).allowed
    assert not take(keys[2], 1, 3_600_000, T0).allowed
    assert take(keys[0], 1, WINDOW_MS, T0).allowed
    assert take(keys[1], 1, WINDOW_MS, T0).allowed


def test_script_is_loaded_when_the_server_does_not_know_it(redis_client: redis.Redis) -> None:
    # A script nobody has run has no cached copy: EVALSHA answers NOSCRIPT and the call falls back to EVAL.
    script = rate_limit._Script.of(f"return {{{uuid.uuid4().int % 1_000_000}}}")
    assert not redis_client.script_exists(script.sha)[0]

    first = script.run(redis_client, [], [])
    assert redis_client.script_exists(script.sha)[0]  # EVAL cached it
    assert script.run(redis_client, [], []) == first  # now served by EVALSHA


def test_redis_unreachable_allows_the_request_and_logs_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # An earlier Alembic run in this process (env.py `fileConfig`) may have disabled this logger.
    monkeypatch.setattr(rate_limit.logger, "disabled", False)
    monkeypatch.setattr(rate_limit.logger, "propagate", True)
    down = redis.Redis.from_url(
        "redis://127.0.0.1:1/0", socket_connect_timeout=0.25, socket_timeout=0.25, decode_responses=True
    )
    monkeypatch.setattr(rate_limit, "get_redis_client", lambda: down)
    address = "198.51.100.23"

    with caplog.at_level(logging.WARNING, logger=rate_limit.logger.name):
        results = [take(signin_key(address), MAX, WINDOW_MS, T0) for _ in range(MAX + 5)]
        release(signin_key(address))  # must not raise either

    assert all(result.allowed and result.retry_after == 0 for result in results)
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == MAX + 5 + 1
    text = " ".join(record.getMessage() for record in warnings)
    assert "group=signin" in text
    assert address not in text
    assert KEY_ROOT not in text


def test_client_that_cannot_be_built_also_allows_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse() -> redis.Redis:
        raise redis.ConnectionError("down")

    monkeypatch.setattr(rate_limit, "get_redis_client", refuse)
    assert take(request_key(uuid.uuid4()), MAX, WINDOW_MS, T0).allowed
