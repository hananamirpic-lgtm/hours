"""Settlements in tickets and customers (task 30.3; R30.2 to R30.7; design.md, "Settlements (R30)").

The application is the real one (`create_app` with the flag on), so the customer submission, the staff
submission, the staff lists, the report, the customers routes and `GET /auth/me` are all the production
routes. Each request gets its own session on the in-memory test database, tokens are real, SQLite has no
sequence so the ticket-number allocator is an override, and the object store is the in-memory fake.

The two states of the settlement list drive most tests: an empty active list keeps the free-text city
(`locality_id` is refused), a non-empty one requires `locality_id` (`city` is refused). Every mismatch is
422 `invalid_settlement` under `fields.city`, the field the form shows it on.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker
from test_service_api_photo import FakePhotoStore, Numbers

from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.main import create_app
from app.models.user import UserRole
from app.service_tickets.api import customer_router
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.grammars import fold_key
from app.service_tickets.models import Category, Customer, Locality, Ticket
from app.service_tickets.photo_store import get_photo_store

API = "/api/service"
STAFF = f"{API}/staff"
INVALID_SETTLEMENT = {
    "detail": {"error": {"code": "invalid_settlement", "fields": {"city": "invalid_settlement"}}}
}
REF_KEYS = {"id", "name_he", "name_en"}


# --------------------------------------------------------------------------- harness


@pytest.fixture(autouse=True)
def numbered() -> Iterator[None]:
    """Number `Category.seq` on insert, as PostgreSQL's identity column does."""
    counter = itertools.count(1)

    def assign(_mapper: Any, _connection: Any, target: Category) -> None:
        if target.seq is None:
            target.seq = next(counter)

    event.listen(Category, "before_insert", assign)
    try:
        yield
    finally:
        event.remove(Category, "before_insert", assign)


@pytest.fixture
def numbers() -> Numbers:
    return Numbers()


@pytest.fixture
def photos() -> FakePhotoStore:
    return FakePhotoStore()


