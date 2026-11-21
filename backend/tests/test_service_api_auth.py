"""Customer authentication endpoints (task 13.2; R5.1 to R5.6, R5.11, R5.12, R6.1, R6.3, R6.8, R7.11, R1.3).

A small application includes the customer router with the dependency overrides the other service tests
use: each request gets its own session on the in-memory test database (as in production, one session per
request) and the typed configuration is the all-defaults one (5 failures in 900 seconds, 8-hour tokens).
Tokens are real. The router is not mounted anywhere else; task 13.7 does that.

Because requests use their own sessions, a row the test reads afterwards is a row the router committed.
That is how the tests show that a refused sign-in persists its failure row.
"""

from __future__ import annotations

import itertools
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models.user import UserRole
from app.service_tickets.api import customer_router
from app.service_tickets.config import ServiceConfig
from app.service_tickets.customer_token import decode_customer_token, mint_customer_token
from app.service_tickets.models import Customer, SigninFailure

PASSWORD = "4321"
WRONG_PASSWORD = "7351"
NEW_PASSWORD = "5566"
PHONE = "0505556677"
_phones = itertools.count(1)


def _config() -> ServiceConfig:
    """Every setting unset, so every default applies."""
    names = (
        "service_tickets_enabled service_customer_token_ttl_seconds service_signin_rate_max "
        "service_signin_rate_window_seconds service_submit_rate_max service_submit_rate_window_seconds "
        "service_request_rate_max service_request_rate_window_seconds service_signin_lock_attempts "
        "service_signin_lock_seconds service_client_ip_header service_trusted_proxy_cidrs "
        "service_accessibility_contact"
    ).split()
    return ServiceConfig.from_settings(SimpleNamespace(**dict.fromkeys(names, "")))  # type: ignore[arg-type]


CONFIG = _config()


def _build_app(session: Session) -> FastAPI:
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True)

    def request_session() -> Iterator[Session]:
        with factory() as per_request:
            yield per_request

    app = FastAPI()
    app.include_router(customer_router.router, prefix="/api/service")
    app.dependency_overrides[get_db] = request_session
    app.dependency_overrides[customer_router.get_service_config] = lambda: CONFIG
    return app


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    with TestClient(_build_app(session), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture
def make_customer(session: Session, password_hash: str):
    made = itertools.count()

    def _make(**overrides: Any) -> Customer:
        """The first customer of a test has `PHONE`, the next ones get numbers of their own."""
        fields: dict[str, Any] = {
            "phone": PHONE if next(made) == 0 else f"05{next(_phones):08d}",
            "full_name": "Avi Cohen",
            "full_name_key": "avi cohen",
            "city": "Haifa",
            "apartment": "12",
            "password_hash": password_hash,
            "must_change_password": False,
            "token_version": 1,
        }
        fields.update(overrides)
        created = Customer(**fields)
        session.add(created)
        session.commit()
        return created

    return _make


URL = "/api/service/auth"


def _sign_in(client: TestClient, phone: Any = PHONE, password: Any = PASSWORD, **extra: Any) -> Any:
    body: dict[str, Any] = {"phone": phone, "password": password}
    body.update(extra)
    return client.post(f"{URL}/sign-in", json=body)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _headers(customer: Customer, **kwargs: Any) -> dict[str, str]:
    return _bearer(mint_customer_token(customer.id, kwargs.pop("version", customer.token_version), **kwargs))


def _code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


def _failure_count(session: Session, phone: str | None = None) -> int:
    query = select(func.count()).select_from(SigninFailure)
    if phone is not None:
        query = query.where(SigninFailure.phone == phone)
    return session.execute(query).scalar_one()


def _version(session: Session, customer: Customer) -> int:
    return session.execute(select(Customer.token_version).where(Customer.id == customer.id)).scalar_one()


def _add_failures(session: Session, phone: str, count: int, age_seconds: float) -> None:
    moment = datetime.now(UTC) - timedelta(seconds=age_seconds)
    for _ in range(count):
        session.add(SigninFailure(phone=phone, failed_at=moment))
    session.commit()


# --------------------------------------------------------------------------- sign-in: success


def test_sign_in_returns_an_eight_hour_customer_token(client: TestClient, make_customer) -> None:
    customer = make_customer(token_version=3)

    response = _sign_in(client)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "must_change_password"}
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 28800
    assert body["must_change_password"] is False
    claims = decode_customer_token(body["access_token"])
    assert claims.customer_id == customer.id
    assert claims.version == 3
    assert (claims.expires_at - datetime.now(UTC)).total_seconds() == pytest.approx(28800, abs=30)


