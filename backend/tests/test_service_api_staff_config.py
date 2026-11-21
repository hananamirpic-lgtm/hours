"""Staff configuration endpoints (task 13.6; R1.9 to R1.11, R19; design "Staff endpoints").

A small application mounts only `staff_router.router` under `/api/service`. `get_db` is overridden to give
each request its own session on the in-memory test database, so every assertion about stored data reads the
database as a later request would see it. Tokens are real staff access tokens and a real customer token.

SQLite cannot number the identity column `Category.seq`, so the autouse fixture `numbered` assigns it on
insert (the stand-in for PostgreSQL's identity numbering). `foreign_keys_on` turns on SQLite's foreign key
check for the one test that deletes a category a ticket uses. Row locking (`FOR UPDATE`) and the unique
violation race are PostgreSQL behaviour, covered by the integration tests of the service.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import create_access_token
from app.db.session import get_db
from app.models.user import User, UserRole
from app.service_tickets.api import staff_router
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.grammars import fold_key
from app.service_tickets.models import (
    FIELD_LABEL_KEYS,
    Category,
    Customer,
    FieldLabel,
    ServiceSettings,
    Ticket,
)

PREFIX = "/api/service/staff"

#: What the migration seeds, in a short form: the seven labels, "<key>-he" and "<key>-en".
_SEED_LABELS = {key: (f"{key}-he", f"{key}-en") for key in FIELD_LABEL_KEYS}


# --------------------------------------------------------------------------------- app and fixtures


@pytest.fixture(autouse=True)
def numbered() -> Iterator[None]:
    """Number `Category.seq` on insert, as PostgreSQL's identity column does."""
    counter = itertools.count(1)

    def assign(_mapper, _connection, target: Category) -> None:
        if target.seq is None:
            target.seq = next(counter)

    event.listen(Category, "before_insert", assign)
    try:
        yield
    finally:
        event.remove(Category, "before_insert", assign)


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


