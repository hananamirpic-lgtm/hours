"""Customer form and ticket endpoints (task 13.3; R9.1, R9.3, R9.8, R9.9, R9.10, R12.1 to R12.4, R12.9,
R13.1, R13.4, R13.6, R13.7, R13.10, R14.5, R6.2).

Same harness as `test_service_api_auth.py`: a small application includes the customer router, each request
gets its own session on the in-memory test database, and tokens are real. The router is not mounted
anywhere else; task 13.7 does that. SQLite has no sequence, so the ticket-number allocator is a
dependency override that counts how many numbers it handed out, and `Category.seq` is numbered by an
insert listener, as in the configuration tests.

Because requests use their own sessions, a row the test reads afterwards is a row the router committed.
The tests read single columns, never entities, so they never see a stale identity-map copy.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.models.user import UserRole
from app.service_tickets.api import customer_router
from app.service_tickets.config import ServiceConfig
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.models import (
    Category,
    Customer,
    FieldLabel,
    HistoryEntry,
    ServiceSettings,
    Ticket,
)
from app.service_tickets.services.numbering import TicketNumbersExhausted

PHONE = "0505556677"
URL = "/api/service"


class Numbers:
    """The ticket-number allocator of the tests: counts the numbers it hands out."""

    def __init__(self, first: int = 10000) -> None:
        self.next = first
        self.calls = 0

    def __call__(self, _session: Session) -> int:
        self.calls += 1
        number = self.next
        self.next += 1
        return number


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


def _build_app(session: Session, allocator: Callable[[Session], int]) -> FastAPI:
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True)

    def request_session() -> Iterator[Session]:
        with factory() as per_request:
            yield per_request

    app = FastAPI()
    app.include_router(customer_router.router, prefix=URL)
    app.dependency_overrides[get_db] = request_session
    app.dependency_overrides[customer_router.get_ticket_allocator] = lambda: allocator
    app.dependency_overrides[customer_router.get_service_config] = lambda: _config()
    return app


def _config() -> ServiceConfig:
    names = (
        "service_tickets_enabled service_customer_token_ttl_seconds service_signin_rate_max "
        "service_signin_rate_window_seconds service_submit_rate_max service_submit_rate_window_seconds "
        "service_request_rate_max service_request_rate_window_seconds service_signin_lock_attempts "
        "service_signin_lock_seconds service_client_ip_header service_trusted_proxy_cidrs "
        "service_accessibility_contact"
    ).split()
    return ServiceConfig.from_settings(SimpleNamespace(**dict.fromkeys(names, "")))  # type: ignore[arg-type]


@pytest.fixture
def client(session: Session, numbers: Numbers) -> Iterator[TestClient]:
    with TestClient(_build_app(session, numbers), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password("4321")


_phones = itertools.count(1)


@pytest.fixture
def make_customer(session: Session, password_hash: str):
    made = itertools.count()

    def _make(**overrides: Any) -> Customer:
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


@pytest.fixture
def make_category(session: Session):
    def _make(name_en: str, **overrides: Any) -> Category:
        fields: dict[str, Any] = {
            "name_he": f"{name_en}-he",
            "name_en": name_en,
            "name_he_key": f"{name_en}-he".casefold(),
            "name_en_key": name_en.casefold(),
        }
        fields.update(overrides)
        created = Category(**fields)
        session.add(created)
        session.commit()
        return created

    return _make


@pytest.fixture
def category(make_category) -> Category:
    return make_category("Plumbing")


@pytest.fixture
def customer(make_customer) -> Customer:
    return make_customer()


@pytest.fixture
def make_ticket(session: Session):
    counter = itertools.count(10000)

    def _make(owner: Customer, category: Category, **overrides: Any) -> Ticket:
        created_at = overrides.pop("created_at", datetime(2025, 3, 1, 8, 0, tzinfo=UTC))
        fields: dict[str, Any] = {
            "ticket_number": next(counter),
            "customer_id": owner.id,
            "category_id": category.id,
            "city": "Haifa",
            "city_key": "haifa",
            "apartment": "12",
            "phone": PHONE,
            "description": "The tap leaks.",
            "status": "received",
            "assigned_technician_name": "",
            "created_at": created_at,
            "created_date": created_at.date(),
            "updated_at": created_at,
        }
        fields.update(overrides)
        created = Ticket(**fields)
        session.add(created)
        session.commit()
        return created

    return _make


# --------------------------------------------------------------------------- helpers


def _bearer(owner: Customer, **kwargs: Any) -> dict[str, str]:
    version = kwargs.pop("version", owner.token_version)
    return {"Authorization": f"Bearer {mint_customer_token(owner.id, version, **kwargs)}"}


def _code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


def _form(fields: dict[str, Any] | list[tuple[str, Any]], photo: bytes | None = None) -> list[Any]:
    """A multipart body without a real file: every field is a plain form part."""
    items = list(fields.items()) if isinstance(fields, dict) else list(fields)
    parts: list[Any] = [(name, (None, value)) for name, value in items]
    if photo is not None:
        parts.append(("photo", ("photo.jpg", photo, "image/jpeg")))
    return parts


def _good(category: Category, **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "city": "Haifa",
        "apartment": "12",
        "phone": "050-555-6677",
        "category_id": str(category.id),
        "description": "The kitchen tap leaks.",
        "urgent": "false",
    }
    fields.update(overrides)
    return fields


def _submit(client: TestClient, owner: Customer, fields: Any, photo: bytes | None = None) -> Any:
    return client.post(f"{URL}/tickets", files=_form(fields, photo), headers=_bearer(owner))


def _ticket_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(Ticket)).scalar_one()


def _stored(session: Session, number: int) -> Any:
    return session.execute(
        select(
            Ticket.status,
            Ticket.updated_at,
            Ticket.photo_key,
            Ticket.assigned_technician_name,
            Ticket.customer_id,
            Ticket.city,
            Ticket.apartment,
            Ticket.phone,
            Ticket.description,
            Ticket.urgent,
            Ticket.created_date,
            Ticket.category_id,
        ).where(Ticket.ticket_number == number)
    ).one()


# --------------------------------------------------------------------------- GET /form-config


def test_form_config_lists_active_categories_oldest_first_with_labels_and_options(
    client: TestClient, session: Session, customer: Customer, make_category
) -> None:
    first = make_category("Electrical")
    second = make_category("Plumbing")
    make_category("Retired", is_active=False)
    third = make_category("General")
    session.add_all(
        [
            FieldLabel(field_key="city", label_he="עיר", label_en="City"),
            ServiceSettings(id=1, description_required=False, photo_upload_enabled=False),
        ]
    )
    session.commit()

    response = client.get(f"{URL}/form-config", headers=_bearer(customer))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"categories", "labels", "settings"}
    assert [c["id"] for c in body["categories"]] == [str(first.id), str(second.id), str(third.id)]
    assert set(body["categories"][0]) == {"id", "name_he", "name_en"}
    assert body["labels"] == {"city": {"he": "עיר", "en": "City"}}
    assert body["settings"] == {"description_required": False, "photo_upload_enabled": False}


def test_form_config_uses_the_migration_defaults_when_no_settings_row_exists(
    client: TestClient, customer: Customer, category: Category
) -> None:
    body = client.get(f"{URL}/form-config", headers=_bearer(customer)).json()

    assert body["settings"] == {"description_required": True, "photo_upload_enabled": True}


# --------------------------------------------------------------------------- POST /tickets


def test_a_valid_submission_is_201_and_returns_only_the_ticket_number(
    client: TestClient, session: Session, customer: Customer, category: Category, numbers: Numbers
) -> None:
    response = _submit(
        client,
        customer,
        _good(category, city="  Haifa  ", apartment=" 12a ", phone="+972 50-555-6677", urgent="TRUE"),
    )

    assert response.status_code == 201
    assert response.json() == {"ticket_number": 10000}
    assert response.headers["cache-control"] == "no-store"
    stored = _stored(session, 10000)
    assert stored.status == "received"
    assert stored.customer_id == customer.id
    assert (stored.city, stored.apartment, stored.phone) == ("Haifa", "12a", PHONE)
    assert stored.description == "The kitchen tap leaks."
    assert stored.urgent is True
    assert stored.assigned_technician_name == ""
    assert stored.photo_key is None
    assert stored.category_id == category.id
    assert isinstance(stored.created_date, date)
    assert numbers.calls == 1


def test_urgent_defaults_to_false_and_numbers_rise_with_each_ticket(
    client: TestClient, session: Session, customer: Customer, category: Category
) -> None:
    fields = _good(category)
    del fields["urgent"]

    first = _submit(client, customer, fields)
    second = _submit(client, customer, fields)

    assert (first.json()["ticket_number"], second.json()["ticket_number"]) == (10000, 10001)
    assert _stored(session, 10000).urgent is False


def test_the_owner_comes_from_the_token_and_the_customer_row_is_unchanged(
    client: TestClient, session: Session, make_customer, category: Category
) -> None:
    first = make_customer()
    second = make_customer(city="Acre", apartment="3")
    before = session.execute(
        select(Customer.city, Customer.apartment, Customer.phone, Customer.updated_at).where(
            Customer.id == second.id
        )
    ).one()

    response = _submit(client, second, _good(category, city="Elsewhere", phone="0529999999"))

    assert response.status_code == 201
    assert _stored(session, response.json()["ticket_number"]).customer_id == second.id
    assert _stored(session, response.json()["ticket_number"]).customer_id != first.id
    after = session.execute(
        select(Customer.city, Customer.apartment, Customer.phone, Customer.updated_at).where(
            Customer.id == second.id
        )
    ).one()
    assert after == before


def test_every_failing_field_is_reported_in_form_order_and_no_number_is_used(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    numbers: Numbers,
) -> None:
    response = _submit(
        client,
        customer,
        {
            "city": "",
            "apartment": "x" * 21,
            "phone": "12345",
            "category_id": "not-a-uuid",
            "description": "",
            "urgent": "maybe",
        },
    )

    assert response.status_code == 422
    error = response.json()["detail"]["error"]
    assert error["fields"] == {
        "city": "invalid_city",
        "apartment": "invalid_apartment",
        "phone": "invalid_phone",
        "category_id": "invalid_category",
        "description": "invalid_description",
        "urgent": "invalid_urgent",
    }
    assert list(error["fields"]) == ["city", "apartment", "phone", "category_id", "description", "urgent"]
    assert error["code"] == "invalid_city"
    assert _ticket_count(session) == 0
    assert numbers.calls == 0
    assert _submit(client, customer, _good(category)).json() == {"ticket_number": 10000}


def test_a_single_failing_field_gives_its_own_code(
    client: TestClient, customer: Customer, category: Category
) -> None:
    response = _submit(client, customer, _good(category, phone="050-555-667"))

    assert response.status_code == 422
    assert response.json() == {
        "detail": {"error": {"code": "invalid_phone", "fields": {"phone": "invalid_phone"}}}
    }


def test_an_empty_description_depends_on_the_setting(
    client: TestClient, session: Session, customer: Customer, category: Category
) -> None:
    required = _submit(client, customer, _good(category, description="  "))
    assert (required.status_code, required.json()["detail"]["error"]["fields"]) == (
        422,
        {"description": "invalid_description"},
    )

    session.add(ServiceSettings(id=1, description_required=False, photo_upload_enabled=True))
    session.commit()
    optional = _submit(client, customer, _good(category, description="  "))

    assert optional.status_code == 201
    assert _stored(session, optional.json()["ticket_number"]).description == ""


def test_an_inactive_or_unknown_category_is_invalid_category(
    client: TestClient, session: Session, customer: Customer, make_category
) -> None:
    retired = make_category("Retired", is_active=False)
    make_category("Active")

    for category_id in (str(retired.id), str(uuid.uuid4())):
        response = _submit(client, customer, _good(retired, category_id=category_id))
        assert response.status_code == 422
        assert response.json()["detail"]["error"]["fields"] == {"category_id": "invalid_category"}
    assert _ticket_count(session) == 0


@pytest.mark.parametrize(
    "extra",
    [
        ("customer_id", str(uuid.uuid4())),
        ("status", "closed"),
        ("ticket_number", "12345"),
        ("assigned_technician_name", "Dan"),
        ("history", "x"),
    ],
)
def test_an_unknown_part_is_422_unknown_field_and_creates_no_ticket(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    numbers: Numbers,
    extra: tuple[str, str],
) -> None:
    fields = [*_good(category).items(), extra]

    response = _submit(client, customer, fields)

    assert response.status_code == 422
    error = response.json()["detail"]["error"]
    assert error == {"code": "unknown_field", "fields": {extra[0]: "unknown_field"}}
    assert _ticket_count(session) == 0
    assert numbers.calls == 0


def test_an_unknown_part_is_reported_after_the_failing_known_fields(
    client: TestClient, customer: Customer, category: Category
) -> None:
    fields = [*_good(category, city="").items(), ("owner", "me")]

    error = _submit(client, customer, fields).json()["detail"]["error"]

    assert error["code"] == "invalid_city"
    assert list(error["fields"]) == ["city", "owner"]


def test_a_repeated_field_is_invalid_for_that_field(
    client: TestClient, session: Session, customer: Customer, category: Category
) -> None:
    fields = [*_good(category).items(), ("city", "Acre")]

    response = _submit(client, customer, fields)

    assert response.status_code == 422
    assert response.json()["detail"]["error"]["fields"] == {"city": "invalid_city"}
    assert _ticket_count(session) == 0


def test_a_photo_part_is_accepted_and_ignored_until_the_photo_task(
    client: TestClient, session: Session, customer: Customer, category: Category
) -> None:
    response = _submit(client, customer, _good(category), photo=b"\xff\xd8not really a jpeg")

    assert response.status_code == 201
    assert _stored(session, response.json()["ticket_number"]).photo_key is None


def test_a_body_that_is_not_a_form_fails_every_required_field(
    client: TestClient, session: Session, customer: Customer, category: Category
) -> None:
    response = client.post(f"{URL}/tickets", json=_good(category), headers=_bearer(customer))

    assert response.status_code == 422
    assert set(response.json()["detail"]["error"]["fields"]) == {
        "city",
        "apartment",
        "phone",
        "category_id",
        "description",
    }
    assert _ticket_count(session) == 0


def test_a_broken_multipart_body_is_422_with_the_package_envelope(
    client: TestClient, session: Session, customer: Customer
) -> None:
    response = client.post(
        f"{URL}/tickets",
        content=b'--nope\r\nContent-Disposition: form-data; name="city"\r\n\r\nHaifa',
        headers={**_bearer(customer), "Content-Type": "multipart/form-data; boundary=nope"},
    )

    assert response.status_code == 422
    assert "fields" in response.json()["detail"]["error"]
    assert _ticket_count(session) == 0


def test_exhausted_ticket_numbers_are_503_and_store_nothing(
    session: Session, customer: Customer, category: Category
) -> None:
    def exhausted(_session: Session) -> int:
        raise TicketNumbersExhausted

    with TestClient(_build_app(session, exhausted), raise_server_exceptions=False) as client:
        response = _submit(client, customer, _good(category))

    assert response.status_code == 503
    assert response.json() == {"detail": {"error": {"code": "ticket_numbers_exhausted"}}}
    assert _ticket_count(session) == 0


def test_a_database_failure_while_storing_is_503_and_stores_nothing(
    session: Session, customer: Customer, category: Category
) -> None:
    def broken(_session: Session) -> int:
        raise DBAPIError("SELECT 1", {}, Exception("connection lost"))

    with TestClient(_build_app(session, broken), raise_server_exceptions=False) as client:
        response = _submit(client, customer, _good(category))

    assert response.status_code == 503
    assert response.json() == {"detail": {"error": {"code": "service_unavailable"}}}
    assert _ticket_count(session) == 0


def test_submission_needs_a_customer_token(
    client: TestClient, session: Session, category: Category, make_user, numbers: Numbers
) -> None:
    missing = client.post(f"{URL}/tickets", files=_form(_good(category)))
    assert (missing.status_code, _code(missing)) == (401, "not_authenticated")

    staff = make_user(role=UserRole.ADMIN)
    token = create_access_token(user_id=staff.id, token_version=1, role="admin")
    refused = client.post(
        f"{URL}/tickets", files=_form(_good(category)), headers={"Authorization": f"Bearer {token}"}
    )
    assert (refused.status_code, _code(refused)) == (403, "staff_token_not_accepted")
    assert _ticket_count(session) == 0
    assert numbers.calls == 0


# --------------------------------------------------------------------------- GET /tickets


def test_the_list_holds_only_own_tickets_newest_first_with_five_fields(
    client: TestClient, make_customer, make_ticket, category: Category
) -> None:
    mine = make_customer()
    other = make_customer()
    old = make_ticket(mine, category, created_at=datetime(2025, 3, 1, 8, 0, tzinfo=UTC))
    new = make_ticket(mine, category, created_at=datetime(2025, 3, 2, 8, 0, tzinfo=UTC), urgent=True)
    tie = make_ticket(mine, category, created_at=datetime(2025, 3, 2, 8, 0, tzinfo=UTC), status="cancelled")
    make_ticket(other, category)

    response = client.get(f"{URL}/tickets", headers=_bearer(mine))

    assert response.status_code == 200
    body = response.json()
    assert (body["page"], body["page_size"], body["total"]) == (1, 50, 3)
    assert [item["ticket_number"] for item in body["items"]] == [
        tie.ticket_number,
        new.ticket_number,
        old.ticket_number,
    ]
    assert set(body["items"][0]) == {"ticket_number", "category", "created_date", "status", "urgent"}
    assert body["items"][0]["status"] == "cancelled"
    assert body["items"][1]["urgent"] is True
    assert body["items"][0]["category"] == {
        "id": str(category.id),
        "name_he": category.name_he,
        "name_en": category.name_en,
    }
    assert body["items"][2]["created_date"] == "2025-03-01"


def test_the_list_pages_by_fifty_and_a_page_past_the_end_is_empty_with_the_total(
    client: TestClient, session: Session, customer: Customer, category: Category, make_ticket
) -> None:
    base = datetime(2025, 3, 1, tzinfo=UTC)
    for index in range(51):
        make_ticket(customer, category, created_at=base + timedelta(minutes=index))

    first = client.get(f"{URL}/tickets", headers=_bearer(customer)).json()
    second = client.get(f"{URL}/tickets?page=2", headers=_bearer(customer)).json()
    beyond = client.get(f"{URL}/tickets?page=9", headers=_bearer(customer))

    assert (len(first["items"]), first["total"]) == (50, 51)
    assert (len(second["items"]), second["page"], second["total"]) == (1, 2, 51)
    assert beyond.status_code == 200
    assert beyond.json() == {"items": [], "page": 9, "page_size": 50, "total": 51}


def test_an_empty_list_is_page_one_with_total_zero(client: TestClient, customer: Customer) -> None:
    response = client.get(f"{URL}/tickets", headers=_bearer(customer))

    assert response.json() == {"items": [], "page": 1, "page_size": 50, "total": 0}


@pytest.mark.parametrize("page", ["0", "-1", "abc", "1.5", "", "+1", "%201", "٢"])
def test_an_invalid_page_is_422_invalid_page(client: TestClient, customer: Customer, page: str) -> None:
    response = client.get(f"{URL}/tickets?page={page}", headers=_bearer(customer))

    assert response.status_code == 422
    assert response.json() == {"detail": {"error": {"code": "invalid_page"}}}


# --------------------------------------------------------------------------- GET /tickets/{n}


def test_the_detail_holds_the_stored_values_and_nothing_staff_only(
    client: TestClient, customer: Customer, category: Category, make_ticket
) -> None:
    ticket = make_ticket(
        customer,
        category,
        city="Acre",
        apartment="7",
        phone="0521112233",
        urgent=True,
        photo_key="service-tickets/abc.jpg",
        assigned_technician_name="Dan",
    )

    response = client.get(f"{URL}/tickets/{ticket.ticket_number}", headers=_bearer(customer))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "ticket_number",
        "category",
        "created_date",
        "status",
        "urgent",
        "city",
        "apartment",
        "phone",
        "description",
        "has_photo",
    }
    assert (body["city"], body["apartment"], body["phone"]) == ("Acre", "7", "0521112233")
    assert body["has_photo"] is True
    assert "service-tickets/abc.jpg" not in response.text
    assert "Dan" not in response.text


def test_missing_foreign_and_malformed_tickets_are_the_same_404(
    client: TestClient, make_customer, make_ticket, category: Category
) -> None:
    mine = make_customer()
    other = make_customer()
    foreign = make_ticket(other, category)

    responses = [
        client.get(f"{URL}/tickets/{foreign.ticket_number}", headers=_bearer(mine)),
        client.get(f"{URL}/tickets/99999", headers=_bearer(mine)),
        client.get(f"{URL}/tickets/9999", headers=_bearer(mine)),
        client.get(f"{URL}/tickets/abc", headers=_bearer(mine)),
        client.get(f"{URL}/tickets/١٠٠٠٠", headers=_bearer(mine)),
        client.get(f"{URL}/tickets/10000.5", headers=_bearer(mine)),
    ]

    for response in responses:
        assert response.status_code == 404
        assert response.json() == {"detail": {"error": {"code": "ticket_not_found"}}}
    assert len({response.content for response in responses}) == 1


# --------------------------------------------------------------------------- POST /tickets/{n}/cancel


def _cancel(client: TestClient, owner: Customer, number: Any, body: Any) -> Any:
    return client.post(f"{URL}/tickets/{number}/cancel", json=body, headers=_bearer(owner))


@pytest.mark.parametrize("status", ["received", "in_progress"])
def test_cancel_from_received_and_in_progress_keeps_everything_else(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    make_ticket,
    status: str,
) -> None:
    ticket = make_ticket(
        customer,
        category,
        status=status,
        photo_key="service-tickets/abc.jpg",
        assigned_technician_name="Dan",
        urgent=True,
    )
    session.add(HistoryEntry(ticket_id=ticket.id, entry_text="Checked the pipe", technician_name="Dan"))
    session.commit()
    before = _stored(session, ticket.ticket_number)

    response = _cancel(client, customer, ticket.ticket_number, {"expected_status": status})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["ticket_number"] == ticket.ticket_number
    assert body["has_photo"] is True
    assert set(body) == {
        "ticket_number",
        "category",
        "created_date",
        "status",
        "urgent",
        "city",
        "apartment",
        "phone",
        "description",
        "has_photo",
    }
    assert "Dan" not in response.text
    after = _stored(session, ticket.ticket_number)
    assert after.status == "cancelled"
    assert after.updated_at != before.updated_at
    assert (after.photo_key, after.assigned_technician_name, after.description) == (
        "service-tickets/abc.jpg",
        "Dan",
        "The tap leaks.",
    )
    entries = session.execute(select(HistoryEntry.entry_text)).scalars().all()
    assert entries == ["Checked the pipe"]
    listed = client.get(f"{URL}/tickets", headers=_bearer(customer)).json()
    assert [(i["ticket_number"], i["status"]) for i in listed["items"]] == [
        (ticket.ticket_number, "cancelled")
    ]


@pytest.mark.parametrize("status", ["closed", "cancelled"])
def test_cancel_of_a_closed_or_cancelled_ticket_is_invalid_transition(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    make_ticket,
    status: str,
) -> None:
    ticket = make_ticket(customer, category, status=status)
    before = _stored(session, ticket.ticket_number)

    response = _cancel(client, customer, ticket.ticket_number, {"expected_status": status})

    assert response.status_code == 409
    assert response.json() == {"detail": {"error": {"code": "invalid_transition"}}}
    assert _stored(session, ticket.ticket_number) == before


def test_a_mismatched_expected_status_is_ticket_status_changed(
    client: TestClient, session: Session, customer: Customer, category: Category, make_ticket
) -> None:
    ticket = make_ticket(customer, category, status="in_progress")
    before = _stored(session, ticket.ticket_number)

    response = _cancel(client, customer, ticket.ticket_number, {"expected_status": "received"})

    assert response.status_code == 409
    assert response.json() == {"detail": {"error": {"code": "ticket_status_changed"}}}
    assert _stored(session, ticket.ticket_number) == before


def test_a_status_changed_by_staff_first_makes_the_cancel_a_409(
    client: TestClient, session: Session, customer: Customer, category: Category, make_ticket
) -> None:
    ticket = make_ticket(customer, category, status="received")
    session.execute(update(Ticket).where(Ticket.id == ticket.id).values(status="closed"))
    session.commit()

    response = _cancel(client, customer, ticket.ticket_number, {"expected_status": "received"})

    assert (response.status_code, _code(response)) == (409, "ticket_status_changed")
    assert _stored(session, ticket.ticket_number).status == "closed"


@pytest.mark.parametrize(
    "body", [{}, {"expected_status": None}, {"expected_status": "done"}, {"expected_status": 1}]
)
def test_a_missing_or_invalid_expected_status_is_422_and_changes_nothing(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    make_ticket,
    body: dict[str, Any],
) -> None:
    ticket = make_ticket(customer, category)
    before = _stored(session, ticket.ticket_number)

    response = _cancel(client, customer, ticket.ticket_number, body)

    assert response.status_code == 422
    assert response.json() == {"detail": {"error": {"code": "invalid_expected_status"}}}
    assert _stored(session, ticket.ticket_number) == before


def test_a_cancel_without_a_body_is_422(
    client: TestClient, session: Session, customer: Customer, category: Category, make_ticket
) -> None:
    ticket = make_ticket(customer, category)

    response = client.post(f"{URL}/tickets/{ticket.ticket_number}/cancel", headers=_bearer(customer))

    assert response.status_code == 422
    assert _stored(session, ticket.ticket_number).status == "received"


def test_a_customer_cannot_name_a_target_status(
    client: TestClient, session: Session, customer: Customer, category: Category, make_ticket
) -> None:
    ticket = make_ticket(customer, category, status="received")

    response = _cancel(
        client, customer, ticket.ticket_number, {"expected_status": "received", "target_status": "closed"}
    )

    assert (response.status_code, _code(response)) == (422, "unknown_field")
    assert _stored(session, ticket.ticket_number).status == "received"


def test_a_customer_token_cannot_reach_the_staff_status_route(
    session: Session, customer: Customer, category: Category, make_ticket, numbers: Numbers
) -> None:
    from app.service_tickets.api import staff_router  # imported late: another task edits that module

    ticket = make_ticket(customer, category, status="received")
    app = _build_app(session, numbers)
    app.include_router(staff_router.router, prefix=URL)

    with TestClient(app, raise_server_exceptions=False) as client:
        for target in ("in_progress", "closed", "received"):
            response = client.post(
                f"{URL}/staff/tickets/{ticket.ticket_number}/status",
                json={"expected_status": "received", "target_status": target},
                headers=_bearer(customer),
            )
            assert (response.status_code, _code(response)) == (403, "customer_token_not_accepted")

    assert _stored(session, ticket.ticket_number).status == "received"


def test_cancel_of_a_foreign_missing_or_malformed_ticket_is_the_same_404(
    client: TestClient, session: Session, make_customer, make_ticket, category: Category
) -> None:
    mine = make_customer()
    other = make_customer()
    foreign = make_ticket(other, category)
    body = {"expected_status": "received"}

    responses = [
        _cancel(client, mine, foreign.ticket_number, body),
        _cancel(client, mine, 99999, body),
        _cancel(client, mine, "abc", body),
        _cancel(client, mine, "1234", body),
    ]

    for response in responses:
        assert response.status_code == 404
        assert response.json() == {"detail": {"error": {"code": "ticket_not_found"}}}
    assert len({response.content for response in responses}) == 1
    assert _stored(session, foreign.ticket_number).status == "received"


# --------------------------------------------------------------------------- the password gate


def test_while_a_password_change_is_owed_every_ticket_endpoint_is_403_and_changes_nothing(
    client: TestClient,
    session: Session,
    make_customer,
    make_ticket,
    category: Category,
    numbers: Numbers,
) -> None:
    owing = make_customer(must_change_password=True)
    ticket = make_ticket(owing, category)
    before = _stored(session, ticket.ticket_number)
    headers = _bearer(owing)
    number = ticket.ticket_number

    responses = [
        client.get(f"{URL}/form-config", headers=headers),
        client.get(f"{URL}/tickets", headers=headers),
        client.get(f"{URL}/tickets/{number}", headers=headers),
        client.post(f"{URL}/tickets/{number}/cancel", json={"expected_status": "received"}, headers=headers),
        client.post(f"{URL}/tickets", files=_form(_good(category)), headers=headers),
    ]

    for response in responses:
        assert response.status_code == 403
        assert response.json() == {"detail": {"error": {"code": "password_change_required"}}}
    assert _stored(session, number) == before
    assert _ticket_count(session) == 1
    assert numbers.calls == 0
    assert client.get(f"{URL}/auth/me", headers=headers).status_code == 200


def test_every_ticket_endpoint_needs_a_token(client: TestClient, category: Category) -> None:
    responses = [
        client.get(f"{URL}/form-config"),
        client.get(f"{URL}/tickets"),
        client.get(f"{URL}/tickets/10000"),
        client.post(f"{URL}/tickets/10000/cancel", json={"expected_status": "received"}),
        client.post(f"{URL}/tickets", files=_form(_good(category))),
    ]

    for response in responses:
        assert (response.status_code, _code(response)) == (401, "not_authenticated")


def test_a_revoked_token_is_401_on_the_ticket_endpoints(
    client: TestClient, session: Session, customer: Customer, category: Category
) -> None:
    headers = _bearer(customer)
    session.execute(update(Customer).where(Customer.id == customer.id).values(token_version=2))
    session.commit()

    for response in (
        client.get(f"{URL}/tickets", headers=headers),
        client.post(f"{URL}/tickets", files=_form(_good(category)), headers=headers),
    ):
        assert (response.status_code, _code(response)) == (401, "invalid_token")
    assert _ticket_count(session) == 0
