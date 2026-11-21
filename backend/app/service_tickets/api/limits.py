"""Rate-limit wiring of the service ticket API (R22; design.md, "Request check order", "Abuse protection").

`rate_limit.py` is the limiter (one Lua script per call, `now` supplied by the caller). This module is the
glue between it and the HTTP layer: which key, which limit, which answer, and where the clock comes from.
Nothing here reads a database, and nothing here reads the time directly: every `now` comes from the
`Clock` the request was given (`get_clock`), which a test overrides.

The three limits and where each one sits in the check order
-----------------------------------------------------------
1. **Sign-in, per client address** (`check_signin_address`, run by `deps.signin_address_limit`). It is the
   first thing a sign-in does that is not parsing JSON: it runs as a route dependency, and FastAPI runs
   dependencies before it validates the body model, so the phone and password are neither read nor
   validated for a rejected request (R22.7). A body that is not parseable JSON is still the framework's
   422 first (the R1.14 exception). A client address of `None` skips the limit.
2. **Customer requests, per customer** (`take_request_slot`, run by the customer principals in
   `deps.py`). It runs after the token's signature, expiry and kind are verified (the customer id comes
   from the token) and before the first database read, so a request over the limit is answered 429
   without reading the customer's row. It therefore sits ahead of the "revoked or inactive" 401, and
   behind the 401 and 403 of a token that does not verify. See `deps.py` for what that means for R1.14.
3. **Ticket submissions, per customer** (`take_submit_slot`, run first thing in the submit handler). The
   slot is reserved before the form is read and given back with `release_submit_slot` on every outcome
   but a created ticket.

An answer of 429 `rate_limited` is built by `rate_limited` and carries `Retry-After` in whole seconds.
It never says which account, phone or limit group was involved.

A request that is itself answered 429 is not counted (R22.3). The limiter does not count what it
rejects. For a request that was counted by the principal and is then answered 429 by the handler (a
submission over its own limit, a change-password attempt on a locked phone), the handler calls
`release_request_slot`, which gives that one request-window slot back.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.service_tickets import rate_limit
from app.service_tickets.api.errors import service_error
from app.service_tickets.client_address import client_address
from app.service_tickets.config import ServiceConfig
from app.service_tickets.error_codes import CODE_RATE_LIMITED

#: A source of "now" in whole milliseconds, the unit `rate_limit.take` expects.
Clock = Callable[[], int]


def system_clock() -> int:
    """Production clock: wall time in whole milliseconds."""
    return int(time.time() * 1000)


def get_clock() -> Clock:
    """The request's clock. A test overrides this dependency to step time without sleeping."""
    return system_clock


ClockDep = Annotated[Clock, Depends(get_clock)]


def rate_limited(retry_after: int) -> HTTPException:
    """429 `rate_limited` with `Retry-After` in whole seconds. The body is the same for every cause."""
    return service_error(
        HTTPStatus.TOO_MANY_REQUESTS, CODE_RATE_LIMITED, headers={"Retry-After": str(retry_after)}
    )


# --------------------------------------------------------------------------- sign-in, per address


def check_signin_address(request: Request, config: ServiceConfig, now_ms: int) -> None:
    """Take one slot of the caller's address window; raise 429 `rate_limited` when it is full (R22.1).

    The address comes from `client_address` (peer = `request.client.host`, plus the viewer-address header
    when the peer is a trusted proxy). `None` means no usable address: the limit is skipped.
    """
    peer = request.client.host if request.client else None
    address = client_address(peer, request.headers, config)
    if address is None:
        return
    result = rate_limit.take(
        rate_limit.signin_key(address),
        config.signin_rate_max,
        config.signin_rate_window_seconds * 1000,
        now_ms,
    )
    if not result.allowed:
        raise rate_limited(result.retry_after)


# --------------------------------------------------------------------------- customer requests


def take_request_slot(customer_id: uuid.UUID, config: ServiceConfig, now_ms: int) -> None:
    """Take one slot of the customer's request window; raise 429 `rate_limited` when it is full (R22.3)."""
    result = rate_limit.take(
        rate_limit.request_key(customer_id),
        config.request_rate_max,
        config.request_rate_window_seconds * 1000,
        now_ms,
    )
    if not result.allowed:
        raise rate_limited(result.retry_after)


def release_request_slot(customer_id: uuid.UUID) -> None:
    """Give back the request-window slot of a request that ends up answered 429 (R22.3)."""
    rate_limit.release(rate_limit.request_key(customer_id))


# --------------------------------------------------------------------------- ticket submissions


def take_submit_slot(customer_id: uuid.UUID, config: ServiceConfig, now_ms: int) -> None:
    """Reserve one submission slot; raise 429 `rate_limited` when the window is full (R22.2).

    A rejected reservation counts nothing, so the caller must not release it. The request-window slot of
    the rejected submission is given back here: the request is answered 429, and such requests are not
    counted (R22.3).
    """
    result = rate_limit.take(
        rate_limit.submit_key(customer_id),
        config.submit_rate_max,
        config.submit_rate_window_seconds * 1000,
        now_ms,
    )
    if not result.allowed:
        release_request_slot(customer_id)
        raise rate_limited(result.retry_after)


def release_submit_slot(customer_id: uuid.UUID) -> None:
    """Give back a reserved submission slot because no ticket was created."""
    rate_limit.release(rate_limit.submit_key(customer_id))
