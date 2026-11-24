"""Staff routes for the settlement and replaceable-parts lists (task 30.2; R30.1, R29.6, R1.10, R1.11).

`/staff/localities` and `/staff/parts` have the same six routes (list, create, rename, deactivate,
reactivate, delete), so each rule is checked for both with one parametrised test. They work as the category
routes of `test_service_api_staff_config.py` with three differences, each tested here: every route, the
list included, is for configuration staff only (a technician gets 403); there is no "last active" rule; and
the codes are the list's own (`duplicate_settlement`, `part_not_found`, `invalid_part_name_en`, ...).

The application mounts only `staff_router.router`, as in the categories' API tests, with `get_db`
overridden to give each request a session of its own on the in-memory database. The flag-off case builds
the real application. SQLite cannot number the identity column `seq`, so the autouse fixture `numbered`
assigns it on insert; `foreign_keys_on` turns on SQLite's foreign key check for the delete-a-used-row tests.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from test_service_named_lists_settlements_parts import (
    _used_by_customer,
    _used_by_resolved_ticket,
    _used_by_ticket,
)

from app.core.security import create_access_token
from app.db.session import get_db
from app.main import create_app
from app.models.user import User, UserRole
from app.service_tickets.api import staff_router
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.grammars import fold_key
from app.service_tickets.models import Category, Customer, Locality, Part

PREFIX = "/api/service/staff"


@pytest.fixture(autouse=True)
def numbered() -> Iterator[None]:
    """Number `seq` on insert, as PostgreSQL's identity column does."""
    counter = itertools.count(1)

    def assign(_mapper, _connection, target) -> None:
        if target.seq is None:
            target.seq = next(counter)

    models = (Category, Locality, Part)
    for model in models:
        event.listen(model, "before_insert", assign)
    try:
        yield
    finally:
        for model in models:
            event.remove(model, "before_insert", assign)


@pytest.fixture
def foreign_keys_on(sqlite_engine: Engine) -> None:
    with sqlite_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


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
    """`staff(role)` is a committed, fully enrolled user and its bearer header."""

    def _staff(role: UserRole = UserRole.OPERATIONS_ADMIN) -> tuple[User, dict[str, str]]:
        user = make_user(role=role, is_2fa_enabled=True, must_change_password=False)
        token = create_access_token(user_id=user.id, token_version=user.token_version, role=role.value)
        return user, {"Authorization": f"Bearer {token}"}

    return _staff


@pytest.fixture
def headers(staff) -> dict[str, str]:
    return staff(UserRole.OPERATIONS_ADMIN)[1]


@pytest.fixture
def tech_headers(staff) -> dict[str, str]:
    return staff(UserRole.TECHNICIAN)[1]


# --- the two resources ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Resource:
    path: str  # under /api/service/staff
    model: type
    limit: int
    invalid_he: str
    invalid_en: str
    duplicate: str
    not_found: str
    uses: tuple[Callable[[Session, uuid.UUID], None], ...]

    def __str__(self) -> str:
        return self.path


LOCALITIES = Resource(
    "localities",
    Locality,
    100,
    "invalid_settlement_name_he",
    "invalid_settlement_name_en",
    "duplicate_settlement",
    "settlement_not_found",
    (_used_by_ticket, _used_by_customer),
)
PARTS = Resource(
    "parts",
    Part,
    60,
    "invalid_part_name_he",
    "invalid_part_name_en",
    "duplicate_part",
    "part_not_found",
    (_used_by_resolved_ticket,),
)
both = pytest.mark.parametrize("res", [LOCALITIES, PARTS], ids=str)


def _row(session: Session, res: Resource, name_he: str, name_en: str, *, active: bool = True):
    row = res.model(
        name_he=name_he,
        name_en=name_en,
        name_he_key=fold_key(name_he),
        name_en_key=fold_key(name_en),
        is_active=active,
    )
    session.add(row)
    session.commit()
    return row


def _three(session: Session, res: Resource):
    return (
        _row(session, res, "חיפה", "Haifa"),
        _row(session, res, "עכו", "Akko"),
        _row(session, res, "נהריה", "Nahariya"),
    )


def _code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


def _fields(response: Any) -> dict[str, str]:
    return response.json()["detail"]["error"]["fields"]


