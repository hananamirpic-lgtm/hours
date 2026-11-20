"""Feature discovery: `GET {api_prefix}/features` (Requirements 20.5 and 21.3).

The front end asks this one route whether the service-ticket system is on. It is always registered,
flag on or off. It needs no token, and it touches no database, no Redis key and no stored object: the
answer comes from the `ServiceConfig` captured when the application started. The contact appears only
while the flag is on.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.service_tickets.config import ServiceConfig


def build_features_router(config: ServiceConfig) -> APIRouter:
    """A router with the single discovery route, bound to `config`.

    Taking the config as an argument keeps the route free of any dependency (no session, no caller)
    and lets a test build it from any configuration.
    """
    router = APIRouter(tags=["features"])

    @router.get("/features")
    async def get_features() -> JSONResponse:
        body: dict[str, Any] = {"service_tickets": config.enabled}
        if config.enabled:
            body["accessibility_contact"] = config.accessibility_contact
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    return router