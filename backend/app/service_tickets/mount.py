"""Conditional mount of the ticket routers (Requirement 21).

`mount` is called once from `create_app()` with the configuration read at start. With the flag off it
does nothing, so no ticket route exists and every request under the ticket prefix falls through to the
framework's unknown-path 404.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from app.service_tickets.config import ServiceConfig


def _ticket_routers() -> list[APIRouter]:
    """The ticket routers in their fixed mount order: customer (with the shared photo route), then staff.

    The routers are imported here, not at module level, so that with the flag off nothing of the Ticket
    API beyond this module is even imported by `create_app()` (R21.3).
    """
    from app.service_tickets.api import customer_router, staff_router

    return [customer_router.router, staff_router.router]


def mount(app: FastAPI, config: ServiceConfig, api_prefix: str) -> None:
    """Include the ticket routers under `{api_prefix}/service` when the flag is on; do nothing otherwise."""
    if not config.enabled:
        return
    for router in _ticket_routers():
        app.include_router(router, prefix=f"{api_prefix.rstrip('/')}/service")
