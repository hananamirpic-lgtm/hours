"""`GET /staff/parts/active`: the read-only list of active parts for every ticket staff role (task 30.13).

A technician chooses the replaced parts from this list when resolving a ticket (R29.4), while the full list
`GET /staff/parts` stays for configuration staff (R29.6). The application mounts only `staff_router.router`,
as in `test_service_api_settlements_parts.py`, with `get_db` overridden to give each request a session of its
own on the in-memory database. The flag-off case builds the real application. SQLite cannot number the
identity column `seq`, so the autouse fixture `numbered` assigns it on insert.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import create_access_token
from app.db.session import get_db
from app.main import create_app
from app.models.change_log import ChangeLog
from app.models.user import UserRole
from app.service_tickets.api import staff_router
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.grammars import fold_key
from app.service_tickets.models import Customer, Part

URL = "/api/service/staff/parts/active"
FULL_URL = "/api/service/staff/parts"
TICKET_STAFF_ROLES = [UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.TECHNICIAN]


@pytest.fixture(autouse=True)
def numbered() -> Iterator[None]:
    """Number `seq` on insert, as PostgreSQL's identity column does."""
    counter = itertools.count(1)

    def assign(_mapper, _connection, target) -> None:
        if target.seq is None:
            target.seq = next(counter)

    event.listen(Part, "before_insert", assign)
    try:
        yield
    finally:
        event.remove(Part, "before_insert", assign)


def _build_app(session: Session) -> FastAPI:
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True)

    def request_session() -> Iterator[Session]:
        with factory() as per_request:
            yield per_request

    app = FastAPI()
    app.include_router(staff_router.router, prefix="/api/service")
    app.dependency_overrides[get_db] = request_session
    return app


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    with TestClient(_build_app(session), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def staff(make_user):
    """`staff(role)` is the bearer header of a committed, fully enrolled user of that role."""

    def _staff(role: UserRole) -> dict[str, str]:
        user = make_user(role=role, is_2fa_enabled=True, must_change_password=False)
        token = create_access_token(user_id=user.id, token_version=user.token_version, role=role.value)
        return {"Authorization": f"Bearer {token}"}

    return _staff


def _part(session: Session, name_he: str, name_en: str, *, active: bool = True) -> Part:
    row = Part(
        name_he=name_he,
        name_en=name_en,
        name_he_key=fold_key(name_he),
        name_en_key=fold_key(name_en),
        is_active=active,
    )
    session.add(row)
    session.commit()
    return row


def _code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


@pytest.fixture
def parts(session: Session) -> list[Part]:
    """Four parts in creation order; the second and fourth are inactive."""
    return [
        _part(session, "אטם", "Seal"),
        _part(session, "ברז", "Valve", active=False),
        _part(session, "גלגל", "Wheel"),
        _part(session, "דוד", "Boiler", active=False),
    ]


# --- success ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", TICKET_STAFF_ROLES)
def test_every_ticket_staff_role_gets_the_active_parts(
    client: TestClient, staff, parts: list[Part], role: UserRole
) -> None:
    response = client.get(URL, headers=staff(role))

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"id": str(parts[0].id), "name_he": "אטם", "name_en": "Seal", "is_active": True},
            {"id": str(parts[2].id), "name_he": "גלגל", "name_en": "Wheel", "is_active": True},
        ]
    }


def test_all_three_roles_get_the_same_body(client: TestClient, staff, parts: list[Part]) -> None:
    bodies = [client.get(URL, headers=staff(role)).json() for role in TICKET_STAFF_ROLES]

    assert bodies[0] == bodies[1] == bodies[2]
    assert len(bodies[0]["items"]) == 2


def test_inactive_parts_are_left_out_and_a_reactivated_one_comes_back(
    client: TestClient, session: Session, staff, parts: list[Part]
) -> None:
    auth = staff(UserRole.TECHNICIAN)
    ids = [item["id"] for item in client.get(URL, headers=auth).json()["items"]]
    assert str(parts[1].id) not in ids
    assert str(parts[3].id) not in ids

    parts[1].is_active = True
    session.add(parts[1])
    session.commit()

    ids = [item["id"] for item in client.get(URL, headers=auth).json()["items"]]
    assert ids == [str(parts[0].id), str(parts[1].id), str(parts[2].id)]


