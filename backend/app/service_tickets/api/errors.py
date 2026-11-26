"""The error envelope of the service ticket system (design.md, "Error envelope"; R9.6, R3.5, R16.6, R19.5).

The shape is the one Hours already uses, built by `api_error` in `app.api.deps`:

    {"detail": {"error": {"code": "invalid_phone"}}}

For a form with several failing fields the package adds one key, `fields`, mapping each failing field
to its own code:

    {"detail": {"error": {"code": "invalid_phone",
                          "fields": {"phone": "invalid_phone", "description": "invalid_description"}}}}

`code` is the code of the first failing field in form reading order, so a single-field failure matches
"422 and the code X" literally. The caller passes `fields` already in form order (a dict keeps its
insertion order); this module does not sort it.
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import HTTPException

from app.api.deps import api_error


def service_error(
    status: HTTPStatus | int,
    code: str | None = None,
    fields: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """An `HTTPException` carrying the package's error envelope.

    `fields` is added only when it is non-empty. When it is, and `code` is None, the top-level `code`
    is the code of the first field (form order). An explicit `code` is kept as given, for the rare
    caller whose top-level code is deliberately not a field code. `headers`, such as `Retry-After`,
    pass through unchanged. A call with neither `code` nor `fields` is a programming error.
    """
    if not fields:
        fields = None
    if code is None:
        if fields is None:
            raise ValueError("service_error needs a code or at least one failing field")
        code = next(iter(fields.values()))
    error = api_error(HTTPStatus(status), code, headers=headers)
    if fields is not None:
        error.detail["error"]["fields"] = dict(fields)
    return error
