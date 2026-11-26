"""Preflight guard for the flag-off state (Requirement 21.3, design finding F4).

While the service-ticket flag is off, every request under `{api_prefix}/service` must get the same
404 the Hours API gives an unknown path. `CORSMiddleware` answers a preflight (an `OPTIONS` request
with `Origin` and `Access-Control-Request-Method`) before routing, for any path, so that one case never
reaches the router. This wrapper sits outside `CORSMiddleware` and answers those requests itself.

The reply is built the way Starlette builds the unknown-route reply: a `JSONResponse` of
`{"detail": "Not Found"}` with status 404, passed through `SecurityHeadersMiddleware` (which an unknown
route also passes through, because that middleware is outside the router). Everything else, including
lifespan and websocket scopes, goes to the wrapped application untouched.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.security_headers import SecurityHeadersMiddleware


class PreflightGuard:
    """Answer `OPTIONS` under `{api_prefix}/service` with the unknown-path 404; pass the rest through."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        api_prefix: str,
        hsts_enabled: bool,
        hsts_max_age_seconds: int,
    ) -> None:
        self._app = app
        self._ticket_prefix = f"{api_prefix.rstrip('/')}/service"
        self._not_found = SecurityHeadersMiddleware(
            self._respond_not_found,
            hsts_enabled=hsts_enabled,
            hsts_max_age_seconds=hsts_max_age_seconds,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "OPTIONS" and self._is_ticket_path(scope["path"]):
            await self._not_found(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _is_ticket_path(self, path: str) -> bool:
        """True for the prefix itself and anything below it, not for a lookalike such as `/api/services`."""
        return path == self._ticket_prefix or path.startswith(self._ticket_prefix + "/")

    @staticmethod
    async def _respond_not_found(scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)