@pytest.fixture
def client(settings, session: Session, numbers: Numbers, photos: FakePhotoStore) -> Iterator[TestClient]:
    changes = {
        "service_tickets_enabled": "true",
        "service_accessibility_contact": "a@example.test",
        "api_prefix": "/api",
    }
    application: FastAPI = create_app(settings.model_copy(update=changes))
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True)

    def request_session() -> Iterator[Session]:
        with factory() as per_request:
            yield per_request

    application.dependency_overrides[get_db] = request_session
    application.dependency_overrides[customer_router.get_ticket_allocator] = lambda: numbers
    application.dependency_overrides[get_photo_store] = lambda: photos
    yield TestClient(application, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password("4321")


_phones = itertools.count(1)
_locality_seq = itertools.count(1)


@pytest.fixture
def make_customer(session: Session, password_hash: str):
    def _make(**overrides: Any) -> Customer:
        fields: dict[str, Any] = {
            "phone": f"05055{next(_phones):05d}",
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


@pytest.fixture
def customer(make_customer) -> Customer:
    return make_customer()


@pytest.fixture
def category(session: Session) -> Category:
    created = Category(
        name_he="Plumbing-he", name_en="Plumbing", name_he_key="plumbing-he", name_en_key="plumbing"
    )
    session.add(created)
    session.commit()
    return created


@pytest.fixture
def make_locality(session: Session):
    def _make(name: str, *, active: bool = True) -> Locality:
        created = Locality(
            seq=next(_locality_seq),
            name_he=f"{name}-he",
            name_en=name,
            name_he_key=f"{name}-he".casefold(),
            name_en_key=name.casefold(),
            is_active=active,
        )
        session.add(created)
        session.commit()
        return created

    return _make


@pytest.fixture
def staff(make_user):
    def _staff(role: UserRole = UserRole.TECHNICIAN) -> dict[str, str]:
        user = make_user(role=role, is_2fa_enabled=True, must_change_password=False)
        token = create_access_token(user_id=user.id, token_version=user.token_version, role=role.value)
        return {"Authorization": f"Bearer {token}"}

    return _staff


@pytest.fixture
def headers(staff) -> dict[str, str]:
    return staff(UserRole.OPERATIONS_ADMIN)


# --------------------------------------------------------------------------- helpers


def _customer_headers(owner: Customer) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_customer_token(owner.id, owner.token_version)}"}


def _fields(category: Category, **overrides: Any) -> list[tuple[str, Any]]:
    """The form without a city part; `overrides` adds or replaces parts (a `None` value drops one)."""
    base: dict[str, Any] = {
        "apartment": "12",
        "phone": "050-555-6677",
        "category_id": str(category.id),
        "description": "The kitchen tap leaks.",
        "urgent": "false",
    }
    base.update(overrides)
    return [(name, value) for name, value in base.items() if value is not None]


def _parts(items: list[tuple[str, Any]]) -> list[Any]:
    return [(name, (None, value)) for name, value in items]


def _submit(client: TestClient, owner: Customer, items: list[tuple[str, Any]]) -> Any:
    return client.post(f"{API}/tickets", files=_parts(items), headers=_customer_headers(owner))


def _open(client: TestClient, headers: dict[str, str], owner: Customer, items: list[tuple[str, Any]]) -> Any:
    return client.post(
        f"{STAFF}/tickets", files=_parts([("customer_id", str(owner.id)), *items]), headers=headers
    )


def _stored(session: Session, number: int) -> Ticket:
    session.rollback()
    return session.execute(select(Ticket).where(Ticket.ticket_number == number)).scalar_one()


def _ticket_count(session: Session) -> int:
    session.rollback()
    return session.execute(select(func.count()).select_from(Ticket)).scalar_one()


def _ref(locality: Locality) -> dict[str, str]:
    return {"id": str(locality.id), "name_he": locality.name_he, "name_en": locality.name_en}


@contextmanager
def _counting(session: Session) -> Iterator[list[str]]:
    """The SQL statements the engine runs inside the block."""
    engine = session.get_bind()
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


# --------------------------------------------------------------------------- submission, customer path


@pytest.mark.parametrize("path", ["customer", "staff"])
def test_with_an_empty_list_the_city_text_is_stored_and_the_settlement_is_null(
    client: TestClient, session: Session, customer: Customer, category: Category, headers, path: str
) -> None:
    items = [("city", "  Haifa  "), *_fields(category)]

    response = (
        _submit(client, customer, items) if path == "customer" else _open(client, headers, customer, items)
    )

    assert response.status_code == 201
    number = response.json()["ticket_number"]
    stored = _stored(session, number)
    assert (stored.city, stored.city_key, stored.locality_id) == ("Haifa", "haifa", None)
    if path == "staff":
        assert response.json()["locality"] is None


@pytest.mark.parametrize("path", ["customer", "staff"])
@pytest.mark.parametrize("kind", ["inactive", "unknown", "malformed"])
def test_with_an_empty_list_a_locality_id_is_refused_and_nothing_is_stored(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    make_locality,
    numbers: Numbers,
    headers,
    path: str,
    kind: str,
) -> None:
    value = {
        "inactive": lambda: str(make_locality("Old", active=False).id),
        "unknown": lambda: str(uuid.uuid4()),
        "malformed": lambda: "not-a-uuid",
    }[kind]()
    items = [("city", "Haifa"), ("locality_id", value), *_fields(category)]

    response = (
        _submit(client, customer, items) if path == "customer" else _open(client, headers, customer, items)
    )

    assert response.status_code == 422
    assert response.json() == INVALID_SETTLEMENT
    assert _ticket_count(session) == 0
    assert numbers.calls == 0


@pytest.mark.parametrize("path", ["customer", "staff"])
def test_with_settlements_the_ticket_stores_the_settlement_and_its_hebrew_name(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    make_locality,
    headers,
    path: str,
) -> None:
    make_locality("Haifa")
    chosen = make_locality("Tel Aviv")
    items = [("locality_id", str(chosen.id).upper()), *_fields(category)]

    response = (
        _submit(client, customer, items) if path == "customer" else _open(client, headers, customer, items)
    )

    assert response.status_code == 201
    stored = _stored(session, response.json()["ticket_number"])
    assert stored.locality_id == chosen.id
    assert stored.city == "Tel Aviv-he"
    assert stored.city_key == fold_key("Tel Aviv-he")
    if path == "staff":
        assert response.json()["city"] == "Tel Aviv-he"
        assert response.json()["locality"] == _ref(chosen)
    # The customer record is not touched by a submission.
    session.rollback()
    assert session.execute(
        select(Customer.city, Customer.locality_id).where(Customer.id == customer.id)
    ).one() == (
        "Haifa",
        None,
    )


@pytest.mark.parametrize("path", ["customer", "staff"])
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param([], id="missing"),
        pytest.param([("city", "Haifa")], id="city-only"),
        pytest.param([("locality_id", "")], id="blank"),
        pytest.param([("locality_id", "not-a-uuid")], id="malformed"),
        pytest.param([("locality_id", "00000000-0000-0000-0000-000000000000")], id="nil"),
        pytest.param([("locality_id", "{}")], id="braces"),
    ],
)
def test_with_settlements_a_missing_or_bad_locality_id_is_refused(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    make_locality,
    numbers: Numbers,
    headers,
    path: str,
    extra: list[tuple[str, Any]],
) -> None:
    make_locality("Haifa")
    items = [*extra, *_fields(category)]

    response = (
        _submit(client, customer, items) if path == "customer" else _open(client, headers, customer, items)
    )

    assert response.status_code == 422
    assert response.json() == INVALID_SETTLEMENT
    assert _ticket_count(session) == 0
    assert numbers.calls == 0