def test_sign_in_reports_the_must_change_password_flag(client: TestClient, make_customer) -> None:
    make_customer(must_change_password=True)

    response = _sign_in(client)

    assert response.status_code == 200
    assert response.json()["must_change_password"] is True


def test_sign_in_accepts_any_spelling_of_the_phone(client: TestClient, make_customer) -> None:
    make_customer()

    for spelling in ("050-555-6677", "(050) 555-6677", "+972 50-555-6677", "972505556677"):
        assert _sign_in(client, phone=spelling).status_code == 200


def test_the_token_of_a_sign_in_works_on_me(client: TestClient, make_customer) -> None:
    make_customer()
    token = _sign_in(client).json()["access_token"]

    response = client.get(f"{URL}/me", headers=_bearer(token))

    assert response.status_code == 200


def test_a_successful_sign_in_resets_the_failure_count(
    client: TestClient, make_customer, session: Session
) -> None:
    make_customer()
    for _ in range(2):
        assert _sign_in(client, password=WRONG_PASSWORD).status_code == 401
    assert _failure_count(session, PHONE) == 2

    assert _sign_in(client).status_code == 200

    assert _failure_count(session, PHONE) == 0


@pytest.mark.parametrize("authorization", ["Bearer not.a.token", "Basic abc", "Bearer "])
def test_a_token_sent_to_sign_in_is_ignored(client: TestClient, make_customer, authorization: str) -> None:
    make_customer()

    response = client.post(
        f"{URL}/sign-in",
        json={"phone": PHONE, "password": PASSWORD},
        headers={"Authorization": authorization},
    )

    assert response.status_code == 200


def test_staff_and_expired_customer_tokens_sent_to_sign_in_are_ignored(
    client: TestClient, make_customer, make_user
) -> None:
    customer = make_customer()
    staff = make_user(role=UserRole.ADMIN)
    staff_token = create_access_token(user_id=staff.id, token_version=1, role="admin")
    expired = mint_customer_token(customer.id, 1, now=datetime.now(UTC) - timedelta(days=2))

    for token in (staff_token, expired):
        response = client.post(
            f"{URL}/sign-in", json={"phone": PHONE, "password": PASSWORD}, headers=_bearer(token)
        )
        assert response.status_code == 200


def test_a_password_in_the_url_is_ignored(client: TestClient, make_customer) -> None:
    make_customer()

    response = client.post(f"{URL}/sign-in?password={PASSWORD}", json={"phone": PHONE})

    assert response.status_code == 401
    assert _code(response) == "invalid_credentials"


# --------------------------------------------------------------------------- sign-in: 401


def test_every_credential_failure_is_the_same_bytes(client: TestClient, make_customer) -> None:
    make_customer()
    inactive = make_customer(is_active=False)
    other = make_customer().phone  # a second phone, so no one phone reaches the lock of five
    cases = [
        _sign_in(client, phone="0549998888"),  # unregistered
        _sign_in(client, phone=inactive.phone),  # inactive, right password
        _sign_in(client, password=WRONG_PASSWORD),  # wrong
        _sign_in(client, password=""),  # empty
        _sign_in(client, password="12345"),  # not four digits
        _sign_in(client, password="٣٤٥٦"),  # Arabic-Indic digits
        _sign_in(client, phone=other, password=4321),  # not a string
        _sign_in(client, phone=other, password=None),  # null
        client.post(f"{URL}/sign-in", json={"phone": other}),  # missing
    ]

    for response in cases:
        assert response.status_code == 401
        assert response.json() == {"detail": {"error": {"code": "invalid_credentials"}}}
    assert len({response.content for response in cases}) == 1
    assert len({response.headers["content-length"] for response in cases}) == 1
    assert "access_token" not in cases[0].text


def test_every_refused_attempt_leaves_its_failure_row(
    client: TestClient, make_customer, session: Session
) -> None:
    make_customer()
    inactive = make_customer(is_active=False)

    _sign_in(client, phone="0549998888")
    _sign_in(client, phone=inactive.phone)
    _sign_in(client, password=WRONG_PASSWORD)
    _sign_in(client, password="")

    assert _failure_count(session, "0549998888") == 1
    assert _failure_count(session, inactive.phone) == 1
    assert _failure_count(session, PHONE) == 2


def test_a_staff_username_and_password_is_not_a_customer_sign_in(client: TestClient, make_user) -> None:
    make_user(username="0509998877", role=UserRole.ADMIN)

    not_a_phone = _sign_in(client, phone="admin", password="Sup3r-secret-pass")
    phone_shaped = _sign_in(client, phone="0509998877", password="Sup3r-secret-pass")

    assert (not_a_phone.status_code, _code(not_a_phone)) == (422, "invalid_phone")
    assert (phone_shaped.status_code, _code(phone_shaped)) == (401, "invalid_credentials")
    assert "access_token" not in phone_shaped.text