def _stored(session: Session, res: Resource) -> list[tuple[Any, ...]]:
    session.rollback()
    session.expire_all()
    rows = session.execute(select(res.model).order_by(res.model.seq)).scalars().all()
    return [(r.id, r.name_he, r.name_en, r.is_active) for r in rows]


def _body(**overrides: Any) -> dict[str, Any]:
    body = {"name_he": "קירות", "name_en": "Walls"}
    body.update(overrides)
    return body


def _routes(res: Resource, item_id: uuid.UUID | str) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Every route of the resource with a body that would succeed for configuration staff."""
    base = f"/{res.path}"
    return [
        ("GET", base, None),
        ("POST", base, _body()),
        ("PUT", f"{base}/{item_id}", _body()),
        ("POST", f"{base}/{item_id}/deactivate", None),
        ("POST", f"{base}/{item_id}/reactivate", None),
        ("DELETE", f"{base}/{item_id}", None),
    ]


# --- access -----------------------------------------------------------------------------------------------


@both
def test_technician_gets_403_on_every_route_the_list_included_and_nothing_changes(
    client: TestClient, session: Session, tech_headers, res: Resource
) -> None:
    first, _, _ = _three(session, res)
    before = _stored(session, res)

    for method, path, body in _routes(res, first.id):
        response = client.request(method, PREFIX + path, json=body, headers=tech_headers)
        assert (response.status_code, _code(response)) == (403, "insufficient_role"), (method, path)
        assert "fields" not in response.json()["detail"]["error"], (method, path)

    assert _stored(session, res) == before


@both
def test_technician_is_refused_before_the_body_or_the_id_is_looked_at(
    client: TestClient, session: Session, tech_headers, res: Resource
) -> None:
    _three(session, res)
    before = _stored(session, res)

    bad_body = client.post(f"{PREFIX}/{res.path}", json={"name_he": " ", "bogus": 1}, headers=tech_headers)
    unknown_id = client.delete(f"{PREFIX}/{res.path}/{uuid.uuid4()}", headers=tech_headers)
    malformed_id = client.put(f"{PREFIX}/{res.path}/nope", json={}, headers=tech_headers)

    for response in (bad_body, unknown_id, malformed_id):
        assert (response.status_code, _code(response)) == (403, "insufficient_role")
    assert _stored(session, res) == before


@both
def test_every_route_needs_a_staff_token_and_a_configuration_role(
    client: TestClient, session: Session, staff, res: Resource
) -> None:
    first, _, _ = _three(session, res)
    customer = Customer(phone="0509998877", full_name="C", full_name_key="c", password_hash="x")
    session.add(customer)
    session.commit()
    as_customer = {"Authorization": f"Bearer {mint_customer_token(customer.id, customer.token_version)}"}
    before = _stored(session, res)

    for method, path, body in _routes(res, first.id):
        anonymous = client.request(method, PREFIX + path, json=body)
        assert (anonymous.status_code, _code(anonymous)) == (401, "not_authenticated"), (method, path)
        customer_response = client.request(method, PREFIX + path, json=body, headers=as_customer)
        assert (customer_response.status_code, _code(customer_response)) == (
            403,
            "customer_token_not_accepted",
        ), (method, path)
        accounting = client.request(method, PREFIX + path, json=body, headers=staff(UserRole.ACCOUNTING)[1])
        assert (accounting.status_code, _code(accounting)) == (403, "insufficient_role"), (method, path)

    assert _stored(session, res) == before


@both
@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.OPERATIONS_ADMIN])
def test_both_configuration_roles_may_use_the_routes(
    client: TestClient, session: Session, staff, res: Resource, role: UserRole
) -> None:
    first, _, _ = _three(session, res)
    auth = staff(role)[1]

    assert client.get(f"{PREFIX}/{res.path}", headers=auth).status_code == 200
    assert client.post(f"{PREFIX}/{res.path}", json=_body(), headers=auth).status_code == 201
    assert (
        client.put(
            f"{PREFIX}/{res.path}/{first.id}", json=_body(name_he="אחר", name_en="Other"), headers=auth
        ).status_code
        == 200
    )
    assert client.post(f"{PREFIX}/{res.path}/{first.id}/deactivate", headers=auth).status_code == 200
    assert client.post(f"{PREFIX}/{res.path}/{first.id}/reactivate", headers=auth).status_code == 200
    assert client.delete(f"{PREFIX}/{res.path}/{first.id}", headers=auth).status_code == 200


@both
def test_admin_and_operations_admin_get_equal_responses(
    client: TestClient, session: Session, staff, res: Resource
) -> None:
    first, _, _ = _three(session, res)
    requests: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", f"/{res.path}", None),
        ("PUT", f"/{res.path}/{first.id}", {"name_he": "חיפה", "name_en": "Haifa"}),
        ("POST", f"/{res.path}/{first.id}/reactivate", None),
        ("POST", f"/{res.path}", {"name_he": "חיפה", "name_en": "Brand new"}),
        ("POST", f"/{res.path}", {"name_he": " ", "name_en": "x" * 101}),
        ("PUT", f"/{res.path}/{uuid.UUID(int=7)}", _body()),
        ("DELETE", f"/{res.path}/{uuid.UUID(int=7)}", None),
    ]

    def run(role: UserRole) -> list[tuple[int, Any]]:
        auth = staff(role)[1]
        responses = [client.request(m, PREFIX + p, json=b, headers=auth) for m, p, b in requests]
        return [(response.status_code, response.json()) for response in responses]

    admin = run(UserRole.ADMIN)
    operations_admin = run(UserRole.OPERATIONS_ADMIN)

    assert admin == operations_admin
    assert [status for status, _ in admin] == [200, 200, 200, 409, 422, 404, 404]


@both
def test_flag_off_every_route_is_an_unknown_path(settings, res: Resource) -> None:
    app = create_app(settings.model_copy(update={"service_tickets_enabled": ""}))
    with TestClient(app, raise_server_exceptions=False) as off:
        item = uuid.uuid4()
        for method, path, body in _routes(res, item):
            response = off.request(method, PREFIX + path, json=body)
            unknown = off.request(method, "/api/no-such-path", json=body)
            assert response.status_code == 404, (method, path)
            assert (response.status_code, response.json()) == (unknown.status_code, unknown.json())


# --- list -------------------------------------------------------------------------------------------------


@both
def test_a_new_database_lists_nothing(client: TestClient, headers, res: Resource) -> None:
    response = client.get(f"{PREFIX}/{res.path}", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"items": []}


@both
def test_list_is_oldest_first_with_inactive_items_and_exact_fields(
    client: TestClient, session: Session, headers, res: Resource
) -> None:
    first, second, third = _three(session, res)
    client.post(f"{PREFIX}/{res.path}/{second.id}/deactivate", headers=headers)
    client.put(f"{PREFIX}/{res.path}/{first.id}", json=_body(name_he="זזז", name_en="Zeta"), headers=headers)

    response = client.get(f"{PREFIX}/{res.path}", headers=headers)

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == [str(first.id), str(second.id), str(third.id)]
    assert [item["is_active"] for item in items] == [True, False, True]
    assert all(set(item) == {"id", "name_he", "name_en", "is_active"} for item in items)
    assert (items[0]["name_he"], items[0]["name_en"]) == ("זזז", "Zeta")


# --- create -----------------------------------------------------------------------------------------------


@both
def test_create_is_201_trims_both_names_and_adds_an_active_item_at_the_end(
    client: TestClient, session: Session, headers, res: Resource
) -> None:
    first, _, _ = _three(session, res)

    response = client.post(
        f"{PREFIX}/{res.path}", json={"name_he": "  קירות ", "name_en": "\tWalls "}, headers=headers
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["name_he"], body["name_en"], body["is_active"]) == ("קירות", "Walls", True)
    assert set(body) == {"id", "name_he", "name_en", "is_active"}
    assert [r[1] for r in _stored(session, res)][-1] == "קירות"
    assert _stored(session, res)[0][0] == first.id


@both
def test_create_accepts_the_limit_and_refuses_one_more(
    client: TestClient, session: Session, headers, res: Resource
) -> None:
    ok = client.post(
        f"{PREFIX}/{res.path}", json={"name_he": "א" * res.limit, "name_en": "x" * res.limit}, headers=headers
    )
    assert ok.status_code == 201

    over = client.post(
        f"{PREFIX}/{res.path}",
        json={"name_he": "ב" * (res.limit + 1), "name_en": "y" * (res.limit + 1)},
        headers=headers,
    )

    assert (over.status_code, _code(over)) == (422, res.invalid_he)
    assert list(_fields(over).items()) == [("name_he", res.invalid_he), ("name_en", res.invalid_en)]
    assert len(_stored(session, res)) == 1


@both
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"name_he": "", "name_en": "ok"}, {"name_he": "he"}),
        ({"name_he": "ok", "name_en": "   "}, {"name_en": "en"}),
        ({"name_he": " ", "name_en": ""}, {"name_he": "he", "name_en": "en"}),
    ],
)
def test_create_with_an_invalid_name_gives_the_lists_codes_per_language_and_stores_nothing(
    client: TestClient,
    session: Session,
    headers,
    res: Resource,
    body: dict[str, str],
    expected: dict[str, str],
) -> None:
    codes = {"he": res.invalid_he, "en": res.invalid_en}

    response = client.post(f"{PREFIX}/{res.path}", json=body, headers=headers)

    assert response.status_code == 422
    assert _fields(response) == {field: codes[kind] for field, kind in expected.items()}
    assert _code(response) == next(iter(_fields(response).values()))
    assert _stored(session, res) == []


@both
def test_create_with_a_missing_or_unknown_field_is_422_and_stores_nothing(
    client: TestClient, session: Session, headers, res: Resource
) -> None:
    missing = client.post(f"{PREFIX}/{res.path}", json={"name_he": "קירות"}, headers=headers)
    not_text = client.post(f"{PREFIX}/{res.path}", json={"name_he": 7, "name_en": None}, headers=headers)
    unknown = client.post(f"{PREFIX}/{res.path}", json=_body(is_active=False), headers=headers)

    # A missing or non-string name gets the list's own code, not the category code of the field name.
    assert (missing.status_code, _code(missing)) == (422, res.invalid_en)
    assert _fields(missing) == {"name_en": res.invalid_en}
    assert (not_text.status_code, _code(not_text)) == (422, res.invalid_he)
    assert _fields(not_text) == {"name_he": res.invalid_he, "name_en": res.invalid_en}
    assert (unknown.status_code, _code(unknown)) == (422, "unknown_field")
    assert _stored(session, res) == []


@both
@pytest.mark.parametrize(
    ("body", "clashing"),
    [
        ({"name_he": "חדש", "name_en": "haifa"}, ["name_en"]),
        ({"name_he": "  חיפה ", "name_en": "Other"}, ["name_he"]),
        ({"name_he": "חיפה", "name_en": "HAIFA "}, ["name_he", "name_en"]),
    ],
)
def test_create_of_a_duplicate_is_409_with_the_clashing_languages(
    client: TestClient, session: Session, headers, res: Resource, body: dict[str, str], clashing: list[str]
) -> None:
    _row(session, res, "חיפה", "Haifa")
    before = _stored(session, res)

    response = client.post(f"{PREFIX}/{res.path}", json=body, headers=headers)

    assert (response.status_code, _code(response)) == (409, res.duplicate)
    assert _fields(response) == dict.fromkeys(clashing, res.duplicate)
    assert _stored(session, res) == before


@both
def test_create_of_a_duplicate_ignores_case_beyond_ascii_and_checks_inactive_items(
    client: TestClient, session: Session, headers, res: Resource
) -> None:
    _row(session, res, "שם", "Привет", active=False)

    response = client.post(
        f"{PREFIX}/{res.path}", json={"name_he": "אחר", "name_en": "пРиВеТ"}, headers=headers
    )

    assert (response.status_code, _code(response)) == (409, res.duplicate)
    assert _fields(response) == {"name_en": res.duplicate}


@both
def test_create_reports_a_validation_failure_before_a_duplicate(
    client: TestClient, session: Session, headers, res: Resource
) -> None:
    _row(session, res, "חיפה", "Haifa")

    response = client.post(f"{PREFIX}/{res.path}", json={"name_he": "חיפה", "name_en": ""}, headers=headers)

    assert (response.status_code, _code(response)) == (422, res.invalid_en)


# --- rename -----------------------------------------------------------------------------------------------


@both
def test_rename_sets_both_names_and_keeps_the_id_and_the_order(
    client: TestClient, session: Session, headers, res: Resource
) -> None:
    first, second, third = _three(session, res)

    response = client.put(
        f"{PREFIX}/{res.path}/{second.id}",
        json={"name_he": " עכו העתיקה ", "name_en": " Old Akko "},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(second.id),
        "name_he": "עכו העתיקה",
        "name_en": "Old Akko",
        "is_active": True,
    }
    assert [r[0] for r in _stored(session, res)] == [first.id, second.id, third.id]


@both
def test_rename_to_its_own_names_in_another_case_is_allowed(
    client: TestClient, session: Session, headers, res: Resource
) -> None:
    first, _, _ = _three(session, res)

    response = client.put(
        f"{PREFIX}/{res.path}/{first.id}", json={"name_he": "חיפה", "name_en": "HAIFA"}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["name_en"] == "HAIFA"


@both
def test_rename_into_another_items_name_is_409_and_changes_nothing(
    client: TestClient, session: Session, headers, res: Resource
) -> None:
    first, _, _ = _three(session, res)
    before = _stored(session, res)

    response = client.put(
        f"{PREFIX}/{res.path}/{first.id}", json={"name_he": "עכו", "name_en": "akko"}, headers=headers
    )

    assert (response.status_code, _code(response)) == (409, res.duplicate)
    assert _fields(response) == {"name_he": res.duplicate, "name_en": res.duplicate}
    assert _stored(session, res) == before


@both
def test_rename_with_an_invalid_name_is_422_and_changes_neither_language(
    client: TestClient, session: Session, headers, res: Resource
) -> None:
    first, _, _ = _three(session, res)
    before = _stored(session, res)

    response = client.put(
        f"{PREFIX}/{res.path}/{first.id}",
        json={"name_he": "תקין", "name_en": "x" * (res.limit + 1)},
        headers=headers,
    )

    assert (response.status_code, _code(response)) == (422, res.invalid_en)
    assert _fields(response) == {"name_en": res.invalid_en}
    assert _stored(session, res) == before


@both
def test_rename_of_an_unknown_or_malformed_id_is_404_with_the_lists_code(
    client: TestClient, session: Session, headers, res: Resource
) -> None:
    _three(session, res)
    before = _stored(session, res)

    for item_id in (uuid.uuid4(), "nope", "123"):
        response = client.put(f"{PREFIX}/{res.path}/{item_id}", json=_body(), headers=headers)
        assert (response.status_code, _code(response)) == (404, res.not_found), item_id

    assert _stored(session, res) == before


@both
def test_validation_comes_before_the_lookup(client: TestClient, headers, res: Resource) -> None:
    """Both wrong: the invalid name is reported, as for categories."""
    response = client.put(
        f"{PREFIX}/{res.path}/{uuid.uuid4()}", json={"name_he": "", "name_en": "ok"}, headers=headers
    )

    assert (response.status_code, _code(response)) == (422, res.invalid_he)


# --- deactivate and reactivate (no "last active" rule) ----------------------------------------------------


@both
def test_deactivate_and_reactivate_flip_the_flag_and_return_the_record(
    client: TestClient, session: Session, headers, res: Resource
) -> None:
    first, _, _ = _three(session, res)

    off = client.post(f"{PREFIX}/{res.path}/{first.id}/deactivate", headers=headers)
    assert off.status_code == 200 and off.json()["is_active"] is False
    again = client.post(f"{PREFIX}/{res.path}/{first.id}/deactivate", headers=headers)
    assert again.status_code == 200 and again.json()["is_active"] is False

    on = client.post(f"{PREFIX}/{res.path}/{first.id}/reactivate", headers=headers)
    assert on.status_code == 200 and on.json()["is_active"] is True
    assert on.json()["id"] == str(first.id)


@both
def test_every_item_can_be_deactivated_and_the_last_one_can_be_deleted(
    client: TestClient, session: Session, headers, res: Resource
) -> None:
    items = _three(session, res)

    for item in items:
        assert client.post(f"{PREFIX}/{res.path}/{item.id}/deactivate", headers=headers).status_code == 200
    assert [r[3] for r in _stored(session, res)] == [False, False, False]

    for item in items:
        response = client.delete(f"{PREFIX}/{res.path}/{item.id}", headers=headers)
        assert (response.status_code, response.json()) == (200, {"result": "removed"})
    assert _stored(session, res) == []


@both
def test_deactivate_and_reactivate_of_an_unknown_id_are_404(
    client: TestClient, headers, res: Resource
) -> None:
    for action in ("deactivate", "reactivate"):
        for item_id in (uuid.uuid4(), "nope"):
            response = client.post(f"{PREFIX}/{res.path}/{item_id}/{action}", headers=headers)
            assert (response.status_code, _code(response)) == (404, res.not_found), (action, item_id)


# --- delete -----------------------------------------------------------------------------------------------


@both
def test_delete_of_an_unused_item_removes_it(
    client: TestClient, session: Session, headers, res: Resource
) -> None:
    first, second, third = _three(session, res)

    response = client.delete(f"{PREFIX}/{res.path}/{second.id}", headers=headers)

    assert (response.status_code, response.json()) == (200, {"result": "removed"})
    assert [r[0] for r in _stored(session, res)] == [first.id, third.id]


@both
def test_delete_of_a_used_item_deactivates_it_and_keeps_it_listed(
    client: TestClient, session: Session, headers, res: Resource, foreign_keys_on: None
) -> None:
    for index, use in enumerate(res.uses):
        item = _row(session, res, f"משומש {index}", f"Used {index}")
        use(session, item.id)

        response = client.delete(f"{PREFIX}/{res.path}/{item.id}", headers=headers)

        assert (response.status_code, response.json()) == (200, {"result": "deactivated"})
        listed = {i["id"]: i for i in client.get(f"{PREFIX}/{res.path}", headers=headers).json()["items"]}
        assert listed[str(item.id)]["is_active"] is False
        assert listed[str(item.id)]["name_en"] == f"Used {index}"


@both
def test_delete_of_an_unknown_or_malformed_id_is_404(client: TestClient, headers, res: Resource) -> None:
    for item_id in (uuid.uuid4(), "nope"):
        response = client.delete(f"{PREFIX}/{res.path}/{item_id}", headers=headers)
        assert (response.status_code, _code(response)) == (404, res.not_found), item_id


# --- the lists do not interfere with each other or with categories ----------------------------------------


def test_an_id_of_one_list_is_not_found_in_the_other(client: TestClient, session: Session, headers) -> None:
    locality = _row(session, LOCALITIES, "חיפה", "Haifa")
    part = _row(session, PARTS, "מסנן", "Filter")

    as_part = client.delete(f"{PREFIX}/parts/{locality.id}", headers=headers)
    as_locality = client.put(f"{PREFIX}/localities/{part.id}", json=_body(), headers=headers)

    assert (as_part.status_code, _code(as_part)) == (404, "part_not_found")
    assert (as_locality.status_code, _code(as_locality)) == (404, "settlement_not_found")
    assert len(_stored(session, LOCALITIES)) == len(_stored(session, PARTS)) == 1


def test_the_category_routes_keep_their_codes_and_the_last_active_rule(
    client: TestClient, session: Session, headers, tech_headers
) -> None:
    only = Category(
        name_he="כללי", name_en="General", name_he_key="כללי", name_en_key="general", is_active=True
    )
    session.add(only)
    session.commit()

    last = client.post(f"{PREFIX}/categories/{only.id}/deactivate", headers=headers)
    duplicate = client.post(f"{PREFIX}/categories", json={"name_he": "כללי", "name_en": "x"}, headers=headers)
    invalid = client.post(f"{PREFIX}/categories", json={"name_he": "", "name_en": "x" * 61}, headers=headers)
    missing = client.delete(f"{PREFIX}/categories/{uuid.uuid4()}", headers=headers)
    listing = client.get(f"{PREFIX}/categories", headers=tech_headers)  # still open to every ticket role

    assert (last.status_code, _code(last)) == (409, "last_active_category")
    assert (duplicate.status_code, _code(duplicate)) == (409, "duplicate_category")
    assert _fields(duplicate) == {"name_he": "duplicate_category"}
    assert (invalid.status_code, _code(invalid)) == (422, "invalid_name_he")
    assert _fields(invalid) == {"name_he": "invalid_name_he", "name_en": "invalid_name_en"}
    assert (missing.status_code, _code(missing)) == (404, "category_not_found")
    assert listing.status_code == 200
