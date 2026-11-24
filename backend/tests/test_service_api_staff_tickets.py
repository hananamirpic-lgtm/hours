"""Staff dashboard, ticket and history endpoints (task 13.4; R14, R15, R16, R17; design "Staff endpoints").

A small application mounts only `staff_router.router` under `/api/service`, with `get_db` overridden to give
each request its own session on the in-memory test database (the staff identity read expunges the user row,
so it must not run on the session that created the row). Tokens are real staff access tokens.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import create_access_token
from app.db.session import get_db
from app.models.user import User, UserRole
from app.service_tickets.api import staff_router
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.error_codes import ERROR_CODES
from app.service_tickets.models import Category, Customer, HistoryEntry, Ticket

LONG_AGO = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
PREFIX = "/api/service/staff"

_numbers = itertools.count(20000)
_seq = itertools.count(1)
_phones = itertools.count(1)


# --------------------------------------------------------------------------------- app and fixtures


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
def technician(staff):
    return staff(UserRole.TECHNICIAN)


@pytest.fixture
def admin(staff):
    return staff(UserRole.ADMIN)


@pytest.fixture
def customer(session: Session) -> Customer:
    row = Customer(
        phone=f"05{next(_phones):08d}", full_name="Dana Levi", full_name_key="dana levi", password_hash="x"
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def make_category(session: Session):
    def _make(name: str = "Electric", *, active: bool = True) -> Category:
        row = Category(
            seq=next(_seq),
            name_he=f"{name}-he",
            name_en=name,
            name_he_key=f"{name}-he".casefold(),
            name_en_key=name.casefold(),
            is_active=active,
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
        name: str = "",
        urgent: bool = False,
        city: str = "Haifa",
        created_at: datetime = LONG_AGO,
        created_date: date | None = None,
        in_category: Category | None = None,
    ) -> Ticket:
        ticket = Ticket(
            ticket_number=next(_numbers),
            customer_id=customer.id,
            category_id=(in_category or category).id,
            city=city,
            city_key=city.strip().casefold(),
            apartment="12",
            phone="0501234567",
            description="leaking tap",
            urgent=urgent,
            status=status,
            assigned_technician_name=name,
            created_at=created_at,
            created_date=created_date or created_at.date(),
            updated_at=LONG_AGO,
        )
        session.add(ticket)
        session.commit()
        return ticket

    return _make


@pytest.fixture
def sequence(session: Session):
    """Stand-in for `svc_ticket_number_seq`; `set_last(n)` means n was the last number handed out."""
    session.execute(text("CREATE TABLE svc_ticket_number_seq (last_value INTEGER, is_called BOOLEAN)"))
    session.execute(text("INSERT INTO svc_ticket_number_seq VALUES (10000, 0)"))
    session.commit()

    def set_last(number: int) -> None:
        session.execute(
            text("UPDATE svc_ticket_number_seq SET last_value = :n, is_called = 1"), {"n": number}
        )
        session.commit()

    return set_last


def _code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


def _stored(session: Session, number: int) -> tuple[str, str, datetime]:
    """(status, assigned name, updated_at) read from the database now, not from the identity map."""
    session.rollback()
    row = session.execute(
        select(Ticket.status, Ticket.assigned_technician_name, Ticket.updated_at).where(
            Ticket.ticket_number == number
        )
    ).one()
    updated = row.updated_at if row.updated_at.tzinfo else row.updated_at.replace(tzinfo=UTC)
    return row.status, row.assigned_technician_name, updated


def _entries(session: Session, number: int) -> list[tuple[str, str]]:
    session.rollback()
    rows = session.execute(
        select(HistoryEntry.entry_text, HistoryEntry.technician_name)
        .join(Ticket, Ticket.id == HistoryEntry.ticket_id)
        .where(Ticket.ticket_number == number)
        .order_by(HistoryEntry.created_at, HistoryEntry.id)
    ).all()
    return [(row.entry_text, row.technician_name) for row in rows]


def _status_body(expected: str | None, target: str | None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = dict(extra)
    if expected is not None:
        body["expected_status"] = expected
    if target is not None:
        body["target_status"] = target
    return body


# --------------------------------------------------------------------------------- access


def test_every_route_needs_a_staff_token(client: TestClient, make_ticket, customer: Customer) -> None:
    ticket = make_ticket()
    n = ticket.ticket_number
    routes = [
        ("GET", "/dashboard"),
        ("GET", "/tickets"),
        ("GET", f"/tickets/{n}"),
        ("POST", f"/tickets/{n}/status"),
        ("PUT", f"/tickets/{n}/assigned-technician"),
        ("POST", f"/tickets/{n}/history"),
        ("PUT", f"/tickets/{n}/history/{uuid.uuid4()}"),
    ]
    customer_headers = {"Authorization": f"Bearer {mint_customer_token(customer.id, customer.token_version)}"}

    for method, path in routes:
        anonymous = client.request(method, PREFIX + path, json={})
        assert (anonymous.status_code, _code(anonymous)) == (401, "not_authenticated"), path
        as_customer = client.request(method, PREFIX + path, json={}, headers=customer_headers)
        assert (as_customer.status_code, _code(as_customer)) == (403, "customer_token_not_accepted"), path


@pytest.mark.parametrize("role", [UserRole.SITE_MANAGER, UserRole.ACCOUNTING, UserRole.EMPLOYEE])
def test_other_hours_roles_are_refused(client: TestClient, staff, role: UserRole) -> None:
    _, headers = staff(role)

    response = client.get(f"{PREFIX}/tickets", headers=headers)

    assert (response.status_code, _code(response)) == (403, "insufficient_role")


def test_a_technician_may_use_every_route_of_this_router(client: TestClient, technician, make_ticket) -> None:
    _, headers = technician
    ticket = make_ticket()
    n = ticket.ticket_number

    assert client.get(f"{PREFIX}/dashboard", headers=headers).status_code == 200
    assert client.get(f"{PREFIX}/tickets", headers=headers).status_code == 200
    assert client.get(f"{PREFIX}/tickets/{n}", headers=headers).status_code == 200
    assert (
        client.put(
            f"{PREFIX}/tickets/{n}/assigned-technician",
            json={"assigned_technician_name": "A"},
            headers=headers,
        ).status_code
        == 200
    )
    added = client.post(
        f"{PREFIX}/tickets/{n}/history", json={"entry_text": "x", "technician_name": "A"}, headers=headers
    )
    assert added.status_code == 201
    edited = client.put(
        f"{PREFIX}/tickets/{n}/history/{added.json()['id']}",
        json={"entry_text": "y", "technician_name": "A"},
        headers=headers,
    )
    assert edited.status_code == 200
    changed = client.post(
        f"{PREFIX}/tickets/{n}/status", json=_status_body("received", "in_progress"), headers=headers
    )
    assert changed.status_code == 200


def test_responses_carry_no_store(client: TestClient, admin, make_ticket, sequence) -> None:
    _, headers = admin
    ticket = make_ticket()

    for path in ("/dashboard", f"/tickets/{ticket.ticket_number}", "/tickets/00000"):
        assert client.get(PREFIX + path, headers=headers).headers["cache-control"] == "no-store", path


# --------------------------------------------------------------------------------- dashboard


def test_dashboard_counts(client: TestClient, admin, make_ticket, sequence) -> None:
    _, headers = admin
    make_ticket("received")
    make_ticket("received", urgent=True)
    make_ticket("in_progress", urgent=True)
    make_ticket("closed", urgent=True)
    make_ticket("cancelled")

    response = client.get(f"{PREFIX}/dashboard", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"open": 3, "in_progress": 1, "urgent": 2, "ticket_number_warning": None}


def test_dashboard_warning_is_for_config_staff_only_and_only_below_1000(
    client: TestClient, staff, sequence
) -> None:
    _, admin_headers = staff(UserRole.ADMIN)
    _, operations_headers = staff(UserRole.OPERATIONS_ADMIN)
    _, technician_headers = staff(UserRole.TECHNICIAN)

    fresh = client.get(f"{PREFIX}/dashboard", headers=admin_headers).json()
    assert fresh["ticket_number_warning"] is None

    sequence(99_501)  # 498 left
    for headers in (admin_headers, operations_headers):
        assert client.get(f"{PREFIX}/dashboard", headers=headers).json()["ticket_number_warning"] == {
            "remaining": 498
        }
    as_technician = client.get(f"{PREFIX}/dashboard", headers=technician_headers).json()
    assert as_technician["ticket_number_warning"] is None


# --------------------------------------------------------------------------------- list


def test_list_row_shape_and_paging(client: TestClient, admin, make_ticket) -> None:
    _, headers = admin
    base = LONG_AGO
    made = [make_ticket(created_at=base + timedelta(minutes=i)) for i in range(30)]

    first = client.get(f"{PREFIX}/tickets", headers=headers).json()
    second = client.get(f"{PREFIX}/tickets?page=2", headers=headers).json()
    beyond = client.get(f"{PREFIX}/tickets?page=3", headers=headers).json()

    assert (first["page"], first["page_size"], first["total"], len(first["items"])) == (1, 25, 30, 25)
    assert (second["page"], len(second["items"]), second["total"]) == (2, 5, 30)
    assert (beyond["items"], beyond["total"]) == ([], 30)
    assert first["items"][0]["ticket_number"] == made[-1].ticket_number  # newest first
    assert set(first["items"][0]) == {
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
    assert first["items"][0]["customer_name"] == "Dana Levi"
    assert set(first["items"][0]["category"]) == {"id", "name_he", "name_en"}


def test_list_filters_combine_with_and(client: TestClient, admin, make_ticket, make_category) -> None:
    _, headers = admin
    other = make_category("Plumbing")
    wanted = make_ticket("in_progress", urgent=True, city="  HAIFA ", created_date=date(2025, 3, 10))
    make_ticket("received", urgent=True, city="Haifa", created_date=date(2025, 3, 10))  # wrong status
    make_ticket("in_progress", urgent=False, city="Haifa", created_date=date(2025, 3, 10))  # not urgent
    make_ticket("in_progress", urgent=True, city="Acre", created_date=date(2025, 3, 10))  # wrong city
    make_ticket("in_progress", urgent=True, city="Haifa", created_date=date(2025, 4, 1))  # wrong date
    make_ticket("in_progress", urgent=True, city="Haifa", created_date=date(2025, 3, 10), in_category=other)

    response = client.get(
        f"{PREFIX}/tickets",
        params={
            "status": ["in_progress", "received"],
            "city": "haifa",
            "urgent_only": "true",
            "date_from": "2025-03-01",
            "date_to": "2025-03-31",
        },
        headers=headers,
    ).json()
    narrowed = client.get(
        f"{PREFIX}/tickets",
        params={
            "status": "in_progress",
            "city": "haifa",
            "urgent_only": "true",
            "date_from": "2025-03-01",
            "date_to": "2025-03-31",
            "category_id": str(wanted.category_id),
        },
        headers=headers,
    ).json()

    assert {row["ticket_number"] for row in narrowed["items"]} == {wanted.ticket_number}
    assert narrowed["total"] == 1
    assert response["total"] == 3  # in_progress or received, urgent, Haifa, March: three tickets


def test_comma_separated_status_filter(client: TestClient, admin, make_ticket) -> None:
    _, headers = admin
    make_ticket("received")
    make_ticket("closed")
    make_ticket("cancelled")

    body = client.get(f"{PREFIX}/tickets?status=closed,cancelled", headers=headers).json()

    assert sorted(row["status"] for row in body["items"]) == ["cancelled", "closed"]


def test_exact_search_overrides_the_filters(client: TestClient, admin, make_ticket) -> None:
    _, headers = admin
    ticket = make_ticket("closed")
    make_ticket("received")

    body = client.get(
        f"{PREFIX}/tickets",
        params={"search": f" {ticket.ticket_number} ", "status": "received"},
        headers=headers,
    ).json()

    assert [row["ticket_number"] for row in body["items"]] == [ticket.ticket_number]
    assert client.get(f"{PREFIX}/tickets?search=99999", headers=headers).json() == {
        "items": [],
        "page": 1,
        "page_size": 25,
        "total": 0,
    }


def test_urgent_first_puts_open_urgent_tickets_ahead(client: TestClient, admin, make_ticket) -> None:
    _, headers = admin
    old_urgent = make_ticket("received", urgent=True, created_at=LONG_AGO)
    new_plain = make_ticket("received", created_at=LONG_AGO + timedelta(days=1))
    closed_urgent = make_ticket("closed", urgent=True, created_at=LONG_AGO + timedelta(days=2))

    plain = client.get(f"{PREFIX}/tickets", headers=headers).json()["items"]
    first = client.get(f"{PREFIX}/tickets?urgent_first=true", headers=headers).json()["items"]

    assert [row["ticket_number"] for row in plain] == [
        closed_urgent.ticket_number,
        new_plain.ticket_number,
        old_urgent.ticket_number,
    ]
    assert [row["ticket_number"] for row in first] == [
        old_urgent.ticket_number,
        closed_urgent.ticket_number,
        new_plain.ticket_number,
    ]


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("page=0", "invalid_page"),
        ("page=-1", "invalid_page"),
        ("page=1.5", "invalid_page"),
        ("page=", "invalid_page"),
        ("page=%D9%A1", "invalid_page"),  # Arabic-Indic digit
        ("search=1234", "invalid_ticket_number"),
        ("search=100000", "invalid_ticket_number"),
        ("search=abc12", "invalid_ticket_number"),
        ("search=%2B1234", "invalid_ticket_number"),
        ("date_from=2025-02-30", "invalid_date_range"),
        ("date_from=2025-03-10&date_to=2025-03-09", "invalid_date_range"),
        ("date_to=yesterday", "invalid_date_range"),
        ("status=pending", "invalid_filter"),
        ("status=received,", "invalid_filter"),
        ("category_id=not-a-uuid", "invalid_filter"),
        (f"category_id={uuid.uuid4()}", "invalid_filter"),  # no such category
        ("urgent_only=yes", "invalid_filter"),
        ("urgent_first=1", "invalid_filter"),
    ],
)
def test_list_parameter_failures_carry_the_package_code(
    client: TestClient, admin, query: str, code: str
) -> None:
    _, headers = admin

    response = client.get(f"{PREFIX}/tickets?{query}", headers=headers)

    assert response.status_code == 422
    assert response.json() == {"detail": {"error": {"code": code}}}


def test_a_search_does_not_hide_a_bad_filter(client: TestClient, admin, make_ticket) -> None:
    _, headers = admin
    ticket = make_ticket()

    response = client.get(
        f"{PREFIX}/tickets?search={ticket.ticket_number}&date_from=2025-03-10&date_to=2025-03-09",
        headers=headers,
    )

    assert (response.status_code, _code(response)) == (422, "invalid_date_range")


def test_blank_filters_restrict_nothing(client: TestClient, admin, make_ticket) -> None:
    _, headers = admin
    make_ticket()
    make_ticket("closed")

    body = client.get(
        f"{PREFIX}/tickets?search=&status=&category_id=&city=&date_from=&date_to=&urgent_only=",
        headers=headers,
    ).json()

    assert body["total"] == 2


# --------------------------------------------------------------------------------- detail


def test_detail_shape_and_history_order(client: TestClient, admin, make_ticket, session: Session) -> None:
    _, headers = admin
    ticket = make_ticket("in_progress", name="Dana")
    for offset, label in ((2, "third"), (0, "first"), (1, "second")):
        session.add(
            HistoryEntry(
                ticket_id=ticket.id,
                entry_text=label,
                technician_name="T",
                created_at=LONG_AGO + timedelta(minutes=offset),
            )
        )
    session.commit()

    response = client.get(f"{PREFIX}/tickets/{ticket.ticket_number}", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
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
        "phone",
        "description",
        "has_photo",
        "history",
        "updated_at",
        "opened_by_username",
    }
    assert body["opened_by_username"] is None  # opened by the customer, not by staff
    assert body["customer_name"] == "Dana Levi"
    assert (body["assigned_technician_name"], body["has_photo"]) == ("Dana", False)
    assert [entry["entry_text"] for entry in body["history"]] == ["first", "second", "third"]
    assert set(body["history"][0]) == {"id", "entry_text", "technician_name", "created_at", "edited_at"}
    assert body["history"][0]["edited_at"] is None


MALFORMED_NUMBERS = ["99999", "abc", "1234", "099999", "123456", "%D9%A1%D9%A2%D9%A3%D9%A4%D9%A5", "1e4"]


@pytest.mark.parametrize("raw", MALFORMED_NUMBERS)
def test_a_malformed_or_missing_ticket_number_is_the_same_404(client: TestClient, admin, raw: str) -> None:
    _, headers = admin
    missing = client.get(f"{PREFIX}/tickets/99999", headers=headers)

    response = client.get(f"{PREFIX}/tickets/{raw}", headers=headers)

    assert response.status_code == 404
    assert response.json() == missing.json() == {"detail": {"error": {"code": "ticket_not_found"}}}


# --------------------------------------------------------------------------------- status


def test_approval_defaults_the_technician_name_to_the_acting_username(
    client: TestClient, technician, make_ticket, session: Session
) -> None:
    user, headers = technician
    ticket = make_ticket("received")

    response = client.post(
        f"{PREFIX}/tickets/{ticket.ticket_number}/status",
        json=_status_body("received", "in_progress"),
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["assigned_technician_name"]) == ("in_progress", user.username)
    assert body["history"] == []
    status, name, updated = _stored(session, ticket.ticket_number)
    assert (status, name) == ("in_progress", user.username)
    assert updated > LONG_AGO


def test_approval_with_an_explicit_name_and_reopen_keeps_it(
    client: TestClient, admin, make_ticket, session: Session
) -> None:
    _, headers = admin
    ticket = make_ticket("received")
    path = f"{PREFIX}/tickets/{ticket.ticket_number}/status"

    approved = client.post(
        path,
        json=_status_body("received", "in_progress", assigned_technician_name="  Moshe  "),
        headers=headers,
    )
    closed = client.post(path, json=_status_body("in_progress", "closed"), headers=headers)
    reopened = client.post(
        path,
        json=_status_body("closed", "in_progress", assigned_technician_name="Someone Else"),
        headers=headers,
    )

    assert approved.json()["assigned_technician_name"] == "Moshe"
    assert closed.json()["status"] == "closed"
    assert (reopened.status_code, reopened.json()["status"]) == (200, "in_progress")
    assert reopened.json()["assigned_technician_name"] == "Moshe"
    assert _stored(session, ticket.ticket_number)[:2] == ("in_progress", "Moshe")


@pytest.mark.parametrize(
    ("stored", "expected", "target", "status", "code"),
    [
        ("received", "in_progress", "closed", 409, "ticket_status_changed"),  # stale expectation
        ("received", "closed", "in_progress", 409, "ticket_status_changed"),
        ("received", "received", "closed", 409, "invalid_transition"),
        ("received", "received", "cancelled", 409, "invalid_transition"),  # staff may never cancel
        ("in_progress", "in_progress", "cancelled", 409, "invalid_transition"),
        ("received", "received", "received", 409, "invalid_transition"),  # target equals stored
        ("closed", "closed", "received", 409, "invalid_transition"),
        ("cancelled", "cancelled", "in_progress", 409, "invalid_transition"),
        ("in_progress", "in_progress", "received", 409, "invalid_transition"),
        ("received", "pending", "in_progress", 422, "invalid_expected_status"),
        ("received", "received", "approved", 422, "invalid_target_status"),
        ("received", 5, "in_progress", 422, "invalid_expected_status"),
    ],
)
def test_refused_status_changes_leave_the_ticket_alone(
    client: TestClient,
    admin,
    make_ticket,
    session: Session,
    stored: str,
    expected: Any,
    target: str,
    status: int,
    code: str,
) -> None:
    _, headers = admin
    ticket = make_ticket(stored, name="Keep")

    response = client.post(
        f"{PREFIX}/tickets/{ticket.ticket_number}/status",
        json={"expected_status": expected, "target_status": target},
        headers=headers,
    )

    assert (response.status_code, response.json()) == (status, {"detail": {"error": {"code": code}}})
    assert _stored(session, ticket.ticket_number) == (stored, "Keep", LONG_AGO)


def test_a_status_change_with_a_missing_expected_or_target_is_422(
    client: TestClient, admin, make_ticket, session: Session
) -> None:
    _, headers = admin
    ticket = make_ticket("received")
    path = f"{PREFIX}/tickets/{ticket.ticket_number}/status"

    no_expected = client.post(path, json=_status_body(None, "in_progress"), headers=headers)
    no_target = client.post(path, json=_status_body("received", None), headers=headers)
    empty = client.post(path, json={}, headers=headers)

    assert (no_expected.status_code, _code(no_expected)) == (422, "invalid_expected_status")
    assert (no_target.status_code, _code(no_target)) == (422, "invalid_target_status")
    assert (empty.status_code, _code(empty)) == (422, "invalid_expected_status")
    assert _stored(session, ticket.ticket_number) == ("received", "", LONG_AGO)


def test_an_over_long_approval_name_is_refused_and_changes_nothing(
    client: TestClient, admin, make_ticket, session: Session
) -> None:
    _, headers = admin
    ticket = make_ticket("received")

    response = client.post(
        f"{PREFIX}/tickets/{ticket.ticket_number}/status",
        json=_status_body("received", "in_progress", assigned_technician_name="x" * 101),
        headers=headers,
    )

    assert (response.status_code, _code(response)) == (422, "invalid_assigned_technician_name")
    assert _stored(session, ticket.ticket_number) == ("received", "", LONG_AGO)


def test_status_change_of_a_missing_or_malformed_ticket_is_404(client: TestClient, admin) -> None:
    _, headers = admin
    for raw in ("99999", "abc", "0"):
        response = client.post(
            f"{PREFIX}/tickets/{raw}/status", json=_status_body("received", "in_progress"), headers=headers
        )
        assert (response.status_code, _code(response)) == (404, "ticket_not_found"), raw


def test_ticket_status_changed_comes_before_invalid_transition(
    client: TestClient, admin, make_ticket
) -> None:
    _, headers = admin
    ticket = make_ticket("closed")

    response = client.post(
        f"{PREFIX}/tickets/{ticket.ticket_number}/status",
        json=_status_body("received", "cancelled"),
        headers=headers,
    )

    assert (response.status_code, _code(response)) == (409, "ticket_status_changed")


def test_unknown_body_field_is_unknown_field(client: TestClient, admin, make_ticket) -> None:
    _, headers = admin
    ticket = make_ticket()

    response = client.post(
        f"{PREFIX}/tickets/{ticket.ticket_number}/status",
        json={"expected_status": "received", "target_status": "in_progress", "extra": 1},
        headers=headers,
    )

    assert response.status_code == 422
    error = response.json()["detail"]["error"]
    assert error["code"] == "unknown_field"
    assert error["fields"] == {"extra": "unknown_field"}


# --------------------------------------------------------------------------------- assigned technician


@pytest.mark.parametrize(
    ("sent", "stored"), [("  Dana Levi ", "Dana Levi"), ("", ""), ("   ", ""), ("x" * 100, "x" * 100)]
)
def test_assigned_technician_is_trimmed_and_blank_means_unassigned(
    client: TestClient, admin, make_ticket, session: Session, sent: str, stored: str
) -> None:
    _, headers = admin
    ticket = make_ticket("received", name="Old")

    response = client.put(
        f"{PREFIX}/tickets/{ticket.ticket_number}/assigned-technician",
        json={"assigned_technician_name": sent},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["assigned_technician_name"] == stored
    status, name, updated = _stored(session, ticket.ticket_number)
    assert (status, name) == ("received", stored)
    assert updated > LONG_AGO


@pytest.mark.parametrize("status", ["received", "in_progress", "closed", "cancelled"])
def test_assigned_technician_can_be_set_in_every_status(
    client: TestClient, admin, make_ticket, status: str
) -> None:
    _, headers = admin
    ticket = make_ticket(status)

    response = client.put(
        f"{PREFIX}/tickets/{ticket.ticket_number}/assigned-technician",
        json={"assigned_technician_name": "Dana"},
        headers=headers,
    )

    assert (response.status_code, response.json()["status"]) == (200, status)


@pytest.mark.parametrize(
    "body", [{"assigned_technician_name": "x" * 101}, {}, {"assigned_technician_name": None}]
)
def test_bad_assigned_technician_is_422_and_changes_nothing(
    client: TestClient, admin, make_ticket, session: Session, body: dict[str, Any]
) -> None:
    _, headers = admin
    ticket = make_ticket(name="Keep")

    response = client.put(
        f"{PREFIX}/tickets/{ticket.ticket_number}/assigned-technician", json=body, headers=headers
    )

    assert (response.status_code, _code(response)) == (422, "invalid_assigned_technician_name")
    assert _stored(session, ticket.ticket_number) == ("received", "Keep", LONG_AGO)


def test_assigned_technician_of_a_missing_or_malformed_ticket_is_404(client: TestClient, admin) -> None:
    _, headers = admin
    for raw in ("99999", "abc"):
        response = client.put(
            f"{PREFIX}/tickets/{raw}/assigned-technician",
            json={"assigned_technician_name": "A"},
            headers=headers,
        )
        assert (response.status_code, _code(response)) == (404, "ticket_not_found"), raw


# --------------------------------------------------------------------------------- history


def test_add_entry_defaults_the_technician_name_to_the_acting_username(
    client: TestClient, technician, make_ticket, session: Session
) -> None:
    user, headers = technician
    ticket = make_ticket("closed")
    path = f"{PREFIX}/tickets/{ticket.ticket_number}/history"

    defaulted = client.post(path, json={"entry_text": "  replaced valve  "}, headers=headers)
    null_name = client.post(path, json={"entry_text": "second", "technician_name": None}, headers=headers)
    explicit = client.post(path, json={"entry_text": "third", "technician_name": " Moshe "}, headers=headers)

    assert defaulted.status_code == 201
    body = defaulted.json()
    assert set(body) == {"id", "entry_text", "technician_name", "created_at", "edited_at"}
    assert (body["entry_text"], body["technician_name"]) == ("replaced valve", user.username)
    assert body["edited_at"] is None
    assert null_name.json()["technician_name"] == user.username
    assert explicit.json()["technician_name"] == "Moshe"
    assert _entries(session, ticket.ticket_number) == [
        ("replaced valve", user.username),
        ("second", user.username),
        ("third", "Moshe"),
    ]
    assert _stored(session, ticket.ticket_number)[2] == LONG_AGO  # an entry does not touch updated_at


def test_add_entry_reports_every_failing_field(
    client: TestClient, admin, make_ticket, session: Session
) -> None:
    _, headers = admin
    ticket = make_ticket()
    path = f"{PREFIX}/tickets/{ticket.ticket_number}/history"

    both = client.post(path, json={"entry_text": "  ", "technician_name": ""}, headers=headers)
    text_only = client.post(path, json={"entry_text": "x" * 2001, "technician_name": "A"}, headers=headers)
    name_only = client.post(path, json={"entry_text": "ok", "technician_name": "n" * 101}, headers=headers)
    missing_text = client.post(path, json={"technician_name": "A"}, headers=headers)
    wrong_type = client.post(path, json={"entry_text": 5, "technician_name": "A"}, headers=headers)

    assert both.status_code == 422
    assert both.json()["detail"]["error"] == {
        "code": "invalid_history_text",
        "fields": {"entry_text": "invalid_history_text", "technician_name": "invalid_technician_name"},
    }
    assert _code(text_only) == "invalid_history_text"
    assert _code(name_only) == "invalid_technician_name"
    assert (missing_text.status_code, _code(missing_text)) == (422, "invalid_history_text")
    assert (wrong_type.status_code, _code(wrong_type)) == (422, "invalid_history_text")
    assert _entries(session, ticket.ticket_number) == []


def test_add_entry_of_a_missing_or_malformed_ticket_is_404(client: TestClient, admin) -> None:
    _, headers = admin
    for raw in ("99999", "abc"):
        response = client.post(
            f"{PREFIX}/tickets/{raw}/history",
            json={"entry_text": "x", "technician_name": "A"},
            headers=headers,
        )
        assert (response.status_code, _code(response)) == (404, "ticket_not_found"), raw


def test_validation_comes_before_the_ticket_lookup(client: TestClient, admin) -> None:
    _, headers = admin

    response = client.post(
        f"{PREFIX}/tickets/99999/history", json={"entry_text": "", "technician_name": "A"}, headers=headers
    )

    assert (response.status_code, _code(response)) == (422, "invalid_history_text")


def test_edit_entry_replaces_both_values_and_sets_edited_at(
    client: TestClient, admin, make_ticket, session: Session
) -> None:
    _, headers = admin
    ticket = make_ticket()
    created = client.post(
        f"{PREFIX}/tickets/{ticket.ticket_number}/history",
        json={"entry_text": "first", "technician_name": "A"},
        headers=headers,
    ).json()

    response = client.put(
        f"{PREFIX}/tickets/{ticket.ticket_number}/history/{created['id']}",
        json={"entry_text": " second ", "technician_name": " B "},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["entry_text"], body["technician_name"]) == (created["id"], "second", "B")
    # SQLite hands timezone-aware columns back naive, so only the instant is compared.
    assert body["created_at"].removesuffix("Z") == created["created_at"].removesuffix("Z")
    assert body["edited_at"] is not None
    assert _entries(session, ticket.ticket_number) == [("second", "B")]


def test_edit_entry_does_not_default_the_name(client: TestClient, admin, make_ticket) -> None:
    _, headers = admin
    ticket = make_ticket()
    created = client.post(
        f"{PREFIX}/tickets/{ticket.ticket_number}/history",
        json={"entry_text": "first", "technician_name": "A"},
        headers=headers,
    ).json()

    response = client.put(
        f"{PREFIX}/tickets/{ticket.ticket_number}/history/{created['id']}",
        json={"entry_text": "second"},
        headers=headers,
    )

    assert (response.status_code, _code(response)) == (422, "invalid_technician_name")


def test_edit_entry_of_another_ticket_or_a_missing_entry_is_history_entry_not_found(
    client: TestClient, admin, make_ticket, session: Session
) -> None:
    _, headers = admin
    mine = make_ticket()
    other = make_ticket()
    entry = client.post(
        f"{PREFIX}/tickets/{other.ticket_number}/history",
        json={"entry_text": "theirs", "technician_name": "A"},
        headers=headers,
    ).json()
    body = {"entry_text": "changed", "technician_name": "B"}

    for entry_id in (entry["id"], str(uuid.uuid4()), "not-a-uuid", str(uuid.UUID(int=0))):
        path = f"{PREFIX}/tickets/{mine.ticket_number}/history/{entry_id}"
        response = client.put(path, json=body, headers=headers)
        assert response.status_code == 404, entry_id
        assert response.json() == {"detail": {"error": {"code": "history_entry_not_found"}}}, entry_id

    assert _entries(session, other.ticket_number) == [("theirs", "A")]


def test_edit_entry_check_order_is_validation_then_ticket_then_entry(
    client: TestClient, admin, make_ticket
) -> None:
    _, headers = admin
    ticket = make_ticket()
    entry_id = uuid.uuid4()

    invalid = client.put(
        f"{PREFIX}/tickets/99999/history/{entry_id}",
        json={"entry_text": "", "technician_name": "A"},
        headers=headers,
    )
    no_ticket = client.put(
        f"{PREFIX}/tickets/99999/history/{entry_id}",
        json={"entry_text": "x", "technician_name": "A"},
        headers=headers,
    )
    malformed_ticket = client.put(
        f"{PREFIX}/tickets/abc/history/{entry_id}",
        json={"entry_text": "x", "technician_name": "A"},
        headers=headers,
    )
    no_entry = client.put(
        f"{PREFIX}/tickets/{ticket.ticket_number}/history/{entry_id}",
        json={"entry_text": "x", "technician_name": "A"},
        headers=headers,
    )

    assert _code(invalid) == "invalid_history_text"
    assert (no_ticket.status_code, _code(no_ticket)) == (404, "ticket_not_found")
    assert (malformed_ticket.status_code, _code(malformed_ticket)) == (404, "ticket_not_found")
    assert (no_entry.status_code, _code(no_entry)) == (404, "history_entry_not_found")


# --------------------------------------------------------------------------------- envelope


def test_every_code_the_router_answers_is_in_the_catalogue() -> None:
    assert set(staff_router._ERROR_STATUS) <= set(ERROR_CODES)


def test_a_database_failure_is_503_service_unavailable(
    client: TestClient, admin, make_ticket, session: Session
) -> None:
    _, headers = admin
    ticket = make_ticket()
    session.execute(text("DROP TABLE svc_history_entries"))
    session.commit()

    response = client.get(f"{PREFIX}/tickets/{ticket.ticket_number}", headers=headers)

    assert response.status_code == 503
    assert response.json() == {"detail": {"error": {"code": "service_unavailable"}}}