# --------------------------------------------------------------------------- sign-in: 422


@pytest.mark.parametrize(
    "phone", ["", "050-555-667", "05055566778", "abc", "٠٥٠٥٥٥٦٦٧٧", "0" * 31, None, 505556677]
)
def test_a_bad_phone_is_422_and_counts_nothing(
    client: TestClient, make_customer, session: Session, phone: Any
) -> None:
    make_customer()

    response = _sign_in(client, phone=phone)

    assert response.status_code == 422
    assert response.json() == {"detail": {"error": {"code": "invalid_phone"}}}
    assert _failure_count(session) == 0


def test_a_missing_phone_is_invalid_phone(client: TestClient) -> None:
    response = client.post(f"{URL}/sign-in", json={"password": PASSWORD})

    assert (response.status_code, _code(response)) == (422, "invalid_phone")


def test_an_unknown_field_and_a_non_json_body_are_422(client: TestClient) -> None:
    extra = _sign_in(client, owner="me")
    assert (extra.status_code, _code(extra)) == (422, "unknown_field")

    broken = client.post(f"{URL}/sign-in", content=b"{nope", headers={"Content-Type": "application/json"})
    assert broken.status_code == 422
    assert set(broken.json()["detail"]["error"]) == {"code", "fields"}


# --------------------------------------------------------------------------- sign-in: lock


def test_the_fifth_failure_locks_the_phone_even_for_the_right_password(
    client: TestClient, make_customer, session: Session
) -> None:
    make_customer()
    for _ in range(5):
        assert _sign_in(client, password=WRONG_PASSWORD).status_code == 401
    assert _failure_count(session, PHONE) == 5

    for password in (PASSWORD, WRONG_PASSWORD):
        response = _sign_in(client, password=password)
        assert response.status_code == 429
        assert response.json() == {"detail": {"error": {"code": "too_many_attempts"}}}
        assert re.fullmatch(r"[1-9][0-9]*", response.headers["retry-after"])
        assert 899 <= int(response.headers["retry-after"]) <= 900

    assert _failure_count(session, PHONE) == 5  # a locked request records nothing


def test_retry_after_is_whole_seconds_rounded_up(client: TestClient, make_customer, session: Session) -> None:
    make_customer()
    _add_failures(session, PHONE, 5, age_seconds=100.5)  # 799.5 seconds of lock left

    response = _sign_in(client)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "800"


def test_the_lock_applies_to_an_unregistered_phone_in_the_same_way(
    client: TestClient, session: Session
) -> None:
    for _ in range(5):
        assert _sign_in(client, phone="0549998888").status_code == 401

    response = _sign_in(client, phone="0549998888")

    assert response.status_code == 429
    assert _code(response) == "too_many_attempts"


def test_an_expired_lock_lets_the_customer_in(client: TestClient, make_customer, session: Session) -> None:
    make_customer()
    _add_failures(session, PHONE, 5, age_seconds=901)

    assert _sign_in(client).status_code == 200


# --------------------------------------------------------------------------- sign-out


def test_sign_out_is_204_and_revokes_the_token(client: TestClient, make_customer, session: Session) -> None:
    customer = make_customer()
    headers = _headers(customer)

    response = client.post(f"{URL}/sign-out", headers=headers)

    assert response.status_code == 204
    assert response.content == b""
    assert _version(session, customer) == 2
    again = client.get(f"{URL}/me", headers=headers)
    assert (again.status_code, _code(again)) == (401, "invalid_token")
    second = client.post(f"{URL}/sign-out", headers=headers)
    assert (second.status_code, _code(second)) == (401, "invalid_token")
    assert _version(session, customer) == 2  # the refused sign-out bumped nothing


def test_sign_out_leaves_other_customers_alone_and_a_new_sign_in_works(
    client: TestClient, make_customer, session: Session
) -> None:
    customer = make_customer()
    other = make_customer()

    assert client.post(f"{URL}/sign-out", headers=_headers(customer)).status_code == 204

    assert _version(session, other) == 1
    assert client.get(f"{URL}/me", headers=_headers(other)).status_code == 200
    token = _sign_in(client).json()["access_token"]
    assert client.get(f"{URL}/me", headers=_bearer(token)).status_code == 200


def test_sign_out_works_while_a_password_change_is_owed(client: TestClient, make_customer) -> None:
    customer = make_customer(must_change_password=True)

    assert client.post(f"{URL}/sign-out", headers=_headers(customer)).status_code == 204


