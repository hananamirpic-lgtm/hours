"""Staff open a ticket for a customer (task 29.4; R26.3 to R26.6, R26.8, R1.9; design.md, "Staff-opened
tickets and the Resolved name").

`POST /staff/tickets` and `GET /staff/form-config`. The application is the real one (`create_app` with the
flag on), so the body guard, the header middleware and the router order are the production ones. Each
request gets its own session on the in-memory test database, tokens are real, SQLite has no sequence so
the ticket-number allocator is an override, and the object store is the in-memory `FakePhotoStore`.

The tests that compare the staff answer with the customer answer send the same invalid form down both
paths: R26.3 says a failure has "the same HTTP status and error codes", so the two bodies must be equal.
"""

from __future__ import annotations

import itertools
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from test_service_api_photo import FakePhotoStore, Numbers, jpeg_bytes

from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.main import create_app
from app.models.user import UserRole
from app.service_tickets import rate_limit
from app.service_tickets.api import customer_router
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.models import Category, Customer, ServiceSettings, Ticket
from app.service_tickets.photo import MAX_PHOTO_BYTES
from app.service_tickets.photo_store import get_photo_store

API = "/api/service"
STAFF_TICKETS = f"{API}/staff/tickets"
STAFF_FORM_CONFIG = f"{API}/staff/form-config"
KEY_PATTERN = re.compile(r"^service-tickets/[0-9a-f]{32}\.jpg$")

NOT_FOUND = {"detail": {"error": {"code": "customer_not_found"}}}
BAD_CUSTOMER_ID = {
    "detail": {"error": {"code": "customer_not_found", "fields": {"customer_id": "customer_not_found"}}}
}
INACTIVE = {"detail": {"error": {"code": "customer_inactive"}}}


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


def _build(settings, session: Session, numbers: Numbers, photos: FakePhotoStore, *, enabled: bool) -> FastAPI:
    changes = {
        "service_tickets_enabled": "true" if enabled else "",
        "service_accessibility_contact": "a@example.test",
        "api_prefix": "/api",
    }
    application = create_app(settings.model_copy(update=changes))
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True)

    def request_session() -> Iterator[Session]:
        with factory() as per_request:
            yield per_request

    application.dependency_overrides[get_db] = request_session
    application.dependency_overrides[customer_router.get_ticket_allocator] = lambda: numbers
    application.dependency_overrides[get_photo_store] = lambda: photos
    return application


@pytest.fixture
def client(settings, session: Session, numbers: Numbers, photos: FakePhotoStore) -> Iterator[TestClient]:
    app = _build(settings, session, numbers, photos, enabled=True)
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
    def _set(enabled: bool) -> None:
        row = session.get(ServiceSettings, 1, populate_existing=True)
        if row is None:
            session.add(ServiceSettings(id=1, description_required=True, photo_upload_enabled=enabled))
        else:
            row.photo_upload_enabled = enabled
        session.commit()

    return _set


@pytest.fixture
def staff(make_user):
    """`staff(role)` is a committed, enrolled user and its bearer header."""

    def _staff(role: UserRole = UserRole.TECHNICIAN):
        user = make_user(role=role, is_2fa_enabled=True, must_change_password=False)
        token = create_access_token(user_id=user.id, token_version=user.token_version, role=role.value)
        return user, {"Authorization": f"Bearer {token}"}

    return _staff


# --------------------------------------------------------------------------- helpers


def _customer_headers(owner: Customer) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_customer_token(owner.id, owner.token_version)}"}


def _parts(fields: dict[str, Any]) -> list[Any]:
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


def _photo(data: bytes, name: str = "photo") -> tuple[str, Any]:
    return (name, ("IMG_0001.JPG", data, "image/jpeg"))


def _open(
    client: TestClient,
    headers: dict[str, str],
    owner: Customer | str | None,
    category: Category,
    *extra: Any,
    **overrides: Any,
) -> Any:
    """A staff submission for `owner` (a customer, a raw `customer_id` text, or `None` for no part)."""
    fields = _good(category, **overrides)
    if owner is not None:
        fields = {"customer_id": owner if isinstance(owner, str) else str(owner.id), **fields}
    return client.post(STAFF_TICKETS, files=[*_parts(fields), *extra], headers=headers)