@pytest.mark.parametrize("path", ["customer", "staff"])
def test_with_settlements_inactive_unknown_and_repeated_ids_and_a_city_beside_a_valid_id_are_refused(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    make_locality,
    headers,
    path: str,
) -> None:
    active = make_locality("Haifa")
    inactive = make_locality("Old", active=False)
    cases = [
        [("locality_id", str(inactive.id))],
        [("locality_id", str(uuid.uuid4()))],
        [("locality_id", str(active.id)), ("locality_id", str(active.id))],  # a repeated part
        [("locality_id", str(active.id)), ("city", "Haifa")],  # exactly one form is accepted
    ]
    for extra in cases:
        items = [*extra, *_fields(category)]
        response = (
            _submit(client, customer, items)
            if path == "customer"
            else _open(client, headers, customer, items)
        )
        assert response.status_code == 422, extra
        assert response.json() == INVALID_SETTLEMENT, extra
    assert _ticket_count(session) == 0


def test_the_settlement_check_keeps_the_place_of_the_city_check_in_the_field_order(
    client: TestClient, customer: Customer, category: Category, make_locality
) -> None:
    make_locality("Haifa")
    items = [*_fields(category, apartment="", phone="x")]

    response = _submit(client, customer, items)

    assert response.status_code == 422
    error = response.json()["detail"]["error"]
    assert list(error["fields"]) == ["city", "apartment", "phone"]
    assert error["fields"]["city"] == "invalid_settlement"
    assert error["code"] == "invalid_settlement"


def test_a_deactivated_settlement_is_refused_after_the_list_becomes_empty_again(
    client: TestClient, session: Session, customer: Customer, category: Category, make_locality
) -> None:
    only = make_locality("Haifa")
    assert _submit(client, customer, [("locality_id", str(only.id)), *_fields(category)]).status_code == 201

    only.is_active = False
    session.commit()

    refused = _submit(client, customer, [("locality_id", str(only.id)), *_fields(category)])
    free_text = _submit(client, customer, [("city", "Haifa"), *_fields(category)])
    assert refused.status_code == 422
    assert refused.json() == INVALID_SETTLEMENT
    assert free_text.status_code == 201


# --------------------------------------------------------------------------- ticket responses


def test_the_locality_object_shows_the_current_names_also_after_a_rename_and_a_deactivation(
    client: TestClient, session: Session, customer: Customer, category: Category, make_locality, headers
) -> None:
    chosen = make_locality("Haifa")
    other = make_locality("Akko")
    first = _submit(client, customer, [("locality_id", str(chosen.id)), *_fields(category)]).json()[
        "ticket_number"
    ]
    text_only = _submit(client, customer, [("locality_id", str(other.id)), *_fields(category)]).json()[
        "ticket_number"
    ]
    chosen.name_he, chosen.name_en, chosen.is_active = "Haifa renamed", "Haifa EN", False
    session.commit()
    session.execute(
        Ticket.__table__.update().where(Ticket.ticket_number == text_only).values(locality_id=None)
    )
    session.commit()
    mine = _customer_headers(customer)

    staff_detail = client.get(f"{STAFF}/tickets/{first}", headers=headers).json()
    customer_detail = client.get(f"{API}/tickets/{first}", headers=mine).json()
    customer_text = client.get(f"{API}/tickets/{text_only}", headers=mine).json()

    expected = {"id": str(chosen.id), "name_he": "Haifa renamed", "name_en": "Haifa EN"}
    assert staff_detail["locality"] == expected
    assert staff_detail["city"] == "Haifa-he"  # the stored text keeps the name at the time of submission
    assert customer_detail["locality"] == expected
    assert customer_text["locality"] is None
    assert set(customer_detail["locality"]) == REF_KEYS