def test_sign_out_needs_a_customer_token(client: TestClient, make_user) -> None:
    missing = client.post(f"{URL}/sign-out")
    assert (missing.status_code, _code(missing)) == (401, "not_authenticated")

    staff = make_user(role=UserRole.ADMIN)
    token = create_access_token(user_id=staff.id, token_version=1, role="admin")
    refused = client.post(f"{URL}/sign-out", headers=_bearer(token))
    assert (refused.status_code, _code(refused)) == (403, "staff_token_not_accepted")


# --------------------------------------------------------------------------- me


def test_me_returns_the_profile_and_nothing_else(client: TestClient, make_customer) -> None:
    customer = make_customer(city=None, apartment=None)

    response = client.get(f"{URL}/me", headers=_headers(customer))

    assert response.status_code == 200
    assert response.json() == {
        "full_name": "Avi Cohen",
        "phone": PHONE,
        "city": None,
        "apartment": None,
        "must_change_password": False,
    }


def test_me_reports_the_current_flag_and_works_while_a_change_is_owed(
    client: TestClient, make_customer, session: Session
) -> None:
    customer = make_customer(must_change_password=True)
    headers = _headers(customer)

    owed = client.get(f"{URL}/me", headers=headers)
    assert owed.status_code == 200
    assert owed.json()["must_change_password"] is True

    session.execute(update(Customer).where(Customer.id == customer.id).values(must_change_password=False))
    session.commit()
    cleared = client.get(f"{URL}/me", headers=headers)
    assert cleared.json()["must_change_password"] is False


def test_me_refuses_missing_stale_and_staff_tokens(client: TestClient, make_customer, make_user) -> None:
    customer = make_customer()
    staff = make_user(role=UserRole.ADMIN)

    missing = client.get(f"{URL}/me")
    stale = client.get(f"{URL}/me", headers=_headers(customer, version=9))
    staff_token = client.get(
        f"{URL}/me", headers=_bearer(create_access_token(user_id=staff.id, token_version=1, role="admin"))
    )

    assert (missing.status_code, _code(missing)) == (401, "not_authenticated")
    assert (stale.status_code, _code(stale)) == (401, "invalid_token")
    assert (staff_token.status_code, _code(staff_token)) == (403, "staff_token_not_accepted")


# --------------------------------------------------------------------------- change-password


def _change(
    client: TestClient, headers: dict[str, str], current: Any = PASSWORD, new: Any = NEW_PASSWORD
) -> Any:
    return client.post(
        f"{URL}/change-password", headers=headers, json={"current_password": current, "new_password": new}
    )


