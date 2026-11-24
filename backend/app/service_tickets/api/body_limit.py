"""Request body guard for the multipart routes (Requirements 22.6, 25.6 and 28.2; design "Abuse protection").

No layer in front of the API limits a request body today, so the limits are enforced here, on three
routes only, each an exact match of method and path:

* `POST {api_prefix}/service/tickets` (the customer submission) and
  `POST {api_prefix}/service/staff/tickets` (staff opening a ticket for a customer, R26.8): 12 MB.
* `PUT {api_prefix}/service/staff/branding` (the company name and logo, R28.2): 2 MiB, which holds the
  1 MiB logo and the form around it.

Every other path, another method on the same paths and any subpath (`/tickets/10000/cancel`) pass
through untouched.

* **Declared length.** A `Content-Length` over the limit of the route (`MAX_BODY_BYTES` or
  `MAX_BRANDING_BODY_BYTES`) is answered 413 `request_too_large`
  before the application is called, so the body is never read. The check needs no token: the guard
  sits outside routing and authentication.
* **Streamed length.** The bytes of every `http.request` message are counted, whether or not the
  request declared a length (chunked transfer, an absent, repeated or malformed header, or a client
  that sends more than it declared). The moment the count passes the limit the receive channel raises
  an internal exception, so the application stops reading. If the application has not started its
  response, the guard answers 413. If it has, the guard only stops: a second `http.response.start`
  would be a protocol error.
* **Invalid `Content-Length`.** A value that is not a plain run of ASCII digits (empty, signed,
  decimal, hexadecimal) is ignored as a declaration; the streamed count still applies. A digit-only
  value too long to be a real size is over the limit. HTTP servers reject these before the application
  anyway; this keeps the guard safe when one does not.

Exactly the limit of a route passes; one more byte fails.

The middleware is pure ASGI (no `BaseHTTPMiddleware`, which would buffer the stream and wrap the
exception). In `create_app()` it is added only when the flag is on, before the header middlewares, so
it is the innermost of the user middlewares and its 413 still receives the security headers and the
CORS headers.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.service_tickets.api.errors import service_error
from app.service_tickets.error_codes import CODE_REQUEST_TOO_LARGE

#: 12 MB as the requirements count it: 12 x 1,048,576 bytes (R22.6). A code constant, not a setting.
MAX_BODY_BYTES = 12_582_912

#: 2 MiB for the branding form: the 1 MiB logo plus the form around it (R28.2). A code constant.
MAX_BRANDING_BODY_BYTES = 2_097_152

#: A decimal number of more digits than this (after leading zeros) is larger than any real body.
_MAX_DIGITS = 19


class _BodyTooLarge(Exception):
    """Raised from the receive channel when the streamed body passes the limit."""


def _declared_over_limit(headers: list[tuple[bytes, bytes]], limit: int) -> bool:
    """True when any `Content-Length` header is a valid number over `limit`.

    Anything that is not a plain run of ASCII digits is ignored here; the streamed count covers it.
    """
    for name, raw in headers:
        if name.lower() != b"content-length":
            continue
        value = raw.strip()
        if not value or not value.isdigit():  # bytes.isdigit() is ASCII only
            continue
        digits = value.lstrip(b"0")
        if len(digits) > _MAX_DIGITS or (digits and int(digits) > limit):
            return True
    return False


class BodyLimitMiddleware:
    """Answer 413 `request_too_large` for an oversized ticket submission or branding body.

    `limit` is the limit of the two ticket paths and `branding_limit` the one of the branding path.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        api_prefix: str,
        limit: int = MAX_BODY_BYTES,
        branding_limit: int = MAX_BRANDING_BODY_BYTES,
    ) -> None:
        self._app = app
        base = f"{api_prefix.rstrip('/')}/service"
        # (method, exact path) -> the most bytes the body may have.
        self._limits: dict[tuple[str, str], int] = {
            # The customer submission and the staff submission on a customer's behalf (R22.6, R26.8).
            ("POST", f"{base}/tickets"): limit,
            ("POST", f"{base}/staff/tickets"): limit,
            # The company name and logo of the new-ticket page (R28.2).
            ("PUT", f"{base}/staff/branding"): branding_limit,
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = self._limits.get((scope["method"], scope["path"])) if scope["type"] == "http" else None
        if limit is None:
            await self._app(scope, receive, send)
            return

        if _declared_over_limit(list(scope.get("headers", [])), limit):
            await self._too_large(scope, receive, send)
            return

        received = 0
        response_started = False

        async def counting_receive() -> Message:
            nonlocal received
            if received > limit:
                raise _BodyTooLarge
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            if not response_started:
                await self._too_large(scope, receive, send)

    @staticmethod
    async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
        error = service_error(413, CODE_REQUEST_TOO_LARGE)
        response = JSONResponse(
            # FastAPI's HTTPException handler puts `exc.detail` under "detail"; do the same here.
            {"detail": error.detail},
            status_code=413,
            # The rest of the body is unread, so the connection cannot be reused.
            headers={"Cache-Control": "no-store", "Connection": "close"},
        )
        await response(scope, receive, send)