def _add_category(session: Session, name_he: str, name_en: str, *, active: bool = True) -> Category:
    row = Category(
        name_he=name_he,
        name_en=name_en,
        name_he_key=fold_key(name_he),
        name_en_key=fold_key(name_en),
        is_active=active,
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def three(session: Session) -> tuple[Category, Category, Category]:
    """Three active categories, oldest first, and the seeded labels and settings."""
    session.add_all(
        FieldLabel(field_key=key, label_he=he, label_en=en) for key, (he, en) in _SEED_LABELS.items()
    )
    session.add(ServiceSettings(id=1, description_required=True, photo_upload_enabled=True))
    session.commit()
    return (
        _add_category(session, "חשמל", "Electrical"),
        _add_category(session, "אינסטלציה", "Plumbing"),
        _add_category(session, "כללי", "General"),
    )


def _use(session: Session, category: Category) -> Ticket:
    """A ticket that uses `category`."""
    customer = Customer(
        phone="0501234567", full_name="Dana", full_name_key=fold_key("Dana"), password_hash="not-a-hash"
    )
    session.add(customer)
    session.flush()
    ticket = Ticket(
        ticket_number=10000,
        customer_id=customer.id,
        category_id=category.id,
        city="Haifa",
        city_key=fold_key("Haifa"),
        apartment="4",
        phone="0501234567",
        created_date=date(2025, 1, 1),
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    session.add(ticket)
    session.commit()
    return ticket


def _code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


def _fields(response: Any) -> dict[str, str]:
    return response.json()["detail"]["error"]["fields"]


def _stored(session: Session) -> dict[str, Any]:
    """Everything this task can change, as the database holds it now."""
    session.rollback()
    session.expire_all()
    categories = session.execute(select(Category).order_by(Category.seq)).scalars().all()
    settings = session.get(ServiceSettings, 1)
    labels = session.execute(select(FieldLabel).order_by(FieldLabel.field_key)).scalars().all()
    return {
        "categories": [(c.id, c.name_he, c.name_en, c.is_active) for c in categories],
        "settings": None
        if settings is None
        else (settings.description_required, settings.photo_upload_enabled),
        "labels": [(row.field_key, row.label_he, row.label_en) for row in labels],
    }


def _category_body(**overrides: Any) -> dict[str, Any]:
    body = {"name_he": "קירות", "name_en": "Walls"}
    body.update(overrides)
    return body


# --------------------------------------------------------------------------------- access


def _config_routes(category_id: uuid.UUID | str) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Every configuration route with a body that would succeed for configuration staff."""
    return [
        ("POST", "/categories", _category_body()),
        ("PUT", f"/categories/{category_id}", _category_body()),
        ("POST", f"/categories/{category_id}/deactivate", None),
        ("POST", f"/categories/{category_id}/reactivate", None),
        ("DELETE", f"/categories/{category_id}", None),
        ("GET", "/settings", None),
        ("PUT", "/settings", {"description_required": False, "photo_upload_enabled": False}),
        ("PUT", "/labels", {"labels": {"city": {"he": "עיר חדשה", "en": "New city"}}}),
    ]


def test_technician_gets_403_on_every_configuration_route_and_nothing_changes(
    client: TestClient, session: Session, tech_headers, three
) -> None:
    before = _stored(session)

    for method, path, body in _config_routes(three[1].id):
        response = client.request(method, PREFIX + path, json=body, headers=tech_headers)
        assert (response.status_code, _code(response)) == (403, "insufficient_role"), (method, path)
        assert "fields" not in response.json()["detail"]["error"], (method, path)

    assert _stored(session) == before


def test_technician_is_refused_before_the_body_or_the_id_is_looked_at(
    client: TestClient, session: Session, tech_headers, three
) -> None:
    before = _stored(session)

    bad_body = client.post(f"{PREFIX}/categories", json={"name_he": " ", "bogus": 1}, headers=tech_headers)
    unknown_id = client.delete(f"{PREFIX}/categories/{uuid.uuid4()}", headers=tech_headers)
    malformed_id = client.put(f"{PREFIX}/categories/nope", json={}, headers=tech_headers)
    bad_settings = client.put(
        f"{PREFIX}/settings", json={"description_required": "yes"}, headers=tech_headers
    )

    for response in (bad_body, unknown_id, malformed_id, bad_settings):
        assert (response.status_code, _code(response)) == (403, "insufficient_role")
    assert _stored(session) == before


def test_every_configuration_route_needs_a_staff_token(
    client: TestClient, three, staff, session: Session
) -> None:
    customer = Customer(phone="0509998877", full_name="C", full_name_key="c", password_hash="x")
    session.add(customer)
    session.commit()
    as_customer = {"Authorization": f"Bearer {mint_customer_token(customer.id, customer.token_version)}"}
    routes = [("GET", "/categories", None), *_config_routes(three[0].id)]

    for method, path, body in routes:
        anonymous = client.request(method, PREFIX + path, json=body)
        assert (anonymous.status_code, _code(anonymous)) == (401, "not_authenticated"), (method, path)
        customer_response = client.request(method, PREFIX + path, json=body, headers=as_customer)
        assert (customer_response.status_code, _code(customer_response)) == (
            403,
            "customer_token_not_accepted",
        ), (method, path)
        accounting = client.request(method, PREFIX + path, json=body, headers=staff(UserRole.ACCOUNTING)[1])
        assert (accounting.status_code, _code(accounting)) == (403, "insufficient_role"), (method, path)


@pytest.mark.parametrize("role", [UserRole.TECHNICIAN, UserRole.ADMIN, UserRole.OPERATIONS_ADMIN])
def test_every_ticket_staff_role_reads_all_categories_including_inactive(
    client: TestClient, session: Session, staff, three, role: UserRole
) -> None:
    client.post(f"{PREFIX}/categories/{three[1].id}/deactivate", headers=staff(UserRole.ADMIN)[1])

    response = client.get(f"{PREFIX}/categories", headers=staff(role)[1])

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"id": str(three[0].id), "name_he": "חשמל", "name_en": "Electrical", "is_active": True},
            {"id": str(three[1].id), "name_he": "אינסטלציה", "name_en": "Plumbing", "is_active": False},
            {"id": str(three[2].id), "name_he": "כללי", "name_en": "General", "is_active": True},
        ]
    }


def test_admin_and_operations_admin_get_equal_responses(client: TestClient, staff, three) -> None:
    """The same requests, in the same state, give the same status and body to both configuration roles."""
    new_labels = {"labels": {"city": {"he": " עיר ", "en": " Town "}}}
    requests: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", "/categories", None),
        ("PUT", "/settings", {"description_required": False, "photo_upload_enabled": True}),
        ("GET", "/settings", None),
        ("PUT", "/labels", new_labels),
        ("PUT", f"/categories/{three[0].id}", {"name_he": "חשמל", "name_en": "Electrical"}),
        ("POST", f"/categories/{three[0].id}/reactivate", None),
        ("POST", "/categories", {"name_he": "חשמל", "name_en": "Brand new"}),
        ("POST", "/categories", {"name_he": " ", "name_en": "x" * 61}),
        ("PUT", f"/categories/{uuid.UUID(int=7)}", _category_body()),
        ("DELETE", f"/categories/{uuid.UUID(int=7)}", None),
        ("PUT", "/labels", {"labels": {"colour": {"he": "צבע", "en": "Colour"}}}),
    ]

    def run(role: UserRole) -> list[tuple[int, Any]]:
        auth = staff(role)[1]
        responses = [client.request(m, PREFIX + p, json=b, headers=auth) for m, p, b in requests]
        return [(response.status_code, response.json()) for response in responses]

    admin = run(UserRole.ADMIN)
    operations_admin = run(UserRole.OPERATIONS_ADMIN)

    assert admin == operations_admin
    assert [status for status, _ in admin] == [200, 200, 200, 200, 200, 200, 409, 422, 404, 404, 422]


# --------------------------------------------------------------------------------- create


def test_create_adds_an_active_category_after_the_existing_ones(
    client: TestClient, session: Session, headers, three
) -> None:
    response = client.post(f"{PREFIX}/categories", json=_category_body(name_he="  קירות  "), headers=headers)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "name_he", "name_en", "is_active"}
    assert (body["name_he"], body["name_en"], body["is_active"]) == ("קירות", "Walls", True)
    listed = client.get(f"{PREFIX}/categories", headers=headers).json()["items"]
    assert [item["name_en"] for item in listed] == ["Electrical", "Plumbing", "General", "Walls"]
    assert listed[-1]["id"] == body["id"]


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({"name_he": "", "name_en": "ok"}, {"name_he": "invalid_name_he"}),
        ({"name_he": "ok", "name_en": "   "}, {"name_en": "invalid_name_en"}),
        (
            {"name_he": "x" * 61, "name_en": "y" * 61},
            {"name_he": "invalid_name_he", "name_en": "invalid_name_en"},
        ),
        ({"name_he": 5, "name_en": "ok"}, {"name_he": "invalid_name_he"}),
        ({"name_en": "ok"}, {"name_he": "invalid_name_he"}),
        ({"name_he": "a\x00b", "name_en": "ok"}, {"name_he": "invalid_name_he"}),
    ],
)
def test_create_validation_names_every_failing_text_and_stores_nothing(
    client: TestClient, session: Session, headers, three, body: dict[str, Any], fields: dict[str, str]
) -> None:
    before = _stored(session)

    response = client.post(f"{PREFIX}/categories", json=body, headers=headers)

    assert response.status_code == 422
    assert _fields(response) == fields
    assert _code(response) == next(iter(fields.values()))
    assert _stored(session) == before


def test_create_rejects_an_unknown_field(client: TestClient, session: Session, headers, three) -> None:
    before = _stored(session)

    response = client.post(f"{PREFIX}/categories", json=_category_body(is_active=False), headers=headers)

    assert (response.status_code, _code(response)) == (422, "unknown_field")
    assert _stored(session) == before


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({"name_he": "  חשמל ", "name_en": "Fresh"}, ["name_he"]),
        ({"name_he": "Fresh", "name_en": " ELECTRICAL "}, ["name_en"]),
        ({"name_he": "חשמל", "name_en": "electrical"}, ["name_he", "name_en"]),
    ],
)
def test_create_duplicate_name_is_409_and_names_the_clashing_languages(
    client: TestClient, session: Session, headers, three, body: dict[str, Any], fields: list[str]
) -> None:
    before = _stored(session)

    response = client.post(f"{PREFIX}/categories", json=body, headers=headers)

    assert (response.status_code, _code(response)) == (409, "duplicate_category")
    assert _fields(response) == {name: "duplicate_category" for name in fields}
    assert _stored(session) == before


def test_duplicate_check_includes_inactive_categories(
    client: TestClient, session: Session, headers, three
) -> None:
    client.post(f"{PREFIX}/categories/{three[0].id}/deactivate", headers=headers)
    before = _stored(session)

    response = client.post(f"{PREFIX}/categories", json=_category_body(name_en="electrical"), headers=headers)

    assert (response.status_code, _code(response)) == (409, "duplicate_category")
    assert _stored(session) == before


# --------------------------------------------------------------------------------- rename


def test_rename_replaces_both_names_and_keeps_state_and_position(
    client: TestClient, session: Session, headers, three
) -> None:
    client.post(f"{PREFIX}/categories/{three[1].id}/deactivate", headers=headers)

    response = client.put(
        f"{PREFIX}/categories/{three[1].id}",
        json={"name_he": " צנרת ", "name_en": " Pipes "},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(three[1].id),
        "name_he": "צנרת",
        "name_en": "Pipes",
        "is_active": False,
    }
    names = [item["name_en"] for item in client.get(f"{PREFIX}/categories", headers=headers).json()["items"]]
    assert names == ["Electrical", "Pipes", "General"]


def test_rename_to_its_own_current_name_is_not_a_duplicate(client: TestClient, headers, three) -> None:
    response = client.put(
        f"{PREFIX}/categories/{three[0].id}",
        json={"name_he": "חשמל", "name_en": "ELECTRICAL"},
        headers=headers,
    )
    assert response.status_code == 200 and response.json()["name_en"] == "ELECTRICAL"


def test_rename_to_another_categorys_name_is_409_and_changes_nothing(
    client: TestClient, session: Session, headers, three
) -> None:
    before = _stored(session)

    response = client.put(
        f"{PREFIX}/categories/{three[0].id}",
        json={"name_he": "חדש לגמרי", "name_en": " plumbing"},
        headers=headers,
    )

    assert (response.status_code, _code(response)) == (409, "duplicate_category")
    assert _fields(response) == {"name_en": "duplicate_category"}
    assert _stored(session) == before


def test_rename_validates_both_names_before_it_looks_the_category_up(
    client: TestClient, session: Session, headers, three
) -> None:
    before = _stored(session)

    response = client.put(
        f"{PREFIX}/categories/{uuid.uuid4()}", json={"name_he": " ", "name_en": "ok"}, headers=headers
    )

    assert (response.status_code, _code(response)) == (422, "invalid_name_he")
    assert _stored(session) == before


@pytest.mark.parametrize(
    "raw_id", [str(uuid.uuid4()), str(uuid.UUID(int=0)), "not-a-uuid", "12345", "{" + str(uuid.uuid4()) + "}"]
)
@pytest.mark.parametrize("action", ["put", "deactivate", "reactivate", "delete"])
def test_unknown_or_malformed_category_id_is_404(
    client: TestClient, session: Session, headers, three, action: str, raw_id: str
) -> None:
    before = _stored(session)
    url = f"{PREFIX}/categories/{raw_id}"

    if action == "put":
        response = client.put(url, json=_category_body(), headers=headers)
    elif action == "delete":
        response = client.delete(url, headers=headers)
    else:
        response = client.post(f"{url}/{action}", headers=headers)

    assert (response.status_code, _code(response)) == (404, "category_not_found")
    assert _stored(session) == before


# --------------------------------------------------------------------------------- deactivate, reactivate


def test_deactivate_then_reactivate_toggles_only_the_state(
    client: TestClient, session: Session, headers, three
) -> None:
    off = client.post(f"{PREFIX}/categories/{three[0].id}/deactivate", headers=headers)
    assert off.status_code == 200
    assert off.json() == {
        "id": str(three[0].id),
        "name_he": "חשמל",
        "name_en": "Electrical",
        "is_active": False,
    }
    assert [row[3] for row in _stored(session)["categories"]] == [False, True, True]

    again = client.post(f"{PREFIX}/categories/{three[0].id}/deactivate", headers=headers)
    assert again.status_code == 200 and again.json()["is_active"] is False

    on = client.post(f"{PREFIX}/categories/{three[0].id}/reactivate", headers=headers)
    assert on.status_code == 200 and on.json()["is_active"] is True
    assert [row[3] for row in _stored(session)["categories"]] == [True, True, True]


def test_last_active_category_cannot_be_deactivated_or_deleted(
    client: TestClient, session: Session, headers, three
) -> None:
    client.post(f"{PREFIX}/categories/{three[0].id}/deactivate", headers=headers)
    client.post(f"{PREFIX}/categories/{three[1].id}/deactivate", headers=headers)
    before = _stored(session)

    deactivate = client.post(f"{PREFIX}/categories/{three[2].id}/deactivate", headers=headers)
    delete = client.delete(f"{PREFIX}/categories/{three[2].id}", headers=headers)

    for response in (deactivate, delete):
        assert (response.status_code, _code(response)) == (409, "last_active_category")
    assert _stored(session) == before
    assert [row[3] for row in before["categories"]] == [False, False, True]


def test_an_inactive_category_can_still_be_deleted_while_one_is_active(
    client: TestClient, session: Session, headers, three
) -> None:
    client.post(f"{PREFIX}/categories/{three[0].id}/deactivate", headers=headers)

    response = client.delete(f"{PREFIX}/categories/{three[0].id}", headers=headers)

    assert (response.status_code, response.json()) == (200, {"result": "removed"})


# --------------------------------------------------------------------------------- delete


def test_delete_of_an_unused_category_removes_it(
    client: TestClient, session: Session, headers, three
) -> None:
    plumbing_id = three[1].id

    response = client.delete(f"{PREFIX}/categories/{plumbing_id}", headers=headers)

    assert (response.status_code, response.json()) == (200, {"result": "removed"})
    assert [row[2] for row in _stored(session)["categories"]] == ["Electrical", "General"]
    assert client.delete(f"{PREFIX}/categories/{plumbing_id}", headers=headers).status_code == 404


def test_delete_of_a_used_category_deactivates_it_and_keeps_the_ticket(
    client: TestClient, session: Session, headers, three, foreign_keys_on: None
) -> None:
    ticket = _use(session, three[1])

    response = client.delete(f"{PREFIX}/categories/{three[1].id}", headers=headers)

    assert (response.status_code, response.json()) == (200, {"result": "deactivated"})
    stored = _stored(session)
    assert [(row[2], row[3]) for row in stored["categories"]] == [
        ("Electrical", True),
        ("Plumbing", False),
        ("General", True),
    ]
    assert (
        session.execute(select(Ticket.category_id).where(Ticket.id == ticket.id)).scalar_one() == three[1].id
    )
    listed = client.get(f"{PREFIX}/categories", headers=headers).json()["items"]
    assert [item["is_active"] for item in listed] == [True, False, True]


# --------------------------------------------------------------------------------- settings


def test_settings_default_when_no_row_exists_and_a_put_creates_it(
    client: TestClient, session: Session, headers
) -> None:
    assert client.get(f"{PREFIX}/settings", headers=headers).json() == {
        "settings": {"description_required": True, "photo_upload_enabled": True}
    }
    assert _stored(session)["settings"] is None

    response = client.put(
        f"{PREFIX}/settings",
        json={"description_required": False, "photo_upload_enabled": True},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {"settings": {"description_required": False, "photo_upload_enabled": True}}
    assert _stored(session)["settings"] == (False, True)


def test_settings_put_is_read_back_by_get(client: TestClient, session: Session, headers, three) -> None:
    client.put(
        f"{PREFIX}/settings",
        json={"description_required": False, "photo_upload_enabled": False},
        headers=headers,
    )

    assert client.get(f"{PREFIX}/settings", headers=headers).json() == {
        "settings": {"description_required": False, "photo_upload_enabled": False}
    }
    assert _stored(session)["settings"] == (False, False)


@pytest.mark.parametrize(
    "body",
    [
        {"description_required": "yes", "photo_upload_enabled": True},
        {"description_required": 1, "photo_upload_enabled": True},
        {"description_required": None, "photo_upload_enabled": True},
        {"description_required": True},
        {},
    ],
)
def test_settings_put_needs_two_strict_booleans_and_stores_nothing_otherwise(
    client: TestClient, session: Session, headers, three, body: dict[str, Any]
) -> None:
    before = _stored(session)

    response = client.put(f"{PREFIX}/settings", json=body, headers=headers)

    assert response.status_code == 422
    assert response.json()["detail"]["error"]["fields"]
    assert _stored(session) == before


def test_settings_put_rejects_an_unknown_field(client: TestClient, session: Session, headers, three) -> None:
    before = _stored(session)

    response = client.put(
        f"{PREFIX}/settings",
        json={"description_required": False, "photo_upload_enabled": False, "x": 1},
        headers=headers,
    )

    assert (response.status_code, _code(response)) == (422, "unknown_field")
    assert _stored(session) == before


# --------------------------------------------------------------------------------- labels


def test_labels_put_stores_trimmed_texts_and_leaves_the_other_fields_alone(
    client: TestClient, session: Session, headers, three
) -> None:
    response = client.put(
        f"{PREFIX}/labels",
        json={
            "labels": {
                "urgent": {"he": " דחוף מאוד ", "en": " Very urgent "},
                "city": {"he": "עיר", "en": "Town"},
            }
        },
        headers=headers,
    )

    assert response.status_code == 200
    labels = response.json()["labels"]
    assert list(labels) == list(FIELD_LABEL_KEYS)  # all seven, in form reading order
    assert labels["city"] == {"he": "עיר", "en": "Town"}
    assert labels["urgent"] == {"he": "דחוף מאוד", "en": "Very urgent"}
    assert labels["phone"] == {"he": "phone-he", "en": "phone-en"}
    stored = {key: (he, en) for key, he, en in _stored(session)["labels"]}
    assert stored["city"] == ("עיר", "Town") and stored["apartment"] == ("apartment-he", "apartment-en")


def test_labels_put_with_no_labels_changes_nothing(
    client: TestClient, session: Session, headers, three
) -> None:
    before = _stored(session)

    response = client.put(f"{PREFIX}/labels", json={"labels": {}}, headers=headers)

    assert response.status_code == 200 and len(response.json()["labels"]) == 7
    assert _stored(session) == before


def test_labels_validation_names_every_failing_text_in_form_order_and_stores_nothing(
    client: TestClient, session: Session, headers, three
) -> None:
    before = _stored(session)
    body = {
        "labels": {
            "urgent": {"he": "תקין", "en": "Fine"},
            "phone": {"he": "   ", "en": "ok"},
            "city": {"he": "ok", "en": "x" * 61},
        }
    }

    response = client.put(f"{PREFIX}/labels", json=body, headers=headers)

    assert response.status_code == 422
    assert list(_fields(response).items()) == [
        ("city.label_en", "invalid_label_en"),
        ("phone.label_he", "invalid_label_he"),
    ]
    assert _code(response) == "invalid_label_en"
    assert _stored(session) == before


@pytest.mark.parametrize(
    ("texts", "field", "code"),
    [
        ({"he": 5, "en": "ok"}, "city.label_he", "invalid_label_he"),
        ({"he": "ok", "en": None}, "city.label_en", "invalid_label_en"),
        ({"he": "ok"}, "city.label_en", "invalid_label_en"),
        ({"en": "ok"}, "city.label_he", "invalid_label_he"),
        ({"he": "a\x00b", "en": "ok"}, "city.label_he", "invalid_label_he"),
    ],
)
def test_labels_missing_or_non_string_text_gets_the_label_code(
    client: TestClient, session: Session, headers, three, texts: dict[str, Any], field: str, code: str
) -> None:
    before = _stored(session)

    response = client.put(f"{PREFIX}/labels", json={"labels": {"city": texts}}, headers=headers)

    assert (response.status_code, _code(response)) == (422, code)
    assert _fields(response) == {field: code}
    assert _stored(session) == before


def test_labels_unknown_field_key_is_422_unknown_field_and_stores_nothing(
    client: TestClient, session: Session, headers, three
) -> None:
    before = _stored(session)

    response = client.put(
        f"{PREFIX}/labels",
        json={"labels": {"city": {"he": "עיר", "en": "Town"}, "colour": {"he": "צבע", "en": "Colour"}}},
        headers=headers,
    )

    assert (response.status_code, _code(response)) == (422, "unknown_field")
    assert _fields(response) == {"colour": "unknown_field"}
    assert _stored(session) == before


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"labels": []},
        {"labels": {"city": "text"}},
        {"labels": {"city": {"he": "a", "en": "b", "fr": "c"}}},
        {"labels": {}, "extra": 1},
    ],
)
def test_labels_malformed_body_is_422_and_stores_nothing(
    client: TestClient, session: Session, headers, three, body: dict[str, Any]
) -> None:
    before = _stored(session)

    response = client.put(f"{PREFIX}/labels", json=body, headers=headers)

    assert response.status_code == 422
    assert _stored(session) == before