def test_the_customer_detail_has_the_listed_fields_plus_the_settlement_and_the_list_row_has_no_settlement(
    client: TestClient, customer: Customer, category: Category, make_locality
) -> None:
    chosen = make_locality("Haifa")
    number = _submit(client, customer, [("locality_id", str(chosen.id)), *_fields(category)]).json()[
        "ticket_number"
    ]
    mine = _customer_headers(customer)

    detail = client.get(f"{API}/tickets/{number}", headers=mine).json()
    listed = client.get(f"{API}/tickets", headers=mine).json()["items"][0]

    assert set(detail) == {
        "ticket_number",
        "category",
        "created_date",
        "apartment",
        "status",
        "urgent",
        "city",
        "phone",
        "description",
        "has_photo",
        "locality",
    }
    assert set(listed) == {"ticket_number", "category", "created_date", "apartment", "status", "urgent"}


def test_the_cancel_response_carries_the_settlement(
    client: TestClient, customer: Customer, category: Category, make_locality
) -> None:
    chosen = make_locality("Haifa")
    number = _submit(client, customer, [("locality_id", str(chosen.id)), *_fields(category)]).json()[
        "ticket_number"
    ]

    response = client.post(
        f"{API}/tickets/{number}/cancel",
        json={"expected_status": "received"},
        headers=_customer_headers(customer),
    )

    assert response.status_code == 200
    assert response.json()["locality"] == _ref(chosen)


def test_staff_rows_and_details_carry_the_locality_or_null(
    client: TestClient, session: Session, customer: Customer, category: Category, make_locality, headers
) -> None:
    chosen = make_locality("Haifa")
    with_one = _open(client, headers, customer, [("locality_id", str(chosen.id)), *_fields(category)]).json()
    chosen.is_active = False
    session.commit()
    without = _open(client, headers, customer, [("city", "Elsewhere"), *_fields(category)]).json()

    page = client.get(f"{STAFF}/tickets", headers=headers).json()
    by_number = {row["ticket_number"]: row for row in page["items"]}

    assert by_number[with_one["ticket_number"]]["locality"] == _ref(chosen)
    assert by_number[without["ticket_number"]]["locality"] is None
    assert without["locality"] is None
    assert set(by_number[with_one["ticket_number"]]) == {
        "ticket_number",
        "created_at",
        "customer_name",
        "city",
        "apartment",
        "category",
        "status",
        "assigned_technician_name",
        "urgent",
        "locality",
    }
    detail = client.get(f"{STAFF}/tickets/{with_one['ticket_number']}", headers=headers).json()
    assert detail["locality"] == _ref(chosen)
    changed = client.post(
        f"{STAFF}/tickets/{with_one['ticket_number']}/status",
        json={"expected_status": "received", "target_status": "in_progress"},
        headers=headers,
    )
    assert changed.status_code == 200
    assert changed.json()["locality"] == _ref(chosen)


def test_the_report_rows_carry_the_locality_or_null(
    client: TestClient, customer: Customer, category: Category, make_locality, headers
) -> None:
    chosen = make_locality("Haifa")
    with_one = _open(client, headers, customer, [("locality_id", str(chosen.id)), *_fields(category)]).json()

    report = client.get(f"{STAFF}/reports", headers=headers).json()

    assert [row["locality"] for row in report["tickets"]["items"]] == [_ref(chosen)]
    assert report["tickets"]["items"][0]["ticket_number"] == with_one["ticket_number"]


# --------------------------------------------------------------------------- filters


def _seed_tickets(
    client: TestClient, headers: dict[str, str], customer: Customer, category: Category, make_locality
) -> tuple[Locality, Locality, dict[str, int]]:
    haifa = make_locality("Haifa")
    akko = make_locality("Akko")
    numbers_by_name = {
        "haifa": _open(
            client, headers, customer, [("locality_id", str(haifa.id)), *_fields(category)]
        ).json()["ticket_number"],
        "akko": _open(client, headers, customer, [("locality_id", str(akko.id)), *_fields(category)]).json()[
            "ticket_number"
        ],
    }
    return haifa, akko, numbers_by_name


