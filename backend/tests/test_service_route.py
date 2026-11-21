"""`ServiceRoute` of the service ticket system (R20.13, R20.6, R9.6).

Each test builds a small application whose routers use `ServiceRoute`, plus one plain FastAPI route next
to them, so the tests can show both what the package's routes do and that nothing leaks into routes
outside the package.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Query, Response
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.service_tickets.api.deps import ServiceSession
from app.service_tickets.api.errors import service_error
from app.service_tickets.api.route import (
    CODE_INVALID_REQUEST,
    ServiceRoute,
    fields_from_validation_errors,
)
from app.service_tickets.error_codes import ERROR_CODES


class TicketForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str
    phone: str
    description: str

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("invalid_phone")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("invalid_description")
        return value


class ExpectedStatus(BaseModel):
    expected_status: str


def _build_app(session: Session) -> FastAPI:
    app = FastAPI()
    router = APIRouter(prefix="/svc", route_class=ServiceRoute)

    @router.post("/form")
    def post_form(body: TicketForm) -> dict[str, str]:
        return {"city": body.city}

    @router.post("/status")
    def post_status(body: ExpectedStatus) -> dict[str, str]:
        return {"ok": body.expected_status}

    @router.get("/page")
    def get_page(page: int = Query(1, ge=1)) -> dict[str, int]:
        return {"page": page}

    @router.get("/ok")
    def get_ok() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/own-cache-control")
    def get_own_cache_control(response: Response) -> dict[str, bool]:
        response.headers["Cache-Control"] = "private, max-age=60"
        return {"ok": True}

    @router.get("/denied")
    def get_denied() -> None:
        raise service_error(403, "insufficient_role")

    @router.get("/missing-table")
    def get_missing_table(db: ServiceSession) -> dict[str, bool]:
        db.execute(text("SELECT 1"))  # opens the transaction
        db.execute(text("SELECT * FROM svc_table_that_does_not_exist"))
        return {"ok": True}

    @router.get("/driver-secret")
    def get_driver_secret(db: ServiceSession) -> dict[str, bool]:
        db.execute(text("SELECT secret_column_name FROM svc_customers"))
        return {"ok": True}

    @router.get("/boom")
    def get_boom() -> dict[str, bool]:
        raise RuntimeError("boom")

    @router.get("/plain-http-error")
    def get_plain_http_error() -> None:
        raise HTTPException(status_code=404, detail="gone")

    app.include_router(router)

    @app.get("/outside")
    def get_outside(page: int = Query(1, ge=1)) -> dict[str, int]:
        return {"page": page}

    app.dependency_overrides[get_db] = lambda: session
    return app


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    with TestClient(_build_app(session), raise_server_exceptions=False) as test_client:
        yield test_client


def _error(response: Any) -> dict[str, Any]:
    return response.json()["detail"]["error"]


# --------------------------------------------------------------------------- validation envelope


def test_validation_failure_is_the_package_envelope_with_every_failing_field(client: TestClient) -> None:
    response = client.post("/svc/form", json={"city": "Haifa", "phone": "05x", "description": "  "})

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "error": {
                "code": "invalid_phone",
                "fields": {"phone": "invalid_phone", "description": "invalid_description"},
            }
        }
    }


def test_the_top_level_code_is_the_first_failing_field_not_the_last(client: TestClient) -> None:
    response = client.post("/svc/form", json={"city": "Haifa", "phone": "1234", "description": ""})

    assert response.status_code == 422
    assert _error(response)["code"] == "invalid_description"
    assert list(_error(response)["fields"]) == ["description"]


def test_extra_and_missing_fields_get_their_own_codes(client: TestClient) -> None:
    form = {"city": "Haifa", "phone": "1", "description": "x", "owner": "me"}
    response = client.post("/svc/form", json=form)
    assert response.status_code == 422
    assert _error(response) == {"code": "unknown_field", "fields": {"owner": "unknown_field"}}

    response = client.post("/svc/status", json={})
    assert response.status_code == 422
    assert _error(response) == {
        "code": "invalid_expected_status",
        "fields": {"expected_status": "invalid_expected_status"},
    }


def test_a_bad_query_parameter_is_named_by_its_field(client: TestClient) -> None:
    response = client.get("/svc/page", params={"page": "0"})

    assert response.status_code == 422
    assert _error(response) == {"code": "invalid_page", "fields": {"page": "invalid_page"}}


def test_a_body_that_is_not_json_still_gets_an_envelope(client: TestClient) -> None:
    response = client.post("/svc/form", content=b"{not json", headers={"Content-Type": "application/json"})

    assert response.status_code == 422
    assert _error(response) == {"code": CODE_INVALID_REQUEST, "fields": {"body": CODE_INVALID_REQUEST}}


def test_a_route_outside_the_package_keeps_the_framework_422_body(client: TestClient) -> None:
    response = client.get("/outside", params={"page": "0"})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail[0]["loc"] == ["query", "page"]
    assert "cache-control" not in response.headers


def test_no_application_wide_handler_is_registered(session: Session) -> None:
    app = _build_app(session)

    assert app.exception_handlers.keys() == FastAPI().exception_handlers.keys()


# --------------------------------------------------------------------------- field code rules


def _error_item(
    loc: tuple[Any, ...], type_: str = "value_error", error: Exception | None = None
) -> dict[str, Any]:
    item: dict[str, Any] = {"loc": loc, "type": type_, "msg": "x"}
    if error is not None:
        item["ctx"] = {"error": error}
    return item


def test_field_codes_follow_the_documented_rules() -> None:
    errors = [
        _error_item(("body", "city"), error=ValueError("invalid_city")),
        _error_item(("body", "phone"), error=ValueError("a message that is not a code")),
        _error_item(("body", "category_id"), "uuid_parsing"),
        _error_item(("body", "extra"), "extra_forbidden"),
        _error_item(("body", "nonsense_field"), "int_parsing"),
        _error_item(("query", "page"), "int_parsing"),
        _error_item(("body", "new_password"), "string_too_short"),
    ]

    assert fields_from_validation_errors(errors) == {
        "city": "invalid_city",
        "phone": "invalid_phone",
        "category_id": "invalid_category",
        "extra": "unknown_field",
        "nonsense_field": CODE_INVALID_REQUEST,
        "page": "invalid_page",
        "new_password": "invalid_password_format",
    }


def test_the_first_error_of_a_field_wins_and_nested_locations_name_the_top_field() -> None:
    errors = [
        _error_item(("body", "city"), error=ValueError("invalid_city")),
        _error_item(("body", "city"), error=ValueError("invalid_apartment")),
        _error_item(("body", "items", 0, "name"), "missing"),
    ]

    assert fields_from_validation_errors(errors) == {"city": "invalid_city", "items": CODE_INVALID_REQUEST}


def test_every_code_the_rules_can_produce_for_a_known_field_is_in_the_catalogue() -> None:
    codes = fields_from_validation_errors(
        [
            _error_item(("body", name), "missing")
            for name in ("page", "expected_status", "target_status", "category_id", "password", "full_name")
        ]
    )

    assert set(codes.values()) <= set(ERROR_CODES)
    assert CODE_INVALID_REQUEST not in ERROR_CODES


# --------------------------------------------------------------------------- database failures


def test_a_database_error_rolls_back_and_answers_503(client: TestClient, session: Session) -> None:
    response = client.get("/svc/missing-table")

    assert response.status_code == 503
    assert response.json() == {"detail": {"error": {"code": "service_unavailable"}}}
    assert response.headers["cache-control"] == "no-store"
    # The request had opened a transaction with its first statement; only a rollback ends it.
    assert not session.in_transaction()


def test_the_rollback_is_the_sessions_own_rollback(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = session.rollback

    def spy() -> None:
        calls.append("rollback")
        original()

    monkeypatch.setattr(session, "rollback", spy)
    with TestClient(_build_app(session), raise_server_exceptions=False) as test_client:
        assert test_client.get("/svc/missing-table").status_code == 503

    assert calls == ["rollback"]


def test_a_failing_rollback_does_not_turn_the_503_into_a_500(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> None:
        raise ConnectionError("connection dropped")

    monkeypatch.setattr(session, "rollback", broken)
    with TestClient(_build_app(session), raise_server_exceptions=False) as test_client:
        response = test_client.get("/svc/missing-table")

    assert response.status_code == 503
    assert _error(response)["code"] == "service_unavailable"


def test_the_log_line_names_the_exception_class_and_nothing_from_the_driver(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="app.service_tickets.api.route"):
        response = client.get("/svc/driver-secret")

    assert response.status_code == 503
    ours = [record for record in caplog.records if record.name == "app.service_tickets.api.route"]
    messages = [record.getMessage() for record in ours]
    assert len(messages) == 1
    assert "OperationalError" in messages[0] or "ProgrammingError" in messages[0]
    for fragment in ("secret_column_name", "svc_customers", "SELECT"):
        assert fragment not in messages[0]
    # Nothing was logged with a traceback either, which would carry the driver message.
    assert all(record.exc_info is None for record in caplog.records if record.name.endswith("api.route"))


# --------------------------------------------------------------------------- other failures


def test_an_unexpected_exception_is_a_500_for_that_request_only(client: TestClient) -> None:
    assert client.get("/svc/ok").status_code == 200

    boom = client.get("/svc/boom")
    assert boom.status_code == 500
    assert "boom" not in boom.text

    again = client.get("/svc/ok")
    assert again.status_code == 200
    assert again.json() == {"ok": True}


def test_an_http_exception_keeps_its_status_body_and_headers_and_gains_no_store(client: TestClient) -> None:
    denied = client.get("/svc/denied")
    assert denied.status_code == 403
    assert denied.json() == {"detail": {"error": {"code": "insufficient_role"}}}
    assert denied.headers["cache-control"] == "no-store"

    plain = client.get("/svc/plain-http-error")
    assert plain.status_code == 404
    assert plain.json() == {"detail": "gone"}
    assert plain.headers["cache-control"] == "no-store"


# --------------------------------------------------------------------------- Cache-Control


def test_every_response_kind_carries_no_store(client: TestClient) -> None:
    ok = client.get("/svc/ok")
    invalid = client.get("/svc/page", params={"page": "x"})

    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    assert invalid.status_code == 422 and invalid.headers["cache-control"] == "no-store"


def test_a_cache_control_the_route_set_is_kept(client: TestClient) -> None:
    response = client.get("/svc/own-cache-control")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, max-age=60"