"""The photo on a ticket: submission and delivery (task 16.4; R11.1, R11.4, R11.8 to R11.15, R12.2).

The application is the real one (`create_app` with the flag on), so the response headers of the real
middleware stack are the ones under test: the delivery route must not set `X-Content-Type-Options`
itself, because `SecurityHeadersMiddleware` appends it and a second value would be sent. Each request
gets its own session on the in-memory test database, tokens are real, SQLite has no sequence so the
ticket-number allocator is an override, and the object store is `FakePhotoStore`, an in-memory stand-in
that records every call. Nothing reaches a real object store.

`FakePhotoStore` is shared with the other ticket test files that need a store.
"""

from __future__ import annotations

import itertools
import logging
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import event, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.main import create_app
from app.models.user import UserRole
from app.service_tickets.api import customer_router
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.models import Category, Customer, ServiceSettings, Ticket
from app.service_tickets.photo import MAX_PHOTO_BYTES
from app.service_tickets.photo_store import (
    KEY_PREFIX,
    PhotoNotFound,
    PhotoStorageUnavailable,
    check_key,
    get_photo_store,
)
from app.service_tickets.services.numbering import TicketNumbersExhausted

API = "/api/service"
PHONE = "0505556677"
KEY_PATTERN = re.compile(r"^service-tickets/[0-9a-f]{32}\.jpg$")


# --------------------------------------------------------------------------- the fake store