def test_the_staff_list_filters_by_one_settlement_and_combines_it_with_the_city_by_and(
    client: TestClient, customer: Customer, category: Category, make_locality, headers
) -> None:
    haifa, akko, made = _seed_tickets(client, headers, customer, category, make_locality)

    def numbers_of(**params: str) -> list[int]:
        response = client.get(f"{STAFF}/tickets", params=params, headers=headers)
        assert response.status_code == 200, response.text
        return [row["ticket_number"] for row in response.json()["items"]]

    assert numbers_of(locality_id=str(haifa.id)) == [made["haifa"]]
    assert numbers_of(locality_id=str(akko.id).upper()) == [made["akko"]]
    assert numbers_of(locality_id=str(haifa.id), city="Haifa-he") == [made["haifa"]]
    assert numbers_of(locality_id=str(haifa.id), city="Akko-he") == []
    assert sorted(numbers_of(locality_id="")) == sorted(made.values())  # blank is no filter
    assert sorted(numbers_of()) == sorted(made.values())


@pytest.mark.parametrize("value", ["abc", "00000000-0000-0000-0000-000000000000", str(uuid.uuid4())])
@pytest.mark.parametrize("route", ["tickets", "reports"])
def test_a_malformed_or_unknown_settlement_filter_is_invalid_filter(
    client: TestClient, headers, route: str, value: str
) -> None:
    response = client.get(f"{STAFF}/{route}", params={"locality_id": value}, headers=headers)

    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "invalid_filter"


def test_an_invalid_settlement_filter_is_refused_also_with_a_ticket_number_search(
    client: TestClient, headers
) -> None:
    response = client.get(
        f"{STAFF}/tickets", params={"search": "10000", "locality_id": "abc"}, headers=headers
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "invalid_filter"


def test_the_report_filters_by_one_settlement_for_counts_and_rows(
    client: TestClient, customer: Customer, category: Category, make_locality, headers
) -> None:
    haifa, akko, made = _seed_tickets(client, headers, customer, category, make_locality)

    report = client.get(f"{STAFF}/reports", params={"locality_id": str(haifa.id)}, headers=headers).json()
    combined = client.get(
        f"{STAFF}/reports", params={"locality_id": str(haifa.id), "city": "Akko-he"}, headers=headers
    ).json()
    everything = client.get(f"{STAFF}/reports", headers=headers).json()

    assert report["total"] == 1
    assert report["by_status"]["received"] == 1
    assert [row["ticket_number"] for row in report["tickets"]["items"]] == [made["haifa"]]
    assert combined["total"] == 0
    assert combined["tickets"]["items"] == []
    assert everything["total"] == 2


# --------------------------------------------------------------------------- customers (staff routes)


def _customer_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"phone": "0527778899", "full_name": "Dana Levi", "apartment": "4"}
    body.update(overrides)
    return body


def _stored_customer(session: Session, customer_id: str) -> tuple[Any, ...]:
    session.rollback()
    row = session.execute(
        select(Customer.city, Customer.locality_id).where(Customer.id == uuid.UUID(customer_id))
    ).one()
    return tuple(row)


def test_creating_a_customer_with_a_settlement_stores_the_id_and_the_hebrew_name(
    client: TestClient, session: Session, make_locality, headers
) -> None:
    chosen = make_locality("Haifa")

    response = client.post(
        f"{STAFF}/customers", json=_customer_body(locality_id=str(chosen.id), city="ignored"), headers=headers
    )

    assert response.status_code == 201
    body = response.json()
    assert body["city"] == "Haifa-he"  # the settlement wins over a city sent with it
    assert body["locality"] == _ref(chosen)
    assert set(body) == {"id", "full_name", "phone", "city", "apartment", "is_active", "locality"}
    assert _stored_customer(session, body["id"]) == ("Haifa-he", chosen.id)


