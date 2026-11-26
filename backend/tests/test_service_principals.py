"""Principal dependencies of the service ticket API (R1.2 to R1.8, R1.14, R6.1, R20.13).

A small application mounts one route per dependency under `ServiceRoute` and answers with the
principal it got. Tokens are real: staff tokens come from `create_access_token`, customer tokens from
`mint_customer_token`. The database is the in-memory one of `conftest.py`, shared with the test.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import create_access_token, create_refresh_token
from app.db.session import get_db
from app.models.user import User, UserRole
from app.service_tickets.api import deps as service_deps
from app.service_tickets.api.route import ServiceRoute
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.models import Customer

_phones = itertools.count(1)


def _build_app(session: Session) -> FastAPI:
    """Each request gets its own session on the test database, as in production.

    `load_staff_identity` expunges the row it reads, so it must not run on the session that created the
    row: a shared session would hand back the test's own object without a query and detach it.
    """
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True)

    def request_session() -> Iterator[Session]:
        with factory() as per_request:
            yield per_request

    app = FastAPI()
    router = APIRouter(prefix="/p", route_class=ServiceRoute)

    @router.get("/staff")
    def staff(principal: service_deps.StaffPrincipalDep) -> dict[str, Any]:
        name = principal.display_name
        return {"user_id": str(principal.user_id), "name": name, "role": principal.role.value}

    @router.get("/config")
    def config(principal: service_deps.ConfigStaffPrincipalDep) -> dict[str, Any]:
        return {"role": principal.role.value}

    @router.get("/customer")
    def customer(principal: service_deps.CustomerPrincipalDep) -> dict[str, Any]:
        return {"customer_id": str(principal.customer_id), "phone": principal.phone}

    @router.get("/customer-ungated")
    def customer_ungated(principal: service_deps.CustomerPrincipalUngatedDep) -> dict[str, Any]:
        return {"customer_id": str(principal.customer_id), "must_change": principal.must_change_password}

    @router.get("/photo")
    def photo(principal: service_deps.PhotoPrincipalDep) -> dict[str, Any]:
        if isinstance(principal, service_deps.CustomerPrincipal):
            return {"kind": "customer", "id": str(principal.customer_id)}
        return {"kind": "staff", "id": str(principal.user_id), "role": principal.role.value}

    app.include_router(router)
    app.dependency_overrides[get_db] = request_session
    return app


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    with TestClient(_build_app(session)) as test_client:
        yield test_client


@pytest.fixture
def statements(session: Session) -> Iterator[list[str]]:
    engine: Engine = session.get_bind()  # type: ignore[assignment]
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.fixture
def make_customer(session: Session):
    def _make(**overrides: Any) -> Customer:
        fields: dict[str, Any] = {
            "phone": f"05{next(_phones):08d}",
            "full_name": "Dana Levi",
            "full_name_key": "dana levi",
            "password_hash": "$2b$12$notarealhashnotarealhashnotarealhashnotarealhashnotare",
            "must_change_password": False,
            "token_version": 1,
        }
        fields.update(overrides)
        created = Customer(**fields)
        session.add(created)
        session.commit()
        return created

    return _make


def _staff_headers(user: User, **kwargs: Any) -> dict[str, str]:
    version = kwargs.pop("version", user.token_version)
    role = kwargs.pop("role", user.role.value)
    token = create_access_token(user_id=user.id, token_version=version, role=role)
    return {"Authorization": f"Bearer {token}"}


def _customer_headers(
    customer: Customer, *, version: int | None = None, now: datetime | None = None
) -> dict[str, str]:
    token = mint_customer_token(customer.id, customer.token_version if version is None else version, now=now)
    return {"Authorization": f"Bearer {token}"}


def _code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


# --------------------------------------------------------------------------- staff_principal


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.TECHNICIAN])
def test_ticket_staff_roles_are_admitted(client: TestClient, make_user, role: UserRole) -> None:
    user = make_user(role=role, is_2fa_enabled=True, must_change_password=False)

    response = client.get("/p/staff", headers=_staff_headers(user))

    assert response.status_code == 200
    assert response.json() == {"user_id": str(user.id), "name": user.username, "role": role.value}


@pytest.mark.parametrize("role", [UserRole.SITE_MANAGER, UserRole.ACCOUNTING, UserRole.EMPLOYEE])
def test_other_hours_roles_are_insufficient_role(client: TestClient, make_user, role: UserRole) -> None:
    user = make_user(role=role, is_2fa_enabled=True, must_change_password=False)

    response = client.get("/p/staff", headers=_staff_headers(user))

    assert (response.status_code, _code(response)) == (403, "insufficient_role")


def test_no_header_and_a_bad_token_are_401(client: TestClient, make_user) -> None:
    missing = client.get("/p/staff")
    assert (missing.status_code, _code(missing)) == (401, "not_authenticated")
    assert missing.headers["www-authenticate"] == "Bearer"

    garbage = client.get("/p/staff", headers={"Authorization": "Bearer not-a-token"})
    assert (garbage.status_code, _code(garbage)) == (401, "invalid_token")

    user = make_user(role=UserRole.TECHNICIAN, must_change_password=False)
    refresh, _ = create_refresh_token(user_id=user.id, token_version=user.token_version)
    wrong_type = client.get("/p/staff", headers={"Authorization": f"Bearer {refresh}"})
    assert (wrong_type.status_code, _code(wrong_type)) == (401, "invalid_token")


def test_a_deactivated_or_revoked_technician_is_401_invalid_token(client: TestClient, make_user) -> None:
    inactive = make_user(role=UserRole.TECHNICIAN, is_active=False, must_change_password=False)
    revoked = make_user(role=UserRole.TECHNICIAN, token_version=2, must_change_password=False)

    for response in (
        client.get("/p/staff", headers=_staff_headers(inactive)),
        client.get("/p/staff", headers=_staff_headers(revoked, version=1)),
    ):
        assert (response.status_code, _code(response)) == (401, "invalid_token")


def test_a_valid_customer_token_is_403_but_an_expired_one_is_401(client: TestClient, make_customer) -> None:
    customer = make_customer()

    valid = client.get("/p/staff", headers=_customer_headers(customer))
    assert (valid.status_code, _code(valid)) == (403, "customer_token_not_accepted")

    long_ago = datetime.now(UTC) - timedelta(days=2)
    expired = client.get("/p/staff", headers=_customer_headers(customer, now=long_ago))
    assert (expired.status_code, _code(expired)) == (401, "invalid_token")


def test_the_role_comes_from_the_row_not_from_the_token_claim(client: TestClient, make_user) -> None:
    technician = make_user(role=UserRole.TECHNICIAN, must_change_password=False)

    response = client.get("/p/config", headers=_staff_headers(technician, role="admin"))

    assert (response.status_code, _code(response)) == (403, "insufficient_role")


def test_admin_without_2fa_is_blocked_until_enrolled(client: TestClient, make_user) -> None:
    owing = make_user(role=UserRole.ADMIN, is_2fa_enabled=False)
    enrolled = make_user(role=UserRole.ADMIN, is_2fa_enabled=True)

    blocked = client.get("/p/staff", headers=_staff_headers(owing))
    assert (blocked.status_code, _code(blocked)) == (403, "totp_enrolment_required")
    assert client.get("/p/staff", headers=_staff_headers(enrolled)).status_code == 200


def test_forced_password_change_blocks_non_admins_only(client: TestClient, make_user) -> None:
    for role in (UserRole.OPERATIONS_ADMIN, UserRole.TECHNICIAN):
        user = make_user(role=role, must_change_password=True)
        response = client.get("/p/staff", headers=_staff_headers(user))
        assert (response.status_code, _code(response)) == (403, "password_change_required")

    admin = make_user(role=UserRole.ADMIN, is_2fa_enabled=True, must_change_password=True)
    assert client.get("/p/staff", headers=_staff_headers(admin)).status_code == 200


def test_the_role_check_comes_before_the_forced_password_check(client: TestClient, make_user) -> None:
    site_manager = make_user(role=UserRole.SITE_MANAGER, must_change_password=True)

    response = client.get("/p/staff", headers=_staff_headers(site_manager))

    assert (response.status_code, _code(response)) == (403, "insufficient_role")


def test_the_enrolment_gate_comes_before_the_forced_password_gate(client: TestClient, make_user) -> None:
    admin = make_user(role=UserRole.ADMIN, is_2fa_enabled=False, must_change_password=True)

    response = client.get("/p/staff", headers=_staff_headers(admin))

    assert (response.status_code, _code(response)) == (403, "totp_enrolment_required")


# --------------------------------------------------------------------------- config_staff_principal


def test_config_staff_are_admins_but_not_technicians(client: TestClient, make_user) -> None:
    for role in (UserRole.ADMIN, UserRole.OPERATIONS_ADMIN):
        user = make_user(role=role, is_2fa_enabled=True, must_change_password=False)
        assert client.get("/p/config", headers=_staff_headers(user)).status_code == 200

    technician = make_user(role=UserRole.TECHNICIAN, must_change_password=False)
    response = client.get("/p/config", headers=_staff_headers(technician))
    assert (response.status_code, _code(response)) == (403, "insufficient_role")


def test_a_technician_who_owes_a_password_change_gets_that_code_not_the_config_one(
    client: TestClient, make_user
) -> None:
    technician = make_user(role=UserRole.TECHNICIAN, must_change_password=True)

    response = client.get("/p/config", headers=_staff_headers(technician))

    assert (response.status_code, _code(response)) == (403, "password_change_required")


# --------------------------------------------------------------------------- customer_principal


def test_an_active_current_customer_is_admitted(client: TestClient, make_customer) -> None:
    customer = make_customer()

    response = client.get("/p/customer", headers=_customer_headers(customer))

    assert response.status_code == 200
    assert response.json() == {"customer_id": str(customer.id), "phone": customer.phone}


def test_no_header_and_a_bad_token_are_401_for_customers(client: TestClient) -> None:
    missing = client.get("/p/customer")
    assert (missing.status_code, _code(missing)) == (401, "not_authenticated")

    garbage = client.get("/p/customer", headers={"Authorization": "Bearer nope"})
    assert (garbage.status_code, _code(garbage)) == (401, "invalid_token")


def test_a_staff_access_token_is_403_but_a_refresh_token_is_401(client: TestClient, make_user) -> None:
    user = make_user(role=UserRole.ADMIN, is_2fa_enabled=True)

    valid = client.get("/p/customer", headers=_staff_headers(user))
    assert (valid.status_code, _code(valid)) == (403, "staff_token_not_accepted")

    refresh, _ = create_refresh_token(user_id=user.id, token_version=user.token_version)
    wrong_type = client.get("/p/customer", headers={"Authorization": f"Bearer {refresh}"})
    assert (wrong_type.status_code, _code(wrong_type)) == (401, "invalid_token")

    expired = create_access_token(
        user_id=user.id, token_version=user.token_version, now=datetime.now(UTC) - timedelta(days=1)
    )
    old = client.get("/p/customer", headers={"Authorization": f"Bearer {expired}"})
    assert (old.status_code, _code(old)) == (401, "invalid_token")


def test_an_inactive_stale_or_unknown_customer_is_401(
    client: TestClient, make_customer, session: Session
) -> None:
    inactive = make_customer(is_active=False)
    current = make_customer(token_version=3)

    cases = [
        _customer_headers(inactive),
        _customer_headers(current, version=2),
        {"Authorization": f"Bearer {mint_customer_token(uuid.uuid4(), 1)}"},
    ]
    for headers in cases:
        response = client.get("/p/customer", headers=headers)
        assert (response.status_code, _code(response)) == (401, "invalid_token")

    # A bump on the row revokes a token that was good a moment ago.
    good = _customer_headers(current)
    assert client.get("/p/customer", headers=good).status_code == 200
    current.token_version = 4
    session.commit()
    assert client.get("/p/customer", headers=good).status_code == 401


def test_the_forced_password_gate_is_on_the_gated_dependency_only(client: TestClient, make_customer) -> None:
    customer = make_customer(must_change_password=True)
    headers = _customer_headers(customer)

    gated = client.get("/p/customer", headers=headers)
    assert (gated.status_code, _code(gated)) == (403, "password_change_required")

    ungated = client.get("/p/customer-ungated", headers=headers)
    assert ungated.status_code == 200
    assert ungated.json() == {"customer_id": str(customer.id), "must_change": True}


def test_the_staleness_check_comes_before_the_password_gate(client: TestClient, make_customer) -> None:
    customer = make_customer(must_change_password=True, token_version=2)

    response = client.get("/p/customer", headers=_customer_headers(customer, version=1))

    assert (response.status_code, _code(response)) == (401, "invalid_token")


# --------------------------------------------------------------------------- photo_principal


def test_photo_principal_admits_staff_and_customers(client: TestClient, make_user, make_customer) -> None:
    technician = make_user(role=UserRole.TECHNICIAN, must_change_password=False)
    customer = make_customer()

    staff = client.get("/p/photo", headers=_staff_headers(technician))
    assert staff.json() == {"kind": "staff", "id": str(technician.id), "role": "technician"}

    owner = client.get("/p/photo", headers=_customer_headers(customer))
    assert owner.json() == {"kind": "customer", "id": str(customer.id)}


def test_photo_principal_applies_each_kinds_own_gates(client: TestClient, make_user, make_customer) -> None:
    site_manager = make_user(role=UserRole.SITE_MANAGER, must_change_password=False)
    owing_staff = make_user(role=UserRole.TECHNICIAN, must_change_password=True)
    owing_customer = make_customer(must_change_password=True)

    for headers, code in (
        (_staff_headers(site_manager), "insufficient_role"),
        (_staff_headers(owing_staff), "password_change_required"),
        (_customer_headers(owing_customer), "password_change_required"),
    ):
        response = client.get("/p/photo", headers=headers)
        assert (response.status_code, _code(response)) == (403, code)


def test_photo_principal_401s_for_missing_revoked_and_inactive(
    client: TestClient, make_user, make_customer
) -> None:
    revoked_staff = make_user(role=UserRole.TECHNICIAN, is_active=False, must_change_password=False)
    inactive_customer = make_customer(is_active=False)

    missing = client.get("/p/photo")
    assert (missing.status_code, _code(missing)) == (401, "not_authenticated")
    for headers in (
        {"Authorization": "Bearer nope"},
        _staff_headers(revoked_staff),  # a staff token the route would accept, were it still good
        _customer_headers(inactive_customer),
    ):
        response = client.get("/p/photo", headers=headers)
        assert (response.status_code, _code(response)) == (401, "invalid_token")


# --------------------------------------------------------------------------- reads and writes


def test_a_staff_check_reads_one_users_row_and_writes_nothing(
    client: TestClient, make_user, statements: list[str]
) -> None:
    user = make_user(role=UserRole.TECHNICIAN, must_change_password=False)
    headers = _staff_headers(user)
    statements.clear()

    assert client.get("/p/staff", headers=headers).status_code == 200

    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("SELECT")
    assert "FROM users" in statements[0]
    assert "password_hash" not in statements[0]


def test_a_customer_check_reads_one_customer_row_and_never_the_hash(
    client: TestClient, make_customer, statements: list[str]
) -> None:
    customer = make_customer()
    headers = _customer_headers(customer)
    statements.clear()

    assert client.get("/p/customer", headers=headers).status_code == 200

    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("SELECT")
    assert "FROM svc_customers" in statements[0]
    assert "password_hash" not in statements[0]


def test_the_photo_check_reads_at_most_one_row_per_token_kind(
    client: TestClient, make_user, make_customer, statements: list[str]
) -> None:
    user = make_user(role=UserRole.TECHNICIAN, must_change_password=False)
    customer = make_customer()

    cases = ((_staff_headers(user), "FROM users"), (_customer_headers(customer), "FROM svc_customers"))
    for headers, table in cases:
        statements.clear()
        assert client.get("/p/photo", headers=headers).status_code == 200
        assert len(statements) == 1 and table in statements[0]


def test_a_failed_check_changes_no_stored_data(client: TestClient, make_user, session: Session) -> None:
    user = make_user(role=UserRole.TECHNICIAN, must_change_password=True)
    before = (user.token_version, user.failed_login_count, user.must_change_password, user.locked_until)

    assert client.get("/p/staff", headers=_staff_headers(user)).status_code == 403

    session.expire_all()
    stored = session.get(User, user.id)
    after = (stored.token_version, stored.failed_login_count, stored.must_change_password)
    after += (stored.locked_until,)
    assert after == before