def _submit_as_customer(
    client: TestClient, owner: Customer, category: Category, *extra: Any, **kw: Any
) -> Any:
    return client.post(
        f"{API}/tickets",
        files=[*_parts(_good(category, **kw)), *extra],
        headers=_customer_headers(owner),
    )


def _code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


def _stored(session: Session, number: int) -> Any:
    session.rollback()
    return session.execute(select(Ticket).where(Ticket.ticket_number == number)).scalar_one()


def _ticket_count(session: Session) -> int:
    session.rollback()
    return session.execute(select(func.count()).select_from(Ticket)).scalar_one()


def _customer_row(session: Session, owner: Customer) -> tuple[Any, ...]:
    session.rollback()
    row = session.execute(
        select(
            Customer.phone,
            Customer.full_name,
            Customer.city,
            Customer.apartment,
            Customer.is_active,
            Customer.token_version,
            Customer.must_change_password,
            Customer.updated_at,
            Customer.password_hash,
        ).where(Customer.id == owner.id)
    ).one()
    return tuple(row)


def _nothing_happened(session: Session, numbers: Numbers, photos: FakePhotoStore) -> None:
    assert _ticket_count(session) == 0
    assert numbers.calls == 0
    assert photos.ops == []


# --------------------------------------------------------------------------- success (R26.4)


def test_a_staff_member_opens_a_ticket_without_a_photo(
    client: TestClient,
    session: Session,
    staff,
    customer: Customer,
    category: Category,
    numbers: Numbers,
    photos: FakePhotoStore,
) -> None:
    user, headers = staff(UserRole.TECHNICIAN)
    before = _customer_row(session, customer)

    response = _open(client, headers, customer, category, city="Elsewhere", apartment="7", urgent="TRUE")

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["ticket_number"] == 10000
    assert body["status"] == "received"
    assert body["customer_name"] == "Avi Cohen"
    assert body["city"] == "Elsewhere"
    assert body["apartment"] == "7"
    assert body["urgent"] is True
    assert body["phone"] == "0505556677"
    assert body["has_photo"] is False
    assert body["assigned_technician_name"] == ""
    assert body["history"] == []
    assert body["category"]["id"] == str(category.id)
    assert body["opened_by_username"] == user.username
    stored = _stored(session, 10000)
    assert stored.customer_id == customer.id
    assert stored.opened_by_username == user.username
    assert stored.photo_key is None
    assert (stored.city, stored.apartment) == ("Elsewhere", "7")  # what staff typed, not the record's
    assert numbers.calls == 1
    assert photos.ops == []
    assert _customer_row(session, customer) == before  # R26.4: the Customer_Record is unchanged
    # The answer is the staff detail of the new ticket, as the staff detail route returns it.
    assert client.get(f"{STAFF_TICKETS}/10000", headers=headers).json() == body


def test_a_staff_member_opens_a_ticket_with_a_photo(
    client: TestClient,
    session: Session,
    staff,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
) -> None:
    _user, headers = staff(UserRole.OPERATIONS_ADMIN)
    data = jpeg_bytes()

    response = _open(client, headers, customer, category, _photo(data))

    assert response.status_code == 201
    assert response.json()["has_photo"] is True
    stored = _stored(session, response.json()["ticket_number"])
    assert stored.photo_key is not None
    assert KEY_PATTERN.fullmatch(stored.photo_key)
    assert photos.keys("put") == [stored.photo_key]
    assert photos.keys("delete") == []
    assert stored.photo_key in photos.objects


def test_numbers_rise_with_each_staff_submission(
    client: TestClient, staff, customer: Customer, category: Category
) -> None:
    _user, headers = staff()

    first = _open(client, headers, customer, category)
    second = _open(client, headers, customer, category)

    assert (first.json()["ticket_number"], second.json()["ticket_number"]) == (10000, 10001)


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.TECHNICIAN])
def test_every_ticket_staff_role_may_open_a_ticket_and_is_recorded(
    client: TestClient, session: Session, staff, customer: Customer, category: Category, role: UserRole
) -> None:
    user, headers = staff(role)

    response = _open(client, headers, customer, category)

    assert response.status_code == 201
    assert response.json()["opened_by_username"] == user.username
    assert _stored(session, response.json()["ticket_number"]).opened_by_username == user.username