def test_creating_a_customer_without_a_settlement_keeps_the_text_and_a_null_locality(
    client: TestClient, session: Session, make_locality, headers
) -> None:
    make_locality("Haifa")  # a non-empty list does not make the customer's settlement required

    response = client.post(f"{STAFF}/customers", json=_customer_body(city=" Eilat "), headers=headers)
    explicit_null = client.post(
        f"{STAFF}/customers", json=_customer_body(phone="0527778800", locality_id=None), headers=headers
    )

    assert response.status_code == explicit_null.status_code == 201
    assert response.json()["locality"] is None
    assert _stored_customer(session, response.json()["id"]) == ("Eilat", None)
    assert explicit_null.json()["locality"] is None


@pytest.mark.parametrize("kind", ["inactive", "unknown", "malformed", "number", "empty_list"])
def test_creating_a_customer_with_a_bad_settlement_is_invalid_settlement_under_city(
    client: TestClient, session: Session, make_locality, headers, kind: str
) -> None:
    value: Any
    if kind == "empty_list":
        value = str(uuid.uuid4())  # no active settlement exists, so every id is refused
    else:
        make_locality("Haifa")
        value = {
            "inactive": str(make_locality("Old", active=False).id),
            "unknown": str(uuid.uuid4()),
            "malformed": "nope",
            "number": 7,
        }[kind]

    response = client.post(
        f"{STAFF}/customers", json=_customer_body(phone="bad", locality_id=value), headers=headers
    )

    assert response.status_code == 422
    error = response.json()["detail"]["error"]
    assert list(error["fields"]) == ["phone", "city"]  # the settlement keeps the place of the city
    assert error["fields"]["city"] == "invalid_settlement"
    session.rollback()
    assert session.execute(select(func.count()).select_from(Customer)).scalar_one() == 0