def test_order_is_oldest_first_by_seq_not_by_name(client: TestClient, session: Session, staff) -> None:
    created = [
        _part(session, "תקע", "Plug"),
        _part(session, "אטם", "Seal"),
        _part(session, "מסנן", "Filter"),
    ]

    items = client.get(URL, headers=staff(UserRole.OPERATIONS_ADMIN)).json()["items"]

    assert [item["id"] for item in items] == [str(row.id) for row in created]


def test_each_item_has_exactly_the_four_fields(client: TestClient, staff, parts: list[Part]) -> None:
    items = client.get(URL, headers=staff(UserRole.TECHNICIAN)).json()["items"]

    assert items
    for item in items:
        assert set(item) == {"id", "name_he", "name_en", "is_active"}
        assert item["is_active"] is True


def test_no_parts_gives_an_empty_list(client: TestClient, staff) -> None:
    response = client.get(URL, headers=staff(UserRole.TECHNICIAN))

    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_only_inactive_parts_give_an_empty_list(client: TestClient, session: Session, staff) -> None:
    _part(session, "ברז", "Valve", active=False)

    assert client.get(URL, headers=staff(UserRole.ADMIN)).json() == {"items": []}


def test_the_read_changes_nothing_and_writes_no_audit_row(
    client: TestClient, session: Session, staff, parts: list[Part]
) -> None:
    def snapshot() -> tuple[list[tuple[Any, ...]], int]:
        session.rollback()
        session.expire_all()
        rows = session.execute(select(Part).order_by(Part.seq)).scalars().all()
        count = session.execute(select(func.count()).select_from(ChangeLog)).scalar_one()
        return [(r.id, r.name_he, r.name_en, r.is_active) for r in rows], count

    before = snapshot()

    assert client.get(URL, headers=staff(UserRole.TECHNICIAN)).status_code == 200

    assert snapshot() == before


# --- routing ----------------------------------------------------------------------------------------------


def test_active_is_not_captured_by_a_part_id_route(client: TestClient, staff, parts: list[Part]) -> None:
    """Only PUT and DELETE take `/parts/{part_id}`; a GET on `/parts/active` reaches its own handler."""
    auth = staff(UserRole.TECHNICIAN)

    assert client.get(URL, headers=auth).status_code == 200
    # a verb the path has no route for is 405 (POST /parts/{part_id} does not exist), and the PUT that
    # does match `/parts/{part_id}` stays configuration staff only
    assert client.post(URL, headers=auth).status_code == 405
    put = client.put(URL, json={}, headers=auth)
    assert (put.status_code, _code(put)) == (403, "insufficient_role")


# --- access -----------------------------------------------------------------------------------------------


def test_the_full_list_stays_configuration_staff_only(client: TestClient, staff, parts: list[Part]) -> None:
    response = client.get(FULL_URL, headers=staff(UserRole.TECHNICIAN))

    assert (response.status_code, _code(response)) == (403, "insufficient_role")
    for role in (UserRole.ADMIN, UserRole.OPERATIONS_ADMIN):
        full = client.get(FULL_URL, headers=staff(role))
        assert full.status_code == 200
        assert len(full.json()["items"]) == 4


def test_a_customer_token_is_refused(client: TestClient, session: Session, parts: list[Part]) -> None:
    customer = Customer(phone="0509998877", full_name="C", full_name_key="c", password_hash="x")
    session.add(customer)
    session.commit()
    header = {"Authorization": f"Bearer {mint_customer_token(customer.id, customer.token_version)}"}

    response = client.get(URL, headers=header)

    assert (response.status_code, _code(response)) == (403, "customer_token_not_accepted")


def test_no_token_is_401(client: TestClient, parts: list[Part]) -> None:
    response = client.get(URL)

    assert (response.status_code, _code(response)) == (401, "not_authenticated")


def test_the_accounting_role_is_refused(client: TestClient, staff, parts: list[Part]) -> None:
    response = client.get(URL, headers=staff(UserRole.ACCOUNTING))

    assert (response.status_code, _code(response)) == (403, "insufficient_role")


def test_flag_off_the_route_is_an_unknown_path(settings) -> None:
    app = create_app(settings.model_copy(update={"service_tickets_enabled": ""}))
    with TestClient(app, raise_server_exceptions=False) as off:
        response = off.get(URL)
        unknown = off.get("/api/no-such-path")

    assert response.status_code == 404
    assert (response.status_code, response.json()) == (unknown.status_code, unknown.json())