def test_the_staff_username_comes_from_the_token_and_never_from_the_request(
    client: TestClient,
    session: Session,
    staff,
    customer: Customer,
    category: Category,
    numbers: Numbers,
    photos: FakePhotoStore,
) -> None:
    _user, headers = staff()

    response = _open(client, headers, customer, category, opened_by_username="someone-else")

    assert response.status_code == 422
    assert _code(response) == "unknown_field"
    assert response.json()["detail"]["error"]["fields"] == {"opened_by_username": "unknown_field"}
    _nothing_happened(session, numbers, photos)


@pytest.mark.parametrize("name", ["owner_id", "status", "ticket_number", "created_at"])
def test_a_form_value_cannot_set_the_owner_the_status_or_the_number(
    client: TestClient, session: Session, staff, customer: Customer, category: Category, name: str
) -> None:
    _user, headers = staff()

    response = _open(client, headers, customer, category, **{name: "x"})

    assert response.status_code == 422
    assert response.json()["detail"]["error"]["fields"] == {name: "unknown_field"}
    assert _ticket_count(session) == 0


# ------------------------------------------------------------ validation equals the customer path (R26.3)

INVALID_FORMS: dict[str, dict[str, str]] = {
    "every field wrong": {
        "city": "",
        "apartment": " ",
        "phone": "12",
        "category_id": "nope",
        "description": "",
        "urgent": "maybe",
    },
    "city only": {"city": "x" * 101},
    "apartment only": {"apartment": "y" * 21},
    "phone only": {"phone": "050-555"},
    "inactive or unknown category": {"category_id": "00000000-0000-4000-8000-000000000001"},
    "description too long": {"description": "d" * 2001},
    "urgent": {"urgent": "perhaps"},
    "unknown part": {"surprise": "1"},
    "unknown part and a bad field": {"surprise": "1", "city": ""},
}


@pytest.mark.parametrize("overrides", INVALID_FORMS.values(), ids=INVALID_FORMS.keys())
def test_a_validation_failure_is_the_same_422_as_on_the_customer_path(
    client: TestClient,
    session: Session,
    staff,
    customer: Customer,
    category: Category,
    numbers: Numbers,
    photos: FakePhotoStore,
    overrides: dict[str, str],
) -> None:
    _user, headers = staff()

    as_staff = _open(client, headers, customer, category, **overrides)
    as_customer = _submit_as_customer(client, customer, category, **overrides)

    assert as_staff.status_code == 422
    assert as_customer.status_code == 422
    assert as_staff.json() == as_customer.json()
    _nothing_happened(session, numbers, photos)


def test_the_description_rule_follows_the_setting_on_both_paths(
    client: TestClient, session: Session, staff, customer: Customer, category: Category
) -> None:
    _user, headers = staff()
    session.add(ServiceSettings(id=1, description_required=False, photo_upload_enabled=True))
    session.commit()

    response = _open(client, headers, customer, category, description="  ")

    assert response.status_code == 201
    assert _stored(session, response.json()["ticket_number"]).description == ""


PHOTO_CASES: dict[str, Any] = {
    "not an image": lambda: (_photo(b"not an image"),),
    "two photos": lambda: (_photo(jpeg_bytes()), _photo(jpeg_bytes())),
    "a file part under another name": lambda: (_photo(jpeg_bytes(), name="attachment"),),
}


@pytest.mark.parametrize("make_parts", PHOTO_CASES.values(), ids=PHOTO_CASES.keys())
def test_a_photo_failure_is_the_same_422_as_on_the_customer_path(
    client: TestClient,
    session: Session,
    staff,
    customer: Customer,
    category: Category,
    numbers: Numbers,
    photos: FakePhotoStore,
    make_parts: Any,
) -> None:
    _user, headers = staff()

    as_staff = _open(client, headers, customer, category, *make_parts())
    as_customer = _submit_as_customer(client, customer, category, *make_parts())

    assert as_staff.status_code == as_customer.status_code == 422
    assert as_staff.json() == as_customer.json()
    _nothing_happened(session, numbers, photos)