def test_updating_a_customer_sets_the_settlement_and_the_settlement_wins_over_a_city(
    client: TestClient, session: Session, make_customer, make_locality, headers
) -> None:
    chosen = make_locality("Haifa")
    row = make_customer(phone="0527778899", full_name="Dana Levi", full_name_key="dana levi", city="Eilat")

    response = client.put(
        f"{STAFF}/customers/{row.id}",
        json=_customer_body(locality_id=str(chosen.id), city="typed"),
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["city"] == "Haifa-he"
    assert response.json()["locality"] == _ref(chosen)
    assert _stored_customer(session, str(row.id)) == ("Haifa-he", chosen.id)


def test_updating_with_a_bad_settlement_changes_nothing(
    client: TestClient, session: Session, make_customer, make_locality, headers
) -> None:
    make_locality("Haifa")
    inactive = make_locality("Old", active=False)
    row = make_customer(phone="0527778899", full_name="Dana Levi", full_name_key="dana levi", city="Eilat")

    for value in (str(inactive.id), str(uuid.uuid4()), "nope"):
        response = client.put(
            f"{STAFF}/customers/{row.id}", json=_customer_body(locality_id=value), headers=headers
        )
        assert response.status_code == 422
        assert response.json()["detail"]["error"]["fields"] == {"city": "invalid_settlement"}

    assert _stored_customer(session, str(row.id)) == ("Eilat", None)


def test_an_update_without_locality_id_keeps_the_settlement_while_the_city_text_is_unchanged(
    client: TestClient, session: Session, make_customer, make_locality, headers
) -> None:
    chosen = make_locality("Haifa")
    row = make_customer(
        phone="0527778899",
        full_name="Dana Levi",
        full_name_key="dana levi",
        city="Haifa-he",
        locality_id=chosen.id,
    )

    same = client.put(f"{STAFF}/customers/{row.id}", json=_customer_body(city="Haifa-he"), headers=headers)

    assert same.status_code == 200
    assert same.json()["locality"] == _ref(chosen)
    assert _stored_customer(session, str(row.id)) == ("Haifa-he", chosen.id)

    changed = client.put(f"{STAFF}/customers/{row.id}", json=_customer_body(city="Eilat"), headers=headers)

    assert changed.status_code == 200
    assert changed.json()["locality"] is None  # an id never points at a city the customer no longer has
    assert _stored_customer(session, str(row.id)) == ("Eilat", None)


def test_a_null_locality_id_clears_the_settlement_and_keeps_the_text_unless_a_city_is_sent(
    client: TestClient, session: Session, make_customer, make_locality, headers
) -> None:
    chosen = make_locality("Haifa")
    row = make_customer(
        phone="0527778899",
        full_name="Dana Levi",
        full_name_key="dana levi",
        city="Haifa-he",
        locality_id=chosen.id,
    )

    cleared = client.put(
        f"{STAFF}/customers/{row.id}", json=_customer_body(locality_id=None), headers=headers
    )

    assert cleared.status_code == 200
    assert cleared.json()["locality"] is None
    assert cleared.json()["city"] == "Haifa-he"
    assert _stored_customer(session, str(row.id)) == ("Haifa-he", None)

    row2 = make_customer(
        phone="0527778877",
        full_name="Dana Two",
        full_name_key="dana two",
        city="Haifa-he",
        locality_id=chosen.id,
    )
    retyped = client.put(
        f"{STAFF}/customers/{row2.id}",
        json=_customer_body(phone="0527778877", locality_id=None, city="Eilat"),
        headers=headers,
    )
    assert retyped.status_code == 200
    assert _stored_customer(session, str(row2.id)) == ("Eilat", None)


def test_customer_records_and_pages_carry_the_current_settlement_names_in_one_query_per_page(
    client: TestClient, session: Session, make_customer, make_locality, headers
) -> None:
    chosen = make_locality("Haifa")
    rows = [
        make_customer(
            phone=f"05277788{i:02d}",
            full_name=f"Dana {i}",
            full_name_key=f"dana {i}",
            city="Haifa-he",
            locality_id=chosen.id,
        )
        for i in range(1, 5)
    ]
    plain = make_customer(phone="0527779999", full_name="Zed Plain", full_name_key="zed plain")
    chosen.name_he, chosen.name_en, chosen.is_active = "Renamed", "Renamed EN", False
    session.commit()

    with _counting(session) as statements:
        page = client.get(f"{STAFF}/customers", headers=headers).json()
    selects_for_page = [s for s in statements if "svc_customers" in s]

    by_id = {item["id"]: item for item in page["items"]}
    for row in rows:
        assert by_id[str(row.id)]["locality"] == {
            "id": str(chosen.id),
            "name_he": "Renamed",
            "name_en": "Renamed EN",
        }
        assert by_id[str(row.id)]["city"] == "Haifa-he"  # the stored text is untouched
    assert by_id[str(plain.id)]["locality"] is None
    assert len(selects_for_page) == 2  # the count and the page, however many customers have a settlement

    for action in ("deactivate", "reactivate"):
        response = client.post(f"{STAFF}/customers/{rows[0].id}/{action}", headers=headers)
        assert response.json()["locality"]["name_he"] == "Renamed"


# --------------------------------------------------------------------------- profile


def test_the_profile_carries_the_settlement_or_null_and_nothing_else_new(
    client: TestClient, session: Session, make_customer, make_locality
) -> None:
    chosen = make_locality("Haifa")
    linked = make_customer(city="Haifa-he", locality_id=chosen.id)
    plain = make_customer(city="Eilat")
    chosen.name_he, chosen.is_active = "Renamed", False
    session.commit()

    mine = client.get(f"{API}/auth/me", headers=_customer_headers(linked)).json()
    theirs = client.get(f"{API}/auth/me", headers=_customer_headers(plain)).json()

    assert set(mine) == {"full_name", "phone", "city", "apartment", "must_change_password", "locality"}
    assert mine["city"] == "Haifa-he"
    assert mine["locality"]["name_he"] == "Renamed"
    assert set(mine["locality"]) == REF_KEYS
    assert theirs["locality"] is None
    assert "locality_id" not in mine


# --------------------------------------------------------------------------- no N+1


def test_the_staff_list_and_the_report_use_the_same_number_of_statements_for_one_and_for_many_rows(
    client: TestClient, session: Session, customer: Customer, category: Category, make_locality, headers
) -> None:
    chosen = make_locality("Haifa")
    other = make_locality("Akko")

    def run() -> tuple[int, int]:
        with _counting(session) as listed:
            assert client.get(f"{STAFF}/tickets", headers=headers).status_code == 200
        with _counting(session) as reported:
            assert client.get(f"{STAFF}/reports", headers=headers).status_code == 200
        return len(listed), len(reported)

    _open(client, headers, customer, [("locality_id", str(chosen.id)), *_fields(category)])
    one = run()
    for target in (chosen, other, chosen, other):
        _open(client, headers, customer, [("locality_id", str(target.id)), *_fields(category)])
    many = run()

    assert one == many
