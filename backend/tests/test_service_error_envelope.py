"""Error-code catalogue and error envelope of the service ticket system (R8.12, R9.6, R3.5, R16.6, R19.5)."""

from __future__ import annotations

import re
from http import HTTPStatus

import pytest
from fastapi import HTTPException

from app.api import deps
from app.service_tickets import error_codes
from app.service_tickets.api.errors import service_error
from app.service_tickets.error_codes import ERROR_CODES

SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")

# Every code that requirements.md names after "the code" (the backtick-quoted lowercase snake_case word).
# The spec folder is not mounted in the API container, so the list is kept here rather than parsed.
# Re-derive it with: the regex  the codes?\s+`([a-z][a-z0-9_]*)`  over requirements.md.
CODES_NAMED_IN_REQUIREMENTS = (
    "duplicate_category",
    "invalid_credentials",
    "invalid_date_range",
    "invalid_filter",
    "invalid_page",
    "invalid_password_format",
    "invalid_phone",
    "invalid_photo_type",
    "invalid_ticket_number",
    "invalid_transition",
    "last_active_category",
    "password_change_required",
    "password_not_changed",
    "phone_already_registered",
    "photo_dimensions_too_large",
    "photo_too_large",
    "photo_upload_disabled",
    "rate_limited",
    "request_too_large",
    "ticket_not_found",
    "ticket_numbers_exhausted",
    "ticket_status_changed",
    "too_many_attempts",
    "too_many_photos",
)


def _constants() -> dict[str, str]:
    return {name: value for name, value in vars(error_codes).items() if name.startswith("CODE_")}


# --- the envelope --------------------------------------------------------------------------------


def test_single_code_shape() -> None:
    error = service_error(HTTPStatus.NOT_FOUND, "ticket_not_found")
    assert isinstance(error, HTTPException)
    assert error.status_code == 404
    assert error.detail == {"error": {"code": "ticket_not_found"}}


def test_matches_the_existing_api_error_shape() -> None:
    expected = deps.api_error(HTTPStatus.CONFLICT, "invalid_transition")
    actual = service_error(HTTPStatus.CONFLICT, "invalid_transition")
    assert (actual.status_code, actual.detail) == (expected.status_code, expected.detail)


def test_fields_key_is_omitted_when_absent_or_empty() -> None:
    assert "fields" not in service_error(422, "invalid_phone").detail["error"]
    assert "fields" not in service_error(422, "invalid_phone", fields=None).detail["error"]
    assert "fields" not in service_error(422, "invalid_phone", fields={}).detail["error"]


def test_fields_carry_every_failing_field_and_code_is_the_first() -> None:
    fields = {"phone": "invalid_phone", "description": "invalid_description", "urgent": "invalid_urgent"}
    error = service_error(HTTPStatus.UNPROCESSABLE_ENTITY, fields=fields)
    assert error.status_code == 422
    assert error.detail == {"error": {"code": "invalid_phone", "fields": fields}}
    assert list(error.detail["error"]["fields"]) == ["phone", "description", "urgent"]


def test_first_field_in_form_order_wins_not_alphabetical_order() -> None:
    fields = {"description": "invalid_description", "category": "invalid_category"}
    assert service_error(422, fields=fields).detail["error"]["code"] == "invalid_description"


def test_fields_are_copied_not_shared() -> None:
    fields = {"city": "invalid_city"}
    error = service_error(422, fields=fields)
    fields["apartment"] = "invalid_apartment"
    assert error.detail["error"]["fields"] == {"city": "invalid_city"}


def test_explicit_code_is_kept_when_fields_are_given() -> None:
    error = service_error(422, "unknown_field", fields={"phone": "invalid_phone"})
    assert error.detail["error"]["code"] == "unknown_field"
    assert error.detail["error"]["fields"] == {"phone": "invalid_phone"}


def test_code_or_fields_is_required() -> None:
    with pytest.raises(ValueError):
        service_error(422)
    with pytest.raises(ValueError):
        service_error(422, fields={})


def test_retry_after_header_passes_through() -> None:
    error = service_error(HTTPStatus.TOO_MANY_REQUESTS, "too_many_attempts", headers={"Retry-After": "120"})
    assert error.status_code == 429
    assert error.headers == {"Retry-After": "120"}
    assert error.detail == {"error": {"code": "too_many_attempts"}}


def test_headers_absent_by_default() -> None:
    assert service_error(404, "ticket_not_found").headers is None


# --- the catalogue -------------------------------------------------------------------------------


def test_catalogue_is_not_empty_and_has_unique_codes() -> None:
    assert len(ERROR_CODES) == 52
    assert len(set(ERROR_CODES)) == len(ERROR_CODES)


def test_codes_are_snake_case() -> None:
    bad = [code for code in ERROR_CODES if not SNAKE_CASE.fullmatch(code)]
    assert not bad, f"not snake_case: {bad}"


def test_every_constant_appears_in_the_catalogue_exactly_once() -> None:
    for name, value in _constants().items():
        assert ERROR_CODES.count(value) == 1, f"{name} = {value!r} appears {ERROR_CODES.count(value)} times"


def test_every_catalogue_code_has_a_constant() -> None:
    constant_values = list(_constants().values())
    for code in ERROR_CODES:
        assert constant_values.count(code) == 1, f"{code!r} has {constant_values.count(code)} constants"


def test_constant_name_is_the_upper_cased_code() -> None:
    for name, value in _constants().items():
        assert name == f"CODE_{value.upper()}"


@pytest.mark.parametrize("code", CODES_NAMED_IN_REQUIREMENTS)
def test_every_code_named_in_requirements_is_in_the_catalogue(code: str) -> None:
    assert code in ERROR_CODES


def test_catalogue_includes_every_design_group() -> None:
    for code in (
        "not_authenticated",  # access
        "too_many_attempts",  # sign-in
        "password_not_changed",  # password
        "customer_not_found",  # customers
        "invalid_assigned_technician_name",  # tickets
        "photo_too_large",  # photo
        "history_entry_not_found",  # history
        "invalid_filter",  # lists
        "category_not_found",  # config
        "photo_storage_unavailable",  # server
    ):
        assert code in ERROR_CODES


def test_codes_shared_with_hours_keep_their_value() -> None:
    assert error_codes.CODE_CUSTOMER_TOKEN_NOT_ACCEPTED == deps.CODE_CUSTOMER_TOKEN_NOT_ACCEPTED
    assert error_codes.CODE_PASSWORD_CHANGE_REQUIRED == deps.CODE_PASSWORD_CHANGE_REQUIRED
    assert error_codes.CODE_INSUFFICIENT_ROLE == deps.CODE_INSUFFICIENT_ROLE
