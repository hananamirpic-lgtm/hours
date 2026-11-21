"""`ServiceRoute`: the route class of every router in the service ticket package.

It wraps each route handler with four behaviours, and only for routes that use this class. No
exception handler is registered on the application, because that would change the 422 body of every
Hours endpoint (R20.6).

* `RequestValidationError` becomes the package's 422 envelope (see `errors.service_error`):
  `{"detail": {"error": {"code": ..., "fields": {field: code}}}}`. `code` is the code of the first
  failing field and `fields` carries every failing field, in the order FastAPI reported them.
* `DBAPIError` (an undefined table raises `ProgrammingError`, a dropped connection raises
  `OperationalError`) rolls the request's session back and answers 503 `service_unavailable`. The log
  line carries the exception class only: a driver message can hold SQL text or a parameter value.
* Any other exception is left alone. Starlette answers 500 for that request, the process keeps
  running, and `get_db` closes the session in its `finally` (R20.13).
* Every response carries `Cache-Control: no-store` unless the route already set a `Cache-Control`.
  An `HTTPException` raised by a handler or a dependency is turned into its response here, with the
  stock FastAPI handler, so 401, 403 and 404 answers carry the header too.

The session to roll back is the one `api.deps.get_service_session` recorded on `request.state`. A
route that never asked for it has nothing to roll back, and `get_db` closes (and so rolls back) its
session anyway.

Which code a failing field gets
-------------------------------
A pydantic validator that raises `ValueError("<code>")` with a code from `ERROR_CODES` gives that
code. An unexpected extra field gives `unknown_field`. Otherwise the code is `invalid_<field>` when
that is a catalogue code (`page` gives `invalid_page`, `expected_status` gives
`invalid_expected_status`), with a short alias table for the few fields whose name differs from their
code. A field that matches none of these gets `invalid_request`, which is not in the catalogue, so
the front end shows its generic message.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.service_tickets.api.errors import service_error
from app.service_tickets.error_codes import (
    CODE_INVALID_CATEGORY,
    CODE_INVALID_PASSWORD_FORMAT,
    CODE_SERVICE_UNAVAILABLE,
    CODE_UNKNOWN_FIELD,
    ERROR_CODES,
)

logger = logging.getLogger(__name__)

#: Answer for a failing field that matches no catalogue code. Deliberately not in `ERROR_CODES`.
CODE_INVALID_REQUEST = "invalid_request"

#: Where FastAPI says a value came from, the first element of an error `loc`.
_LOCATIONS = frozenset({"body", "query", "path", "header", "cookie"})

#: Fields whose code is not `invalid_<field>`.
_FIELD_CODE_ALIASES: dict[str, str] = {
    "category_id": CODE_INVALID_CATEGORY,
    "password": CODE_INVALID_PASSWORD_FORMAT,
    "current_password": CODE_INVALID_PASSWORD_FORMAT,
    "new_password": CODE_INVALID_PASSWORD_FORMAT,
}

_CATALOGUE = frozenset(ERROR_CODES)

Handler = Callable[[Request], Awaitable[Response]]


def _field_name(loc: tuple[Any, ...]) -> str:
    """The top-level field of an error location: `("body", "city")` gives `city`."""
    parts = list(loc)
    location = parts.pop(0) if parts and parts[0] in _LOCATIONS else "body"
    for part in parts:
        if isinstance(part, str):
            return part
    return location


def _code_for(error: dict[str, Any], field: str) -> str:
    raised = (error.get("ctx") or {}).get("error")
    if isinstance(raised, ValueError) and str(raised) in _CATALOGUE:
        return str(raised)
    if error.get("type") == "extra_forbidden":
        return CODE_UNKNOWN_FIELD
    if field in _FIELD_CODE_ALIASES:
        return _FIELD_CODE_ALIASES[field]
    candidate = f"invalid_{field}"
    return candidate if candidate in _CATALOGUE else CODE_INVALID_REQUEST


def fields_from_validation_errors(errors: list[dict[str, Any]] | tuple[Any, ...]) -> dict[str, str]:
    """One code per failing field, in the order FastAPI reported them. The first error of a field wins."""
    fields: dict[str, str] = {}
    for error in errors:
        field = _field_name(tuple(error.get("loc", ())))
        if field not in fields:
            fields[field] = _code_for(error, field)
    return fields


def validation_error(exc: RequestValidationError) -> HTTPException:
    """The 422 for a `RequestValidationError`, with `fields` and the first field's code."""
    fields = fields_from_validation_errors(exc.errors())
    if not fields:
        fields = {"body": CODE_INVALID_REQUEST}
    return service_error(HTTPStatus.UNPROCESSABLE_ENTITY, fields=fields)


def _json_response(error: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=error.headers)


def _roll_back(request: Request) -> None:
    session = getattr(request.state, "service_session", None)
    if session is None:
        return
    try:
        session.rollback()
    except Exception as error:  # noqa: BLE001 - a dead connection must not turn the 503 into a 500
        logger.warning("service ticket rollback failed: %s", type(error).__name__)


class ServiceRoute(APIRoute):
    """The route class of the package's routers. See the module docstring."""

    def get_route_handler(self) -> Handler:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                response = await original(request)
            except RequestValidationError as exc:
                response = _json_response(validation_error(exc))
            except DBAPIError as exc:
                _roll_back(request)
                logger.error("service ticket database failure: %s", type(exc).__name__)
                unavailable = service_error(HTTPStatus.SERVICE_UNAVAILABLE, CODE_SERVICE_UNAVAILABLE)
                response = _json_response(unavailable)
            except StarletteHTTPException as exc:
                response = await http_exception_handler(request, exc)
            if "cache-control" not in response.headers:
                response.headers["Cache-Control"] = "no-store"
            return response

        return handler