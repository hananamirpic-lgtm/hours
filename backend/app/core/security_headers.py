"""Security response headers (Requirement 20.9).

A small ASGI middleware that stamps the standard set of hardening headers onto every response. These
are cheap, apply everywhere, and are the kind of thing that is easy to forget on a new endpoint — so
they live in one middleware rather than being added per route.

What is set and why:

* **Strict-Transport-Security** — tells the browser to reach this origin over HTTPS only, for the
  configured age. Only meaningful once TLS terminates in front of the app (Requirement 20.1); sending
  it over plain HTTP is harmless because a browser ignores HSTS on an insecure origin. Sent only when
  enabled, so a local HTTP dev server does not pin the developer's browser to HTTPS for a year. The
  TLS termination and the HTTP→HTTPS redirect themselves happen at the ingress in front of the app
  (see `infra/README.md`); the app's contribution is this header.
* **X-Content-Type-Options: nosniff** — stops a browser from second-guessing a declared content type,
  which is the lever behind a class of injection tricks.
* **X-Frame-Options: DENY** — the app is never framed, so clickjacking has nothing to sit on.
* **Referrer-Policy: no-referrer** — a URL in this app may name a resource id; do not leak it to a
  third party in a Referer header.
* **Content-Security-Policy** — a conservative default for an API: deny everything, allow nothing to
  be framed. The single-page front end is served by its own nginx with its own CSP tuned to its
  assets; this policy guards the API's own responses (the docs page, error bodies) from being a
  vector.
* **Cross-Origin-Opener-Policy / Cross-Origin-Resource-Policy** — isolate the browsing context and
  refuse cross-origin embedding of API responses.
* **Permissions-Policy** — disable geolocation (and other powerful features) at the response level,
  which is belt-and-braces for Requirement 20.10: the client never asks for location, and this says
  the server would refuse it the capability anyway.

`Server` is stripped where the server framework allows, so a version string is not advertised.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: The static headers added to every response. Byte strings because that is what the ASGI layer wants.
_STATIC_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    # Location is refused the capability outright (Requirement 20.10), alongside camera/microphone at
    # the API surface — the camera prompt the requirement does allow belongs to the front end origin,
    # not to API responses.
    (b"permissions-policy", b"geolocation=(), camera=(), microphone=()"),
)


class SecurityHeadersMiddleware:
    """Stamp the hardening headers onto every response.

    Written as a raw ASGI middleware rather than `BaseHTTPMiddleware` so the headers are appended to
    the response start without buffering the body — the headers are all this needs to touch.
    """

    def __init__(self, app: ASGIApp, *, hsts_enabled: bool, hsts_max_age_seconds: int) -> None:
        self._app = app
        self._hsts_enabled = hsts_enabled
        self._hsts_max_age_seconds = hsts_max_age_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.extend(self._headers())
            await send(message)

        await self._app(scope, receive, send_with_headers)

    def _headers(self) -> list[tuple[bytes, bytes]]:
        headers = list(_STATIC_HEADERS)
        if self._hsts_enabled:
            value = f"max-age={self._hsts_max_age_seconds}; includeSubDomains".encode()
            headers.append((b"strict-transport-security", value))
        return headers