class FakePhotoStore:
    """An in-memory `PhotoStore`: same three operations, same key check, every call recorded.

    `ops` holds (operation, key) in call order. `seed` puts an object in without recording a call, for
    tests that start from a stored photo. `fail_put` and `fail_get` make that operation raise
    `PhotoStorageUnavailable` on every call, as the real store does when the object store is down;
    `fail_put_once` and `fail_get_once` do it for the next call only.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.ops: list[tuple[str, str]] = []
        self.fail_put = False
        self.fail_get = False
        self.fail_put_once = False
        self.fail_get_once = False

    def seed(self, key: str, data: bytes) -> None:
        check_key(key)
        self.objects[key] = data

    def put(self, key: str, data: bytes) -> None:
        check_key(key)
        self.ops.append(("put", key))
        if self.fail_put or self.fail_put_once:
            self.fail_put_once = False
            raise PhotoStorageUnavailable
        self.objects[key] = bytes(data)

    def get(self, key: str) -> bytes:
        check_key(key)
        self.ops.append(("get", key))
        if self.fail_get or self.fail_get_once:
            self.fail_get_once = False
            raise PhotoStorageUnavailable
        if key not in self.objects:
            raise PhotoNotFound
        return self.objects[key]

    def delete(self, key: str) -> bool:
        check_key(key)
        self.ops.append(("delete", key))
        self.objects.pop(key, None)
        return True

    def keys(self, operation: str) -> list[str]:
        return [key for name, key in self.ops if name == operation]


# --------------------------------------------------------------------------- images


def jpeg_bytes(size: tuple[int, int] = (40, 30)) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(out, "JPEG")
    return out.getvalue()


def png_bytes(size: tuple[int, int] = (20, 20)) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(out, "PNG")
    return out.getvalue()


def too_many_pixels_png() -> bytes:
    """A PNG of 50,000,001 pixels: one bit per pixel, so it is a few KB."""
    out = BytesIO()
    Image.new("1", (16_666_667, 3), 1).save(out, "PNG")
    return out.getvalue()


# --------------------------------------------------------------------------- harness


class Numbers:
    """The ticket-number allocator of the tests. `exhausted` makes every call fail."""

    def __init__(self) -> None:
        self.next = 10000
        self.calls = 0
        self.exhausted = False

    def __call__(self, _session: Session) -> int:
        self.calls += 1
        if self.exhausted:
            raise TicketNumbersExhausted
        number = self.next
        self.next += 1
        return number


@dataclass
class Faults:
    """Failures the request session is told to produce."""

    commit_fails: bool = False


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
def faults() -> Faults:
    return Faults()


@pytest.fixture
def app(settings, session: Session, numbers: Numbers, photos: FakePhotoStore, faults: Faults) -> FastAPI:
    changes = {"service_tickets_enabled": "true", "service_accessibility_contact": "a@example.test"}
    application = create_app(settings.model_copy(update={"api_prefix": "/api", **changes}))
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True)

    def request_session() -> Iterator[Session]:
        with factory() as per_request:
            if faults.commit_fails:

                def failing_commit() -> None:
                    raise DBAPIError("COMMIT", {}, Exception("connection lost"))

                per_request.commit = failing_commit  # type: ignore[method-assign]
            yield per_request

    application.dependency_overrides[get_db] = request_session
    application.dependency_overrides[customer_router.get_ticket_allocator] = lambda: numbers
    application.dependency_overrides[get_photo_store] = lambda: photos
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password("4321")


@pytest.fixture
def make_customer(session: Session, password_hash: str):
    made = itertools.count(1)

    def _make(**overrides: Any) -> Customer:
        index = next(made)
        fields: dict[str, Any] = {
            "phone": f"05055500{index:02d}",
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
def set_uploads(session: Session):
    """Switch photo uploads on or off in the stored settings."""

    def _set(enabled: bool) -> None:
        row = session.get(ServiceSettings, 1, populate_existing=True)
        if row is None:
            session.add(ServiceSettings(id=1, description_required=True, photo_upload_enabled=enabled))
        else:
            row.photo_upload_enabled = enabled
        session.commit()

    return _set


@pytest.fixture
def make_ticket(session: Session):
    counter = itertools.count(20000)

    def _make(owner: Customer, category: Category, **overrides: Any) -> Ticket:
        created_at = datetime(2025, 3, 1, 8, 0, tzinfo=UTC)
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


@pytest.fixture
def photo_ticket(make_ticket, photos: FakePhotoStore):
    """A ticket that has a stored photo. Returns (ticket, stored bytes)."""

    def _make(owner: Customer, category: Category, *, stored: bool = True) -> tuple[Ticket, bytes]:
        key = f"{KEY_PREFIX}{uuid.uuid4().hex}.jpg"
        data = jpeg_bytes()
        if stored:
            photos.seed(key, data)
        return make_ticket(owner, category, photo_key=key), data

    return _make


# --------------------------------------------------------------------------- helpers


def _bearer(owner: Customer) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_customer_token(owner.id, owner.token_version)}"}


@pytest.fixture
def staff_headers(make_user):
    def _headers(role: UserRole) -> dict[str, str]:
        user = make_user(role=role, is_2fa_enabled=True, must_change_password=False)
        token = create_access_token(user_id=user.id, token_version=user.token_version, role=role.value)
        return {"Authorization": f"Bearer {token}"}

    return _headers


def _code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


def _fields(response: Any) -> dict[str, str]:
    return response.json()["detail"]["error"]["fields"]


def _form(fields: dict[str, Any]) -> list[Any]:
    return [(name, (None, value)) for name, value in fields.items()]


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


def _photo_part(data: bytes, name: str = "photo", filename: str = "IMG_0001.JPG") -> tuple[str, Any]:
    return (name, (filename, data, "image/jpeg"))


def _submit(client: TestClient, owner: Customer, fields: dict[str, Any], *photo_parts: Any) -> Any:
    return client.post(f"{API}/tickets", files=[*_form(fields), *photo_parts], headers=_bearer(owner))


def _photo_url(number: int | str) -> str:
    return f"{API}/tickets/{number}/photo"


def _ticket_count(session: Session) -> int:
    session.rollback()
    return session.execute(select(func.count()).select_from(Ticket)).scalar_one()


def _photo_key_of(session: Session, number: int) -> str | None:
    session.rollback()
    return session.execute(select(Ticket.photo_key).where(Ticket.ticket_number == number)).scalar_one()


# --------------------------------------------------------------------------- submission: success


def test_a_valid_photo_is_stored_as_a_clean_jpeg_and_the_ticket_points_at_it(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
    numbers: Numbers,
) -> None:
    response = _submit(
        client, customer, _good(category), _photo_part(png_bytes((3000, 2000)), filename="secret name.png")
    )

    assert response.status_code == 201
    number = response.json()["ticket_number"]
    key = _photo_key_of(session, number)
    assert key is not None and KEY_PATTERN.match(key)
    assert "secret" not in key and "png" not in key
    assert photos.ops == [("put", key)]
    stored = Image.open(BytesIO(photos.objects[key]))
    assert stored.format == "JPEG"
    assert max(stored.size) == 1600
    assert numbers.calls == 1
    detail = client.get(f"{API}/tickets/{number}", headers=_bearer(customer)).json()
    assert detail["has_photo"] is True
    assert key not in str(detail)


def test_a_submission_without_a_photo_touches_no_store_and_keeps_no_key(
    client: TestClient, session: Session, customer: Customer, category: Category, photos: FakePhotoStore
) -> None:
    response = _submit(client, customer, _good(category))

    assert response.status_code == 201
    assert _photo_key_of(session, response.json()["ticket_number"]) is None
    assert photos.ops == []


def test_the_owner_sees_the_photo_they_just_uploaded(
    client: TestClient, customer: Customer, category: Category
) -> None:
    number = _submit(client, customer, _good(category), _photo_part(jpeg_bytes())).json()["ticket_number"]

    response = client.get(_photo_url(number), headers=_bearer(customer))

    assert response.status_code == 200
    assert response.content[:2] == b"\xff\xd8"


# --------------------------------------------------------------------------- submission: 413


def test_an_oversized_photo_is_413_before_any_other_check_and_stores_nothing(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
    numbers: Numbers,
    set_uploads,
) -> None:
    set_uploads(False)  # `photo_upload_disabled` would also apply
    broken = {"city": "", "phone": "12", "category_id": "x", "urgent": "maybe", "surprise": "1"}
    oversized = _photo_part(b"\x00" * (MAX_PHOTO_BYTES + 1))

    response = _submit(client, customer, broken, oversized)

    assert response.status_code == 413
    assert response.json() == {"detail": {"error": {"code": "photo_too_large"}}}
    assert _ticket_count(session) == 0
    assert (photos.ops, numbers.calls) == ([], 0)


def test_the_size_limit_is_inclusive_and_a_part_at_the_limit_is_judged_on_content(
    client: TestClient, session: Session, customer: Customer, category: Category
) -> None:
    at_limit = _photo_part(b"\x00" * MAX_PHOTO_BYTES)

    response = _submit(client, customer, _good(category), at_limit)

    assert response.status_code == 422
    assert _fields(response) == {"photo": "invalid_photo_type"}
    assert _ticket_count(session) == 0


def test_a_file_part_under_another_name_is_also_held_to_the_size_limit(
    client: TestClient, session: Session, customer: Customer, category: Category
) -> None:
    response = _submit(
        client, customer, _good(category), _photo_part(b"\x00" * (MAX_PHOTO_BYTES + 1), name="attachment")
    )

    assert (response.status_code, _code(response)) == (413, "photo_too_large")
    assert _ticket_count(session) == 0


# --------------------------------------------------------------------------- submission: uploads off


@pytest.mark.parametrize(
    "content",
    [jpeg_bytes(), b"not an image", b""],
    ids=["valid", "invalid", "empty"],
)
def test_a_photo_while_uploads_are_off_is_photo_upload_disabled_whatever_it_holds(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
    numbers: Numbers,
    set_uploads,
    content: bytes,
) -> None:
    set_uploads(False)

    response = _submit(client, customer, _good(category), _photo_part(content))

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == {
        "code": "photo_upload_disabled",
        "fields": {"photo": "photo_upload_disabled"},
    }
    assert _ticket_count(session) == 0
    assert (photos.ops, numbers.calls) == ([], 0)


def test_a_submission_without_a_photo_still_works_while_uploads_are_off(
    client: TestClient, customer: Customer, category: Category, set_uploads
) -> None:
    set_uploads(False)

    assert _submit(client, customer, _good(category)).status_code == 201


def test_switching_uploads_off_keeps_existing_photos_viewable_by_the_owner_and_by_staff(
    client: TestClient,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
    set_uploads,
    staff_headers,
) -> None:
    number = _submit(client, customer, _good(category), _photo_part(jpeg_bytes())).json()["ticket_number"]
    key = photos.keys("put")[0]
    set_uploads(False)

    views = [
        client.get(_photo_url(number), headers=_bearer(customer)),
        client.get(_photo_url(number), headers=staff_headers(UserRole.ADMIN)),
        client.get(_photo_url(number), headers=staff_headers(UserRole.OPERATIONS_ADMIN)),
        client.get(_photo_url(number), headers=staff_headers(UserRole.TECHNICIAN)),
    ]

    assert [view.status_code for view in views] == [200, 200, 200, 200]
    assert {view.content for view in views} == {photos.objects[key]}


# --------------------------------------------------------------------------- submission: field errors


def test_two_file_parts_are_too_many_photos_and_store_nothing(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
    numbers: Numbers,
) -> None:
    two = (_photo_part(jpeg_bytes()), _photo_part(jpeg_bytes()))

    response = _submit(client, customer, _good(category), *two)

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == {
        "code": "too_many_photos",
        "fields": {"photo": "too_many_photos"},
    }
    assert _ticket_count(session) == 0
    assert (photos.ops, numbers.calls) == ([], 0)


def test_a_photo_that_is_not_an_image_and_a_bad_field_are_reported_together(
    client: TestClient, customer: Customer, category: Category
) -> None:
    response = _submit(
        client, customer, _good(category, city="", surprise="1"), _photo_part(b"%PDF-1.7 not a photo")
    )

    assert response.status_code == 422
    error = response.json()["detail"]["error"]
    assert error["fields"] == {
        "city": "invalid_city",
        "photo": "invalid_photo_type",
        "surprise": "unknown_field",
    }
    assert list(error["fields"]) == ["city", "photo", "surprise"]  # form order, unknown fields last
    assert error["code"] == "invalid_city"


def test_a_photo_with_too_many_pixels_and_a_bad_field_are_reported_together(
    client: TestClient, customer: Customer, category: Category
) -> None:
    response = _submit(client, customer, _good(category, phone="12"), _photo_part(too_many_pixels_png()))

    assert response.status_code == 422
    assert _fields(response) == {"phone": "invalid_phone", "photo": "photo_dimensions_too_large"}


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", "invalid_photo_type"),
        (b"GIF89a" + b"\x00" * 40, "invalid_photo_type"),
        (jpeg_bytes()[:60], "invalid_photo_type"),
        (too_many_pixels_png(), "photo_dimensions_too_large"),
    ],
    ids=["zero-bytes", "gif", "truncated-jpeg", "50-million-pixels"],
)
def test_an_unusable_photo_alone_is_a_422_that_names_only_the_photo(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
    numbers: Numbers,
    content: bytes,
    code: str,
) -> None:
    response = _submit(client, customer, _good(category), _photo_part(content))

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == {"code": code, "fields": {"photo": code}}
    assert (_ticket_count(session), photos.ops, numbers.calls) == (0, [], 0)


def test_the_file_name_and_declared_type_do_not_decide(
    client: TestClient, customer: Customer, category: Category
) -> None:
    misleading = ("photo", ("not-a-picture.txt", jpeg_bytes(), "text/plain"))

    response = _submit(client, customer, _good(category), misleading)

    assert response.status_code == 201


def test_a_failing_field_stores_nothing_and_takes_no_number_even_with_a_valid_photo(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
    numbers: Numbers,
) -> None:
    response = _submit(client, customer, _good(category, apartment=""), _photo_part(jpeg_bytes()))

    assert response.status_code == 422
    assert _fields(response) == {"apartment": "invalid_apartment"}
    assert (_ticket_count(session), photos.ops, numbers.calls) == (0, [], 0)


@pytest.mark.parametrize("uploads_on", [True, False])
def test_a_text_field_named_photo_is_not_a_photo_and_is_unknown_field(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
    set_uploads,
    uploads_on: bool,
) -> None:
    set_uploads(uploads_on)

    response = _submit(client, customer, {**_good(category), "photo": "not a file"})

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == {
        "code": "unknown_field",
        "fields": {"photo": "unknown_field"},
    }
    assert (_ticket_count(session), photos.ops) == (0, [])


def test_a_file_part_under_an_unknown_name_is_unknown_field_and_is_not_a_photo(
    client: TestClient, session: Session, customer: Customer, category: Category, photos: FakePhotoStore
) -> None:
    response = _submit(client, customer, _good(category), _photo_part(jpeg_bytes(), name="attachment"))

    assert response.status_code == 422
    assert _fields(response) == {"attachment": "unknown_field"}
    assert (_ticket_count(session), photos.ops) == (0, [])


# --------------------------------------------------------------------------- submission: atomicity


def test_a_failing_put_is_503_with_no_ticket_and_nothing_stored(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
) -> None:
    photos.fail_put = True

    response = _submit(client, customer, _good(category), _photo_part(jpeg_bytes()))

    assert response.status_code == 503
    assert response.json() == {"detail": {"error": {"code": "photo_storage_unavailable"}}}
    assert _ticket_count(session) == 0
    assert photos.objects == {}
    assert [name for name, _ in photos.ops] == ["put"]  # nothing was stored, so nothing is deleted


def test_a_failing_insert_deletes_the_stored_photo(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_insert(*_args: Any, **_kwargs: Any) -> Ticket:
        raise DBAPIError("INSERT", {}, Exception("constraint failed"))

    monkeypatch.setattr(customer_router, "create_ticket", broken_insert)

    response = _submit(client, customer, _good(category), _photo_part(jpeg_bytes()))

    assert response.status_code == 503
    assert _code(response) == "service_unavailable"
    key = photos.keys("put")[0]
    assert [op for op, _ in photos.ops] == ["put", "delete"]
    assert photos.keys("delete") == [key]
    assert photos.objects == {}
    assert _ticket_count(session) == 0


def test_a_failing_commit_deletes_the_stored_photo_and_leaves_no_ticket(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
    faults: Faults,
) -> None:
    faults.commit_fails = True

    response = _submit(client, customer, _good(category), _photo_part(jpeg_bytes()))

    assert response.status_code == 503
    assert [op for op, _ in photos.ops] == ["put", "delete"]
    assert photos.keys("delete") == photos.keys("put")
    assert photos.objects == {}
    assert _ticket_count(session) == 0


def test_exhausted_ticket_numbers_store_no_photo(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
    numbers: Numbers,
) -> None:
    numbers.exhausted = True

    response = _submit(client, customer, _good(category), _photo_part(jpeg_bytes()))

    assert response.status_code == 503
    assert _code(response) == "ticket_numbers_exhausted"
    assert (photos.ops, _ticket_count(session)) == ([], 0)


def test_the_number_is_allocated_before_the_photo_is_stored_and_after_validation(
    client: TestClient,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
    numbers: Numbers,
) -> None:
    seen: list[int] = []
    original_put = photos.put

    def spying_put(key: str, data: bytes) -> None:
        seen.append(numbers.calls)
        original_put(key, data)

    photos.put = spying_put  # type: ignore[method-assign]

    response = _submit(client, customer, _good(category), _photo_part(jpeg_bytes()))

    assert response.status_code == 201
    assert seen == [1]


# --------------------------------------------------------------------------- delivery: headers


def test_the_photo_response_carries_exactly_the_designed_headers(
    client: TestClient, customer: Customer, category: Category, photo_ticket
) -> None:
    ticket, data = photo_ticket(customer, category)

    response = client.get(_photo_url(ticket.ticket_number), headers=_bearer(customer))

    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]
    assert response.headers.get_list("cache-control") == ["private, no-store"]
    assert response.headers.get_list("content-type") == ["image/jpeg"]


def test_x_content_type_options_appears_once_for_staff_too(
    client: TestClient, customer: Customer, category: Category, photo_ticket, staff_headers
) -> None:
    ticket, _ = photo_ticket(customer, category)

    response = client.get(_photo_url(ticket.ticket_number), headers=staff_headers(UserRole.TECHNICIAN))

    assert response.status_code == 200
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]


def test_the_delivery_url_holds_no_key_and_no_url_is_exposed_to_the_customer(
    client: TestClient, customer: Customer, category: Category, photo_ticket
) -> None:
    ticket, _ = photo_ticket(customer, category)

    detail = client.get(f"{API}/tickets/{ticket.ticket_number}", headers=_bearer(customer))
    listing = client.get(f"{API}/tickets", headers=_bearer(customer))

    assert ticket.photo_key not in detail.text + listing.text
    assert "http" not in detail.text + listing.text


# --------------------------------------------------------------------------- delivery: who may see it


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.TECHNICIAN])
def test_staff_of_the_three_ticket_roles_view_any_photo(
    client: TestClient,
    make_customer,
    category: Category,
    photo_ticket,
    staff_headers,
    role: UserRole,
) -> None:
    ticket, data = photo_ticket(make_customer(), category)

    response = client.get(_photo_url(ticket.ticket_number), headers=staff_headers(role))

    assert response.status_code == 200
    assert response.content == data


def test_a_staff_role_the_tickets_do_not_serve_is_refused(
    client: TestClient, customer: Customer, category: Category, photo_ticket, staff_headers
) -> None:
    ticket, data = photo_ticket(customer, category)

    response = client.get(_photo_url(ticket.ticket_number), headers=staff_headers(UserRole.ACCOUNTING))

    assert (response.status_code, _code(response)) == (403, "insufficient_role")
    assert data not in response.content


def test_a_request_without_a_token_is_401(
    client: TestClient, customer: Customer, category: Category, photo_ticket
) -> None:
    ticket, _ = photo_ticket(customer, category)

    response = client.get(_photo_url(ticket.ticket_number))

    assert (response.status_code, _code(response)) == (401, "not_authenticated")


def test_another_customers_photo_is_a_404_and_the_store_is_not_read(
    client: TestClient, make_customer, category: Category, photo_ticket, photos: FakePhotoStore
) -> None:
    owner, other = make_customer(), make_customer()
    ticket, data = photo_ticket(owner, category)

    response = client.get(_photo_url(ticket.ticket_number), headers=_bearer(other))

    assert response.status_code == 404
    assert response.json() == {"detail": {"error": {"code": "ticket_not_found"}}}
    assert data not in response.content
    assert photos.ops == []


def test_the_missing_foreign_malformed_and_photoless_cases_are_byte_identical(
    client: TestClient, make_customer, make_ticket, category: Category, photo_ticket
) -> None:
    mine, other = make_customer(), make_customer()
    foreign, _ = photo_ticket(other, category)
    without_photo = make_ticket(mine, category)
    headers = _bearer(mine)

    responses = {
        "foreign": client.get(_photo_url(foreign.ticket_number), headers=headers),
        "missing": client.get(_photo_url(99999), headers=headers),
        "malformed": client.get(_photo_url("abc"), headers=headers),
        "short": client.get(_photo_url("1234"), headers=headers),
        "no photo": client.get(_photo_url(without_photo.ticket_number), headers=headers),
    }

    for name, response in responses.items():
        assert response.status_code == 404, name
        assert response.json() == {"detail": {"error": {"code": "ticket_not_found"}}}, name
    assert len({response.content for response in responses.values()}) == 1
    assert len({tuple(sorted(response.headers.items())) for response in responses.values()}) == 1


def test_a_ticket_without_a_photo_is_the_same_404_for_staff(
    client: TestClient, customer: Customer, make_ticket, category: Category, staff_headers
) -> None:
    ticket = make_ticket(customer, category)

    response = client.get(_photo_url(ticket.ticket_number), headers=staff_headers(UserRole.ADMIN))

    assert response.status_code == 404
    assert response.json() == {"detail": {"error": {"code": "ticket_not_found"}}}


def test_a_customer_who_owes_a_password_change_gets_403_on_the_photo_route(
    client: TestClient, make_customer, category: Category, photo_ticket, photos: FakePhotoStore
) -> None:
    owing = make_customer(must_change_password=True)
    ticket, _ = photo_ticket(owing, category)

    response = client.get(_photo_url(ticket.ticket_number), headers=_bearer(owing))

    assert response.status_code == 403
    assert response.json() == {"detail": {"error": {"code": "password_change_required"}}}
    assert photos.ops == []


# --------------------------------------------------------------------------- delivery: store failures


def test_a_missing_object_is_the_same_404_and_logs_the_ticket_number(
    client: TestClient,
    customer: Customer,
    category: Category,
    photo_ticket,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ticket, _ = photo_ticket(customer, category, stored=False)
    missing_key = ticket.photo_key
    reference = client.get(_photo_url(99999), headers=_bearer(customer))

    with caplog.at_level(logging.ERROR):
        response = client.get(_photo_url(ticket.ticket_number), headers=_bearer(customer))

    assert response.status_code == 404
    assert response.content == reference.content
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and "photo" in r.getMessage()]
    assert errors, "no error was logged for the missing object"
    assert any(str(ticket.ticket_number) in r.getMessage() for r in errors)
    assert all(missing_key not in r.getMessage() and PHONE not in r.getMessage() for r in errors)


def test_an_object_store_outage_is_503_photo_storage_unavailable(
    client: TestClient, customer: Customer, category: Category, photo_ticket, photos: FakePhotoStore
) -> None:
    ticket, _ = photo_ticket(customer, category)
    photos.fail_get = True

    response = client.get(_photo_url(ticket.ticket_number), headers=_bearer(customer))

    assert response.status_code == 503
    assert response.json() == {"detail": {"error": {"code": "photo_storage_unavailable"}}}