def test_change_password_returns_a_new_token_and_revokes_the_old_one(
    client: TestClient, make_customer, session: Session
) -> None:
    customer = make_customer(must_change_password=True)
    old = _headers(customer)

    response = _change(client, old)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "must_change_password"}
    assert body["must_change_password"] is False
    assert body["expires_in"] == 28800
    assert client.get(f"{URL}/me", headers=old).status_code == 401
    me = client.get(f"{URL}/me", headers=_bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["must_change_password"] is False
    stored = session.execute(select(Customer.password_hash).where(Customer.id == customer.id)).scalar_one()
    assert verify_password(NEW_PASSWORD, stored)
    assert _sign_in(client, password=NEW_PASSWORD).status_code == 200
    assert _sign_in(client, password=PASSWORD).status_code == 401


def test_a_wrong_current_password_is_401_and_counted(
    client: TestClient, make_customer, session: Session
) -> None:
    customer = make_customer()

    response = _change(client, _headers(customer), current=WRONG_PASSWORD)

    assert response.status_code == 401
    assert response.json() == {"detail": {"error": {"code": "invalid_credentials"}}}
    assert _failure_count(session, PHONE) == 1  # committed with the 401
    assert _version(session, customer) == 1


@pytest.mark.parametrize("current", [None, "", "12", 4321])
def test_a_missing_or_malformed_current_password_is_401(
    client: TestClient, make_customer, session: Session, current: Any
) -> None:
    customer = make_customer()

    response = _change(client, _headers(customer), current=current)

    assert (response.status_code, _code(response)) == (401, "invalid_credentials")
    assert _failure_count(session, PHONE) == 1


def test_a_missing_current_password_field_is_401(client: TestClient, make_customer) -> None:
    customer = make_customer()

    response = client.post(
        f"{URL}/change-password", headers=_headers(customer), json={"new_password": NEW_PASSWORD}
    )

    assert (response.status_code, _code(response)) == (401, "invalid_credentials")


@pytest.mark.parametrize("new", [None, "", "123", "12345", " 123", "abcd", "١٢٣٤", 5566])
def test_a_bad_new_password_is_422_and_not_counted(
    client: TestClient, make_customer, session: Session, new: Any
) -> None:
    customer = make_customer()

    response = _change(client, _headers(customer), new=new)

    assert response.status_code == 422
    assert response.json() == {"detail": {"error": {"code": "invalid_password_format"}}}
    assert _failure_count(session) == 0
    assert _version(session, customer) == 1


def test_the_new_format_is_checked_before_the_current_password(
    client: TestClient, make_customer, session: Session
) -> None:
    customer = make_customer()

    response = _change(client, _headers(customer), current=WRONG_PASSWORD, new="12")

    assert (response.status_code, _code(response)) == (422, "invalid_password_format")
    assert _failure_count(session) == 0


@pytest.mark.parametrize("new", [PASSWORD, "1234"])
def test_the_current_or_initial_password_as_new_is_422(
    client: TestClient, make_customer, session: Session, new: str
) -> None:
    customer = make_customer(must_change_password=True)

    response = _change(client, _headers(customer), new=new)

    assert response.status_code == 422
    assert response.json() == {"detail": {"error": {"code": "password_not_changed"}}}
    assert _version(session, customer) == 1
    still = session.execute(select(Customer.must_change_password).where(Customer.id == customer.id))
    assert still.scalar_one() is True


def test_a_locked_phone_gets_429_with_retry_after_and_nothing_is_changed(
    client: TestClient, make_customer, session: Session
) -> None:
    customer = make_customer(must_change_password=True)
    _add_failures(session, PHONE, 5, age_seconds=100.5)

    response = _change(client, _headers(customer))

    assert response.status_code == 429
    assert response.json() == {"detail": {"error": {"code": "too_many_attempts"}}}
    assert response.headers["retry-after"] == "800"
    assert _version(session, customer) == 1
    assert _failure_count(session, PHONE) == 5


def test_change_password_refuses_missing_and_staff_tokens(client: TestClient, make_user) -> None:
    missing = client.post(f"{URL}/change-password", json={"current_password": "1", "new_password": "2"})
    assert (missing.status_code, _code(missing)) == (401, "not_authenticated")

    staff = make_user(role=UserRole.ADMIN)
    token = create_access_token(user_id=staff.id, token_version=1, role="admin")
    refused = _change(client, _bearer(token))
    assert (refused.status_code, _code(refused)) == (403, "staff_token_not_accepted")


def test_change_password_rejects_an_unknown_field(client: TestClient, make_customer) -> None:
    customer = make_customer()

    response = client.post(
        f"{URL}/change-password",
        headers=_headers(customer),
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD, "must_change_password": False},
    )

    assert (response.status_code, _code(response)) == (422, "unknown_field")


# --------------------------------------------------------------------------- headers and logs


def test_every_response_is_no_store(client: TestClient, make_customer) -> None:
    customer = make_customer()

    responses = [
        _sign_in(client),
        _sign_in(client, password=WRONG_PASSWORD),
        _sign_in(client, phone="x"),
        client.get(f"{URL}/me", headers=_headers(customer)),
        client.get(f"{URL}/me"),
    ]

    assert all(response.headers["cache-control"] == "no-store" for response in responses)


def test_no_phone_or_password_reaches_a_log_line_or_a_response(
    client: TestClient, make_customer, caplog: pytest.LogCaptureFixture
) -> None:
    customer = make_customer()
    headers = _headers(customer)
    secrets = (PHONE, PASSWORD, WRONG_PASSWORD, NEW_PASSWORD)

    with caplog.at_level(logging.DEBUG):
        responses = [
            _sign_in(client, password=WRONG_PASSWORD),
            _sign_in(client, phone="0549998888", password=WRONG_PASSWORD),
            _sign_in(client, phone="bad phone"),
            _change(client, headers, current=WRONG_PASSWORD),
            _change(client, headers, current=PASSWORD, new="x"),
            _change(client, headers),
        ]

    messages = [record.getMessage() for record in caplog.records]
    for secret in secrets:
        assert not any(secret in message for message in messages), secret
    for response in responses[:5]:
        for secret in (PHONE, WRONG_PASSWORD, NEW_PASSWORD):
            assert secret not in response.text


def test_the_default_configuration_comes_from_the_settings(settings) -> None:
    config = customer_router.get_service_config(settings)

    assert isinstance(config, ServiceConfig)
    assert config.token_ttl_seconds > 0
    assert config.signin_lock_attempts > 0
