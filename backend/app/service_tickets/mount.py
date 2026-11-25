"""Conditional mount of the ticket routers (Requirement 21).

`mount` is called once from `create_app()` with the configuration read at start. With the flag off it
does nothing, so no ticket route exists and every request under the ticket prefix falls through to the
framework's unknown-path 404.

With the flag on it also adds one start-up hook that only logs (the Pillow WebP check). `main.py` passes
`lifespan=` to FastAPI, and Starlette ignores `on_startup` handlers when a lifespan is given, so the hook
wraps `app.router.lifespan_context`. The wrapper runs the check, then enters the original lifespan and
passes its state through unchanged. `main.py` needs no change, and with the flag off the lifespan object
is untouched.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI

from app.service_tickets.config import ServiceConfig


def _ticket_routers() -> list[APIRouter]:
    """The ticket routers in their fixed mount order: customer, shared photo, public branding, then staff.

    The routers are imported here, not at module level, so that with the flag off nothing of the Ticket
    API beyond this module is even imported by `create_app()` (R21.3).
    """
    from app.service_tickets.api import customer_router, photo_router, public_router, staff_router

    return [customer_router.router, photo_router.router, public_router.router, staff_router.router]


def add_startup_check(app: FastAPI, check: Callable[[], object]) -> None:
    """Run `check` once at application start, before the existing lifespan is entered.

    `check` must not raise (it only logs). The existing lifespan keeps running exactly as before, and
    its yielded state is passed on.
    """
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan_with_check(application: Any) -> AsyncIterator[Any]:
        check()
        async with original(application) as state:
            yield state

    app.router.lifespan_context = lifespan_with_check


def _check_webp() -> None:
    # Looked up at call time, so a test can replace `photo_store.check_webp_support`.
    from app.service_tickets import photo_store

    photo_store.check_webp_support()


def mount(app: FastAPI, config: ServiceConfig, api_prefix: str) -> None:
    """Include the ticket routers under `{api_prefix}/service` when the flag is on; do nothing otherwise."""
    if not config.enabled:
        return
    for router in _ticket_routers():
        app.include_router(router, prefix=f"{api_prefix.rstrip('/')}/service")
    add_startup_check(app, _check_webp)
