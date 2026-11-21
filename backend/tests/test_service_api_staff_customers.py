"""Staff customer and report endpoints (task 13.5; R3, R18; design "Staff endpoints").

A small application mounts only `staff_router.router` under `/api/service`, plus one probe route behind
`customer_principal_ungated` so a test can see whether a customer token still works (the customer router
is not part of this task). `get_db` is overridden to give each request its own session on the in-memory
test database. Tokens are real staff access tokens and real customer tokens.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models.user import User, UserRole
from app.service_tickets.api import staff_router
from app.service_tickets.api.deps import CustomerPrincipal, customer_principal_ungated
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.models import Category, Customer, SigninFailure, Ticket

LONG_AGO = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
PREFIX = "/api/service/staff"

_numbers = itertools.count(30000)
_seq = itertools.count(1)
_phones = itertools.count(1)


# --------------------------------------------------------------------------------- app and fixtures


def _build_app(session: Session) -> FastAPI:
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True)

    def request_session() -> Iterator[Session]:
        with factory() as per_request:
            yield per_request

    probe = APIRouter()

    @probe.get("/probe")
    def whoami(principal: CustomerPrincipal = Depends(customer_principal_ungated)) -> dict[str, str]:  # noqa: B008
        return {"customer_id": str(principal.customer_id)}

    app = FastAPI()
    app.include_router(staff_router.router, prefix="/api/service")
    app.include_router(probe, prefix="/api/service")
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
def make_customer(session: Session):
    """A committed customer. The password is a real hash of `1111`, not the initial password."""
    shared_hash = hash_password("1111")

    def _make(
        name: str = "Dana Levi",
        *,
        phone: str | None = None,
        city: str | None = "Haifa",
        active: bool = True,
        must_change: bool = False,
    ) -> Customer:
        row = Customer(
            phone=phone or f"05{next(_phones):08d}",
            full_name=name,
            full_name_key=name.strip().casefold(),
            city=city,
            apartment="12",
            password_hash=shared_hash,
            must_change_password=must_change,
            is_active=active,
        )
        session.add(row)
        session.commit()
        return row

    return _make


@pytest.fixture
def customer(make_customer) -> Customer:
    return make_customer()


@pytest.fixture
def make_category(session: Session):
    def _make(name: str = "Electric") -> Category:
        row = Category(
            seq=next(_seq),
            name_he=f"{name}-he",
            name_en=name,
            name_he_key=f"{name}-he".casefold(),
            name_en_key=name.casefold(),
            is_active=True,
        )
        session.add(row)
        session.commit()
        return row

    return _make


@pytest.fixture
def category(make_category) -> Category:
    return make_category()


@pytest.fixture
def make_ticket(session: Session, customer: Customer, category: Category):
    def _make(
        status: str = "received",
        *,
        city: str = "Haifa",
        created_at: datetime = LONG_AGO,
        created_date: date | None = None,
        in_category: Category | None = None,
        owner: Customer | None = None,
    ) -> Ticket:
        ticket = Ticket(
            ticket_number=next(_numbers),
            customer_id=(owner or customer).id,
            category_id=(in_category or category).id,
            city=city,
            city_key=city.strip().casefold(),
            apartment="12",
            phone="0501234567",
            description="leaking tap",
            urgent=False,
            status=status,
            assigned_technician_name="",
            created_at=created_at,
            created_date=created_date or created_at.date(),
            updated_at=LONG_AGO,
        )
        session.add(ticket)
        session.commit()
        return ticket

    return _make


def _code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


def _row(session: Session, customer_id: uuid.UUID | str) -> Customer:
    """The customer as the database holds it now, not as the identity map remembers it."""
    session.rollback()
    session.expire_all()
    row = session.get(Customer, uuid.UUID(str(customer_id)))
    assert row is not None
    return row


def _bearer(customer: Customer, version: int | None = None) -> dict[str, str]:
    token = mint_customer_token(customer.id, customer.token_version if version is None else version)
    return {"Authorization": f"Bearer {token}"}


def _create(client: TestClient, headers: dict[str, str], **body: Any) -> Any:
    payload = {"phone": "0505556677", "full_name": "Rina Cohen", "city": "Haifa", "apartment": "7"}
    payload.update(body)
    return client.post(f"{PREFIX}/customers", json=payload, headers=headers)


def _count(session: Session) -> int:
    session.rollback()
    return int(session.execute(select(func.count()).select_from(Customer)).scalar_one())


# --------------------------------------------------------------------------------- access


def test_every_route_needs_a_ticket_staff_token(
    client: TestClient, session: Session, customer: Customer, staff
) -> None:
    cid = customer.id
    routes = [
        ("GET", "/customers"),
        ("POST", "/customers"),
        ("PUT", f"/customers/{cid}"),
        ("POST", f"/customers/{cid}/deactivate"),
        ("POST", f"/customers/{cid}/reactivate"),
        ("POST", f"/customers/{cid}/reset-password"),
        ("GET", "/reports"),
    ]
    for method, path in routes:
        anonymous = client.request(method, PREFIX + path, json={})
        assert (anonymous.status_code, _code(anonymous)) == (401, "not_authenticated"), path
        as_customer = client.request(method, PREFIX + path, json={}, headers=_bearer(customer))
        assert (as_customer.status_code, _code(as_customer)) == (403, "customer_token_not_accepted"), path
        as_accounting = client.request(method, PREFIX + path, json={}, headers=staff(UserRole.ACCOUNTING)[1])
        assert (as_accounting.status_code, _code(as_accounting)) == (403, "insufficient_role"), path
    assert _row(session, cid).is_active and _row(session, cid).token_version == 1


@pytest.mark.parametrize("role", [UserRole.TECHNICIAN, UserRole.ADMIN, UserRole.OPERATIONS_ADMIN])
def test_all_ticket_staff_may_manage_customers_and_read_reports(
    client: TestClient, staff, role: UserRole
) -> None:
    _, auth = staff(role)

    assert client.get(f"{PREFIX}/customers", headers=auth).status_code == 200
    assert client.get(f"{PREFIX}/reports", headers=auth).status_code == 200
    created = _create(client, auth, phone=f"05{next(_phones) + 500:08d}")
    assert created.status_code == 201


# --------------------------------------------------------------------------------- create


def test_create_customer_stores_initial_password_and_leaks_nothing(
    client: TestClient, session: Session, headers
) -> None:
    response = _create(client, headers, phone="050-555-6677", city="  ", apartment=" 7 ")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "full_name", "phone", "city", "apartment", "is_active"}
    assert body["phone"] == "0505556677"
    assert body["city"] is None and body["apartment"] == "7" and body["is_active"] is True
    assert "1234" not in response.text and "password" not in response.text and "$2" not in response.text
    row = _row(session, body["id"])
    assert verify_password("1234", row.password_hash)
    assert row.must_change_password is True and row.is_active is True and row.token_version == 1


def test_create_validation_reports_every_failing_field(client: TestClient, session: Session, headers) -> None:
    response = _create(
        client, headers, phone="123", full_name="   ", city="c" * 101, apartment="a" * 21
    )

    assert response.status_code == 422
    error = response.json()["detail"]["error"]
    assert error["code"] == "invalid_phone"
    assert error["fields"] == {
        "phone": "invalid_phone",
        "full_name": "invalid_full_name",
        "city": "invalid_city",
        "apartment": "invalid_apartment",
    }
    assert _count(session) == 0


def test_create_rejects_unknown_fields_and_wrong_types(client: TestClient, session: Session, headers) -> None:
    extra = _create(client, headers, is_active=False)
    assert (extra.status_code, _code(extra)) == (422, "unknown_field")

    wrong_type = _create(client, headers, phone=505556677)
    assert (wrong_type.status_code, _code(wrong_type)) == (422, "invalid_phone")
    assert _count(session) == 0


def test_duplicate_phone_is_409_in_any_spelling_and_for_inactive_holders(
    client: TestClient, session: Session, headers, make_customer
) -> None:
    make_customer("Inactive Holder", phone="0505556677", active=False)

    for spelling in ("0505556677", "+972 50-555-6677", "(050) 555 6677"):
        response = _create(client, headers, phone=spelling, full_name="Someone Else")
        assert (response.status_code, _code(response)) == (409, "phone_already_registered"), spelling

    assert _count(session) == 1


# --------------------------------------------------------------------------------- edit


def test_edit_replaces_the_four_values_and_keeps_tokens_valid(
    client: TestClient, session: Session, headers, customer: Customer
) -> None:
    old_hash, old_version = customer.password_hash, customer.token_version

    response = client.put(
        f"{PREFIX}/customers/{customer.id}",
        json={"phone": "052 777-8899", "full_name": "Dana Mizrahi", "city": "Akko", "apartment": None},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(customer.id),
        "full_name": "Dana Mizrahi",
        "phone": "0527778899",
        "city": "Akko",
        "apartment": None,
        "is_active": True,
    }
    row = _row(session, customer.id)
    assert row.password_hash == old_hash and row.token_version == old_version
    assert client.get("/api/service/probe", headers=_bearer(customer, old_version)).status_code == 200


def test_edit_to_another_customers_phone_is_409_and_changes_nothing(
    client: TestClient, session: Session, headers, customer: Customer, make_customer
) -> None:
    other = make_customer("Other Person")
    before = customer.phone

    response = client.put(
        f"{PREFIX}/customers/{customer.id}",
        json={"phone": other.phone, "full_name": "Renamed"},
        headers=headers,
    )

    assert (response.status_code, _code(response)) == (409, "phone_already_registered")
    row = _row(session, customer.id)
    assert row.phone == before and row.full_name == "Dana Levi"


def test_edit_keeping_its_own_phone_is_allowed(client: TestClient, headers, customer: Customer) -> None:
    response = client.put(
        f"{PREFIX}/customers/{customer.id}",
        json={"phone": customer.phone, "full_name": "Dana Levi-Cohen"},
        headers=headers,
    )
    assert response.status_code == 200 and response.json()["full_name"] == "Dana Levi-Cohen"


def test_edit_validates_before_it_looks_the_customer_up(client: TestClient, headers) -> None:
    response = client.put(
        f"{PREFIX}/customers/{uuid.uuid4()}", json={"phone": "bad", "full_name": "X"}, headers=headers
    )
    assert (response.status_code, _code(response)) == (422, "invalid_phone")


# --------------------------------------------------------------------------------- not found


@pytest.mark.parametrize("raw_id", [str(uuid.uuid4()), "not-a-uuid", "12345", "{" + str(uuid.uuid4()) + "}"])
@pytest.mark.parametrize("action", ["put", "deactivate", "reactivate", "reset-password"])
def test_unknown_or_malformed_customer_id_is_404(
    client: TestClient, headers, action: str, raw_id: str
) -> None:
    url = f"{PREFIX}/customers/{raw_id}"
    if action == "put":
        response = client.put(url, json={"phone": "0505556677", "full_name": "X"}, headers=headers)
    else:
        response = client.post(f"{url}/{action}", headers=headers)

    assert (response.status_code, _code(response)) == (404, "customer_not_found")


# --------------------------------------------------------------------------------- state changes


def test_deactivate_revokes_tokens_and_leaves_tickets_alone(
    client: TestClient, session: Session, headers, customer: Customer, make_ticket
) -> None:
    ticket = make_ticket("in_progress")
    old = _bearer(customer)
    assert client.get("/api/service/probe", headers=old).status_code == 200

    response = client.post(f"{PREFIX}/customers/{customer.id}/deactivate", headers=headers)

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert set(response.json()) == {"id", "full_name", "phone", "city", "apartment", "is_active"}
    stale = client.get("/api/service/probe", headers=old)
    assert (stale.status_code, _code(stale)) == (401, "invalid_token")
    session.rollback()
    assert session.execute(select(Ticket.status).where(Ticket.id == ticket.id)).scalar_one() == "in_progress"


def test_reactivate_keeps_password_and_does_not_revive_old_tokens(
    client: TestClient, session: Session, headers, make_customer
) -> None:
    customer = make_customer("Returning Person")
    old = _bearer(customer)
    password_hash = customer.password_hash
    client.post(f"{PREFIX}/customers/{customer.id}/deactivate", headers=headers)

    response = client.post(f"{PREFIX}/customers/{customer.id}/reactivate", headers=headers)

    assert response.status_code == 200 and response.json()["is_active"] is True
    row = _row(session, customer.id)
    assert row.password_hash == password_hash and row.must_change_password is False
    assert client.get("/api/service/probe", headers=old).status_code == 401
    assert client.get("/api/service/probe", headers=_bearer(row)).status_code == 200


def test_reset_password_sets_initial_password_and_clears_the_lock(
    client: TestClient, session: Session, headers, make_customer
) -> None:
    customer = make_customer("Locked Out", active=False)
    other = make_customer("Bystander")
    old = _bearer(customer)
    session.add_all(
        [SigninFailure(phone=customer.phone, failed_at=LONG_AGO) for _ in range(5)]
        + [SigninFailure(phone=other.phone, failed_at=LONG_AGO)]
    )
    session.commit()

    response = client.post(f"{PREFIX}/customers/{customer.id}/reset-password", headers=headers)

    assert response.status_code == 204 and response.content == b""
    row = _row(session, customer.id)
    assert verify_password("1234", row.password_hash)
    assert row.must_change_password is True and row.is_active is False and row.token_version == 2
    remaining = session.execute(select(SigninFailure.phone)).scalars().all()
    assert remaining == [other.phone]
    # An inactive customer is refused anyway; after reactivation the pre-reset token must still be dead.
    client.post(f"{PREFIX}/customers/{customer.id}/reactivate", headers=headers)
    assert client.get("/api/service/probe", headers=old).status_code == 401


# --------------------------------------------------------------------------------- list and search


@pytest.fixture
def directory(make_customer) -> dict[str, Customer]:
    return {
        "dana": make_customer("Dana Levi", phone="0501112233"),
        "avi": make_customer("Avi Katz", phone="0522223344"),
        "beni": make_customer("Beni Katz", phone="0533334455", active=False),
        "mira": make_customer("MIRA Ben-David", phone="0544445566", active=False),
    }


def _names(response: Any) -> list[str]:
    return [item["full_name"] for item in response.json()["items"]]


def test_list_is_sorted_by_name_and_shows_the_listed_fields(client: TestClient, headers, directory) -> None:
    response = client.get(f"{PREFIX}/customers", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert (body["page"], body["page_size"], body["total"]) == (1, 50, 4)
    assert _names(response) == ["Avi Katz", "Beni Katz", "Dana Levi", "MIRA Ben-David"]
    assert set(body["items"][0]) == {"id", "full_name", "phone", "city", "apartment", "is_active"}
    assert "password" not in response.text and "token_version" not in response.text


def test_search_by_name_and_by_phone_combined_with_the_state_filter(
    client: TestClient, headers, directory
) -> None:
    def names(**params: str) -> list[str]:
        return _names(client.get(f"{PREFIX}/customers", params=params, headers=headers))

    assert names(search="KATZ") == ["Avi Katz", "Beni Katz"]
    assert names(search="mira ben") == ["MIRA Ben-David"]
    assert names(search="(050) 111-22") == ["Dana Levi"]
    assert names(search="katz", active="true") == ["Avi Katz"]
    assert names(search="katz", active="false") == ["Beni Katz"]
    assert names(active="false") == ["Beni Katz", "MIRA Ben-David"]
    assert names(active="all") == names() and len(names()) == 4
    assert names(search="no such person") == []
    assert names(search="%") == []  # a wildcard matches only itself


def test_search_text_is_never_validated_as_a_phone(client: TestClient, headers, directory) -> None:
    response = client.get(f"{PREFIX}/customers", params={"search": "abc-12"}, headers=headers)
    assert response.status_code == 200 and response.json()["items"] == []


def test_list_paging(client: TestClient, session: Session, headers, make_customer) -> None:
    for index in range(51):
        make_customer(f"Person {index:02d}")

    first = client.get(f"{PREFIX}/customers", headers=headers).json()
    second = client.get(f"{PREFIX}/customers", params={"page": "2"}, headers=headers).json()
    beyond = client.get(f"{PREFIX}/customers", params={"page": "9"}, headers=headers).json()

    assert (len(first["items"]), first["total"]) == (50, 51)
    assert [item["full_name"] for item in second["items"]] == ["Person 50"]
    assert beyond["items"] == [] and beyond["total"] == 51


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"page": "0"}, "invalid_page"),
        ({"page": "-1"}, "invalid_page"),
        ({"page": "x"}, "invalid_page"),
        ({"active": "maybe"}, "invalid_filter"),
        ({"active": "1"}, "invalid_filter"),
    ],
)
def test_list_query_errors_are_422(client: TestClient, headers, params: dict[str, str], code: str) -> None:
    response = client.get(f"{PREFIX}/customers", params=params, headers=headers)
    assert (response.status_code, _code(response)) == (422, code)


# --------------------------------------------------------------------------------- reports


@pytest.fixture
def tickets(make_ticket, make_category, make_customer) -> dict[str, Any]:
    plumbing = make_category("Plumbing")
    other_customer = make_customer("Second Customer")
    return {
        "plumbing": plumbing,
        "rows": [
            make_ticket("received", city="Haifa", created_date=date(2025, 3, 1)),
            make_ticket("in_progress", city="  HAIFA ", created_date=date(2025, 3, 2)),
            make_ticket("closed", city="Akko", created_date=date(2025, 3, 3), in_category=plumbing),
            make_ticket(
                "cancelled",
                city="Akko",
                created_date=date(2025, 3, 3),
                in_category=plumbing,
                owner=other_customer,
            ),
            make_ticket("received", city="Tiberias", created_date=date(2025, 4, 1), in_category=plumbing),
        ],
    }


def _report(client: TestClient, headers, **params: str) -> Any:
    return client.get(f"{PREFIX}/reports", params=params, headers=headers)


def test_report_without_filters_covers_every_ticket(client: TestClient, headers, tickets, category) -> None:
    response = _report(client, headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5 and body["open"] == 3
    assert body["by_status"] == {"received": 2, "in_progress": 1, "closed": 1, "cancelled": 1}
    assert sum(item["count"] for item in body["by_category"]) == body["total"]
    assert [(item["category"]["name_en"], item["count"]) for item in body["by_category"]] == [
        ("Electric", 2),
        ("Plumbing", 3),
    ]
    page = body["tickets"]
    assert (page["page"], page["page_size"], page["total"], len(page["items"])) == (1, 25, 5, 5)
    assert set(page["items"][0]) == {
        "ticket_number", "created_at", "customer_name", "city", "apartment",
        "category", "status", "assigned_technician_name", "urgent",
    }  # fmt: skip
    assert "phone" not in page["items"][0] and "description" not in page["items"][0]


def test_report_filters_combine_with_and(client: TestClient, headers, tickets) -> None:
    by_date = _report(client, headers, date_from="2025-03-02", date_to="2025-03-03").json()
    assert by_date["total"] == 3
    assert by_date["by_status"] == {"received": 0, "in_progress": 1, "closed": 1, "cancelled": 1}

    one_day = _report(client, headers, date_from="2025-03-03", date_to="2025-03-03").json()
    assert one_day["total"] == 2

    open_ended = _report(client, headers, date_from="2025-04-01").json()
    assert open_ended["total"] == 1

    by_city = _report(client, headers, city=" haifa ").json()
    assert by_city["total"] == 2

    by_category = _report(client, headers, category_id=str(tickets["plumbing"].id), city="akko").json()
    assert by_category["total"] == 2
    assert [item["count"] for item in by_category["by_category"]] == [2]

    empty = _report(client, headers, city="Nowhere").json()
    assert empty["total"] == 0 and empty["open"] == 0 and empty["tickets"]["items"] == []
    assert empty["by_status"] == {"received": 0, "in_progress": 0, "closed": 0, "cancelled": 0}


def test_report_blank_filters_restrict_nothing(client: TestClient, headers, tickets) -> None:
    body = _report(client, headers, date_from="", date_to="", city="  ", category_id="").json()
    assert body["total"] == 5


def test_report_ticket_table_is_newest_first_and_pages_by_25(
    client: TestClient, headers, make_ticket
) -> None:
    for minute in range(26):
        make_ticket(created_at=datetime(2025, 5, 1, 10, minute, tzinfo=UTC))

    first = _report(client, headers).json()["tickets"]
    second = _report(client, headers, page="2").json()["tickets"]
    beyond = _report(client, headers, page="3").json()["tickets"]

    assert (len(first["items"]), len(second["items"]), beyond["items"]) == (25, 1, [])
    stamps = [item["created_at"] for item in first["items"]]
    assert stamps == sorted(stamps, reverse=True)
    assert first["total"] == second["total"] == beyond["total"] == 26


@pytest.mark.parametrize(
    "params",
    [
        {"date_from": "2025-03-05", "date_to": "2025-03-04"},
        {"date_from": "2025-02-30"},
        {"date_to": "03/04/2025"},
        {"date_from": " 2025-03-01"},
    ],
)
def test_report_bad_dates_are_invalid_date_range(client: TestClient, headers, params: dict[str, str]) -> None:
    response = _report(client, headers, **params)
    assert (response.status_code, _code(response)) == (422, "invalid_date_range")
    assert "total" not in response.text


@pytest.mark.parametrize("category_id", [str(uuid.uuid4()), "not-a-uuid"])
def test_report_unknown_or_malformed_category_is_invalid_filter(
    client: TestClient, headers, category_id: str
) -> None:
    response = _report(client, headers, category_id=category_id)
    assert (response.status_code, _code(response)) == (422, "invalid_filter")


def test_report_bad_page_is_invalid_page(client: TestClient, headers) -> None:
    response = _report(client, headers, page="0")
    assert (response.status_code, _code(response)) == (422, "invalid_page")