def test_a_photo_while_uploads_are_off_is_the_same_422_as_on_the_customer_path(
    client: TestClient,
    session: Session,
    staff,
    customer: Customer,
    category: Category,
    set_uploads,
    numbers: Numbers,
    photos: FakePhotoStore,
) -> None:
    _user, headers = staff()
    set_uploads(False)

    as_staff = _open(client, headers, customer, category, _photo(jpeg_bytes()))
    as_customer = _submit_as_customer(client, customer, category, _photo(jpeg_bytes()))

    assert as_staff.status_code == as_customer.status_code == 422
    assert as_staff.json() == as_customer.json()
    assert as_staff.json()["detail"]["error"]["fields"] == {"photo": "photo_upload_disabled"}
    _nothing_happened(session, numbers, photos)


def test_a_photo_over_ten_mebibytes_is_the_same_413_as_on_the_customer_path(
    client: TestClient,
    session: Session,
    staff,
    customer: Customer,
    category: Category,
    numbers: Numbers,
    photos: FakePhotoStore,
) -> None:
    _user, headers = staff()
    big = _photo(b"\xff" * (MAX_PHOTO_BYTES + 1))

    as_staff = _open(client, headers, customer, category, big)
    as_customer = _submit_as_customer(client, customer, category, big)

    assert as_staff.status_code == as_customer.status_code == 413
    assert _code(as_staff) == "photo_too_large"
    assert as_staff.json() == as_customer.json()
    _nothing_happened(session, numbers, photos)


def test_a_json_body_has_no_customer_id_and_is_the_first_check_that_fails(
    client: TestClient, session: Session, staff, category: Category
) -> None:
    _user, headers = staff()

    response = client.post(STAFF_TICKETS, json=_good(category), headers=headers)

    assert response.status_code == 422
    assert response.json() == BAD_CUSTOMER_ID
    assert _ticket_count(session) == 0


_BOUNDARY = "multipart/form-data; boundary=abc"


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        pytest.param(_BOUNDARY, b"this is not multipart data", id="body-does-not-start-with-boundary"),
        pytest.param(_BOUNDARY, b"\r\n--other\r\nx", id="wrong-boundary"),
        pytest.param(_BOUNDARY, b"--abc\r\nbroken header line\r\n\r\n", id="bad-part-header"),
        pytest.param("multipart/form-data", b"--abc--", id="no-boundary-parameter"),
    ],
)
def test_a_multipart_body_without_a_valid_boundary_is_422_invalid_request_not_500(
    client: TestClient,
    session: Session,
    staff,
    numbers: Numbers,
    photos: FakePhotoStore,
    content_type: str,
    body: bytes,
) -> None:
    _user, headers = staff()

    response = client.post(STAFF_TICKETS, content=body, headers={**headers, "Content-Type": content_type})

    assert response.status_code == 422
    assert response.json() == {
        "detail": {"error": {"code": "invalid_request", "fields": {"body": "invalid_request"}}}
    }
    _nothing_happened(session, numbers, photos)


# ------------------------------------------------------------------ the customer checks (R26.5)


@pytest.mark.parametrize(
    "raw",
    ["", "not-a-uuid", "0" * 32, "{00000000-0000-4000-8000-000000000001}", " ", "12345"],
    ids=["empty", "text", "no hyphens", "braces", "blank", "number"],
)
def test_a_malformed_customer_id_is_422_customer_not_found_with_the_field(
    client: TestClient,
    session: Session,
    staff,
    category: Category,
    numbers: Numbers,
    photos: FakePhotoStore,
    raw: str,
) -> None:
    _user, headers = staff()

    response = _open(client, headers, raw, category, _photo(jpeg_bytes()))

    assert response.status_code == 422
    assert response.json() == BAD_CUSTOMER_ID
    _nothing_happened(session, numbers, photos)


def test_a_missing_customer_id_is_422_customer_not_found_with_the_field(
    client: TestClient, session: Session, staff, category: Category, numbers: Numbers, photos: FakePhotoStore
) -> None:
    _user, headers = staff()

    response = _open(client, headers, None, category)

    assert response.status_code == 422
    assert response.json() == BAD_CUSTOMER_ID
    _nothing_happened(session, numbers, photos)


def test_a_repeated_customer_id_is_422_customer_not_found(
    client: TestClient,
    session: Session,
    staff,
    customer: Customer,
    category: Category,
    numbers: Numbers,
    photos: FakePhotoStore,
) -> None:
    _user, headers = staff()

    response = _open(client, headers, customer, category, ("customer_id", (None, str(customer.id))))

    assert response.status_code == 422
    assert response.json() == BAD_CUSTOMER_ID
    _nothing_happened(session, numbers, photos)


def test_a_customer_id_sent_as_a_file_part_is_422_customer_not_found(
    client: TestClient,
    session: Session,
    staff,
    customer: Customer,
    category: Category,
    numbers: Numbers,
    photos: FakePhotoStore,
) -> None:
    _user, headers = staff()

    response = client.post(
        STAFF_TICKETS,
        files=[
            ("customer_id", ("id.txt", str(customer.id).encode(), "text/plain")),
            *_parts(_good(category)),
        ],
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json() == BAD_CUSTOMER_ID
    _nothing_happened(session, numbers, photos)


def test_an_unknown_customer_is_404_customer_not_found(
    client: TestClient, session: Session, staff, category: Category, numbers: Numbers, photos: FakePhotoStore
) -> None:
    _user, headers = staff()

    response = _open(client, headers, str(uuid.uuid4()), category, _photo(jpeg_bytes()))

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    _nothing_happened(session, numbers, photos)


def test_an_inactive_customer_is_409_customer_inactive(
    client: TestClient,
    session: Session,
    staff,
    make_customer,
    category: Category,
    numbers: Numbers,
    photos: FakePhotoStore,
) -> None:
    _user, headers = staff()
    inactive = make_customer(is_active=False)
    before = _customer_row(session, inactive)

    response = _open(client, headers, inactive, category, _photo(jpeg_bytes()))

    assert response.status_code == 409
    assert response.json() == INACTIVE
    _nothing_happened(session, numbers, photos)
    assert _customer_row(session, inactive) == before


def test_a_customer_who_owes_a_password_change_may_still_be_opened_for(
    client: TestClient, staff, make_customer, category: Category
) -> None:
    _user, headers = staff()
    fresh = make_customer(must_change_password=True)

    response = _open(client, headers, fresh, category)

    assert response.status_code == 201


def test_the_customer_checks_come_before_the_form_validation(
    client: TestClient,
    session: Session,
    staff,
    make_customer,
    category: Category,
    numbers: Numbers,
    photos: FakePhotoStore,
) -> None:
    """Design check order: customer_id (422), unknown (404), inactive (409), then the form fields."""
    _user, headers = staff()
    inactive = make_customer(is_active=False)
    broken = {"city": "", "phone": "x", "surprise": "1"}
    junk_photo = _photo(b"not an image")

    malformed = _open(client, headers, "nope", category, junk_photo, **broken)
    unknown = _open(client, headers, str(uuid.uuid4()), category, junk_photo, **broken)
    refused = _open(client, headers, inactive, category, junk_photo, **broken)

    assert (malformed.status_code, malformed.json()) == (422, BAD_CUSTOMER_ID)
    assert (unknown.status_code, unknown.json()) == (404, NOT_FOUND)
    assert (refused.status_code, refused.json()) == (409, INACTIVE)
    _nothing_happened(session, numbers, photos)


# ------------------------------------------------------------ storage failures (ticket and photo are atomic)


def test_a_failing_photo_store_is_503_and_no_ticket(
    client: TestClient,
    session: Session,
    staff,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
) -> None:
    _user, headers = staff()
    photos.fail_put = True

    response = _open(client, headers, customer, category, _photo(jpeg_bytes()))

    assert response.status_code == 503
    assert _code(response) == "photo_storage_unavailable"
    assert _ticket_count(session) == 0
    assert photos.objects == {}


def test_a_failing_insert_deletes_the_stored_photo(
    client: TestClient,
    session: Session,
    staff,
    customer: Customer,
    category: Category,
    photos: FakePhotoStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _user, headers = staff()

    def broken_insert(*_args: Any, **_kwargs: Any) -> Ticket:
        raise DBAPIError("INSERT", {}, Exception("constraint failed"))

    monkeypatch.setattr(customer_router, "create_ticket", broken_insert)

    response = _open(client, headers, customer, category, _photo(jpeg_bytes()))

    assert response.status_code == 503
    assert _code(response) == "service_unavailable"
    assert [op for op, _ in photos.ops] == ["put", "delete"]
    assert photos.objects == {}
    assert _ticket_count(session) == 0


def test_exhausted_ticket_numbers_are_503_and_store_nothing(
    client: TestClient,
    session: Session,
    staff,
    customer: Customer,
    category: Category,
    numbers: Numbers,
    photos: FakePhotoStore,
) -> None:
    _user, headers = staff()
    numbers.exhausted = True

    response = _open(client, headers, customer, category, _photo(jpeg_bytes()))

    assert response.status_code == 503
    assert _code(response) == "ticket_numbers_exhausted"
    assert _ticket_count(session) == 0
    assert photos.objects == {}


# --------------------------------------------------------------------------- access (R1.9, R26.8)


def test_a_customer_token_is_refused_on_both_routes(
    client: TestClient,
    session: Session,
    customer: Customer,
    category: Category,
    numbers: Numbers,
    photos: FakePhotoStore,
) -> None:
    headers = _customer_headers(customer)

    opened = _open(client, headers, customer, category)
    form = client.get(STAFF_FORM_CONFIG, headers=headers)

    for response in (opened, form):
        assert response.status_code == 403
        assert response.json() == {"detail": {"error": {"code": "customer_token_not_accepted"}}}
    _nothing_happened(session, numbers, photos)


def test_no_token_is_401_on_both_routes(client: TestClient, customer: Customer, category: Category) -> None:
    opened = _open(client, {}, customer, category)
    form = client.get(STAFF_FORM_CONFIG)

    assert (opened.status_code, form.status_code) == (401, 401)
    assert _code(opened) == _code(form) == "not_authenticated"


@pytest.mark.parametrize("role", [UserRole.ACCOUNTING, UserRole.SITE_MANAGER, UserRole.EMPLOYEE])
def test_a_role_outside_ticket_staff_is_403_on_both_routes(
    client: TestClient,
    session: Session,
    staff,
    customer: Customer,
    category: Category,
    numbers: Numbers,
    photos: FakePhotoStore,
    role: UserRole,
) -> None:
    _user, headers = staff(role)

    opened = _open(client, headers, customer, category)
    form = client.get(STAFF_FORM_CONFIG, headers=headers)

    for response in (opened, form):
        assert response.status_code == 403
        assert _code(response) == "insufficient_role"
    _nothing_happened(session, numbers, photos)


def test_a_staff_member_who_owes_a_password_change_is_refused(
    client: TestClient,
    session: Session,
    make_user,
    customer: Customer,
    category: Category,
    numbers: Numbers,
    photos: FakePhotoStore,
) -> None:
    user = make_user(role=UserRole.TECHNICIAN, is_2fa_enabled=True, must_change_password=True)
    token = create_access_token(user_id=user.id, token_version=user.token_version, role="technician")

    response = _open(client, {"Authorization": f"Bearer {token}"}, customer, category)

    assert response.status_code == 403
    assert _code(response) == "password_change_required"
    _nothing_happened(session, numbers, photos)


def test_flag_off_both_routes_are_404(
    settings,
    session: Session,
    staff,
    numbers: Numbers,
    photos: FakePhotoStore,
    customer: Customer,
    category: Category,
) -> None:
    off = TestClient(_build(settings, session, numbers, photos, enabled=False), raise_server_exceptions=False)
    _user, headers = staff()

    opened = _open(off, headers, customer, category)
    form = off.get(STAFF_FORM_CONFIG, headers=headers)

    for response in (opened, form):
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
    _nothing_happened(session, numbers, photos)


def test_a_staff_submission_over_twelve_mebibytes_is_the_coded_413(client: TestClient, staff) -> None:
    _user, headers = staff()

    response = client.post(
        STAFF_TICKETS,
        content=b"x" * (12 * 1024 * 1024 + 1),
        headers={**headers, "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": {"error": {"code": "request_too_large"}}}


# ------------------------------------------------------------------ the customer submission limit (R26.8)


def test_staff_submissions_neither_take_nor_release_a_customer_rate_limit_slot(
    client: TestClient,
    staff,
    customer: Customer,
    category: Category,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def take(key: str, *_args: Any, **_kwargs: Any) -> rate_limit.TakeResult:
        calls.append(("take", key))
        return rate_limit.TakeResult(allowed=True)

    def release(key: str, *_args: Any, **_kwargs: Any) -> None:
        calls.append(("release", key))

    monkeypatch.setattr(rate_limit, "take", take)
    monkeypatch.setattr(rate_limit, "release", release)
    _user, headers = staff()

    ok = _open(client, headers, customer, category)
    refused = _open(client, headers, customer, category, city="")
    unknown = _open(client, headers, str(uuid.uuid4()), category)

    assert (ok.status_code, refused.status_code, unknown.status_code) == (201, 422, 404)
    assert calls == []  # no window of any customer was read or counted

    # Control: the same recorder does see a customer submission take its slot.
    assert _submit_as_customer(client, customer, category).status_code == 201
    assert any(op == "take" and key.startswith(rate_limit.SUBMIT_PREFIX) for op, key in calls)


# --------------------------------------------------------------------------- what the customer sees (R26.6)


def test_the_ticket_is_in_the_owners_list_and_detail_and_carries_no_staff_field(
    client: TestClient, staff, customer: Customer, category: Category
) -> None:
    user, headers = staff()
    number = _open(client, headers, customer, category, city="Elsewhere").json()["ticket_number"]
    owner = _customer_headers(customer)

    listing = client.get(f"{API}/tickets", headers=owner)
    detail = client.get(f"{API}/tickets/{number}", headers=owner)

    assert listing.status_code == detail.status_code == 200
    assert [row["ticket_number"] for row in listing.json()["items"]] == [number]
    assert set(listing.json()["items"][0]) == {
        "ticket_number",
        "category",
        "created_date",
        "apartment",
        "status",
        "urgent",
    }
    assert detail.json()["city"] == "Elsewhere"
    for response in (listing, detail):
        assert "opened_by" not in response.text
        assert user.username not in response.text


def test_the_customer_can_cancel_a_ticket_staff_opened(
    client: TestClient, session: Session, staff, customer: Customer, category: Category
) -> None:
    user, headers = staff()
    number = _open(client, headers, customer, category).json()["ticket_number"]

    response = client.post(
        f"{API}/tickets/{number}/cancel",
        json={"expected_status": "received"},
        headers=_customer_headers(customer),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert "opened_by" not in response.text
    assert user.username not in response.text
    assert _stored(session, number).status == "cancelled"
    assert _stored(session, number).opened_by_username == user.username


def test_staff_see_the_opening_username_only_on_tickets_staff_opened(
    client: TestClient, staff, customer: Customer, category: Category
) -> None:
    user, headers = staff()
    by_staff = _open(client, headers, customer, category).json()["ticket_number"]
    by_customer = _submit_as_customer(client, customer, category).json()["ticket_number"]

    staff_ticket = client.get(f"{STAFF_TICKETS}/{by_staff}", headers=headers).json()
    customer_ticket = client.get(f"{STAFF_TICKETS}/{by_customer}", headers=headers).json()
    listing = client.get(STAFF_TICKETS, headers=headers).json()

    assert staff_ticket["opened_by_username"] == user.username
    assert customer_ticket["opened_by_username"] is None
    assert all("opened_by_username" not in row for row in listing["items"])  # the list row is unchanged


# --------------------------------------------------------------------------- GET /staff/form-config (R26.2)


def test_the_staff_form_config_is_the_customer_form_config(
    client: TestClient, session: Session, staff, customer: Customer, category: Category
) -> None:
    session.add(
        Category(
            name_he="Old-he", name_en="Old", name_he_key="old-he", name_en_key="old", is_active=False
        )
    )
    session.commit()
    _user, headers = staff()

    as_staff = client.get(STAFF_FORM_CONFIG, headers=headers)
    as_customer = client.get(f"{API}/form-config", headers=_customer_headers(customer))

    assert as_staff.status_code == as_customer.status_code == 200
    assert as_staff.json() == as_customer.json()
    assert [c["name_en"] for c in as_staff.json()["categories"]] == ["Plumbing"]  # active ones only
    assert set(as_staff.json()) == {"categories", "labels", "settings"}
    assert set(as_staff.json()["settings"]) == {"description_required", "photo_upload_enabled"}


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.TECHNICIAN])
def test_every_ticket_staff_role_may_read_the_staff_form_config(
    client: TestClient, staff, role: UserRole
) -> None:
    _user, headers = staff(role)

    assert client.get(STAFF_FORM_CONFIG, headers=headers).status_code == 200
