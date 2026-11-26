"""Resolving a ticket: service history and replaced parts (task 30.4; R29.1 to R29.3, R29.7, R29.9).

`POST /staff/tickets/{n}/status` with the target `closed` carries a `resolution`. A small application
mounts only `staff_router.router`, with `get_db` overridden to give each request its own session on the
in-memory test database, as in `test_service_api_staff_tickets.py`. Tokens are real staff access tokens.
Because requests use their own sessions, what a test reads afterwards is what the router committed.

The state of a ticket is read as one tuple (`_state`): the status, the assigned name, the last-modified
time, the history entries and the recorded part ids. Every refusal is checked against that tuple, so a
failure that changed any one of the three tables shows.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import create_access_token
from app.db.session import get_db
from app.models.user import User, UserRole
from app.service_tickets.api import staff_router
from app.service_tickets.models import Category, Customer, HistoryEntry, Part, Ticket, TicketPart

LONG_AGO = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
PREFIX = "/api/service/staff"

_numbers = itertools.count(30000)
_seq = itertools.count(1)
_phones = itertools.count(1)

#: Leave a key out of the resolution body.
MISSING = object()


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
    def _staff(role: UserRole = UserRole.TECHNICIAN) -> tuple[User, dict[str, str]]:
        user = make_user(role=role, is_2fa_enabled=True, must_change_password=False)
        token = create_access_token(user_id=user.id, token_version=user.token_version, role=role.value)
        return user, {"Authorization": f"Bearer {token}"}

    return _staff


@pytest.fixture
def headers(staff) -> dict[str, str]:
    return staff()[1]


@pytest.fixture
def customer(session: Session) -> Customer:
    row = Customer(
        phone=f"05{next(_phones):08d}", full_name="Dana Levi", full_name_key="dana levi", password_hash="x"
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def category(session: Session) -> Category:
    row = Category(
        seq=next(_seq), name_he="חשמל", name_en="Electric", name_he_key="חשמל", name_en_key="electric"
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def make_ticket(session: Session, customer: Customer, category: Category):
    def _make(status: str = "in_progress", *, name: str = "Moshe") -> Ticket:
        ticket = Ticket(
            ticket_number=next(_numbers),
            customer_id=customer.id,
            category_id=category.id,
            city="Haifa",
            city_key="haifa",
            apartment="12",
            phone="0501234567",
            description="leaking tap",
            status=status,
            assigned_technician_name=name,
            created_at=LONG_AGO,
            created_date=LONG_AGO.date(),
            updated_at=LONG_AGO,
        )
        session.add(ticket)
        session.commit()
        return ticket

    return _make


@pytest.fixture
def make_part(session: Session):
    """`make_part("Gasket")` is a committed part; the sequence number follows the creation order."""

    def _make(name: str, *, active: bool = True) -> Part:
        part = Part(
            seq=next(_seq),
            name_he=f"{name}-he",
            name_en=name,
            name_he_key=f"{name}-he".casefold(),
            name_en_key=name.casefold(),
            is_active=active,
        )
        session.add(part)
        session.commit()
        return part

    return _make


# --------------------------------------------------------------------------------- helpers


def _resolution(**overrides: Any) -> dict[str, Any]:
    """A valid resolution without parts; a value of `MISSING` leaves that key out."""
    body: dict[str, Any] = {
        "entry_text": "Replaced the washer",
        "technician_name": "Dana",
        "parts_replaced": False,
        "part_ids": [],
    }
    body.update(overrides)
    return {key: value for key, value in body.items() if value is not MISSING}


def _with_parts(*parts: Part) -> dict[str, Any]:
    return _resolution(parts_replaced=True, part_ids=[str(part.id) for part in parts])


def _status_url(ticket: Ticket) -> str:
    return f"{PREFIX}/tickets/{ticket.ticket_number}/status"


def _close(
    client: TestClient,
    headers: dict[str, str],
    ticket: Ticket,
    resolution: Any = None,
    *,
    expected: str = "in_progress",
) -> Any:
    body: dict[str, Any] = {"expected_status": expected, "target_status": "closed"}
    if resolution is not None:
        body["resolution"] = resolution
    return client.post(_status_url(ticket), json=body, headers=headers)


def _reopen(client: TestClient, headers: dict[str, str], ticket: Ticket) -> Any:
    body = {"expected_status": "closed", "target_status": "in_progress"}
    return client.post(_status_url(ticket), json=body, headers=headers)


def _code(response: Any) -> str:
    return response.json()["detail"]["error"]["code"]


def _fields(response: Any) -> dict[str, str]:
    return response.json()["detail"]["error"]["fields"]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _state(session: Session, number: int) -> tuple[Any, ...]:
    """Status, assigned name, last-modified time, history entries and part ids, read from the database."""
    session.rollback()
    row = session.execute(
        select(Ticket.id, Ticket.status, Ticket.assigned_technician_name, Ticket.updated_at).where(
            Ticket.ticket_number == number
        )
    ).one()
    entries = session.execute(
        select(HistoryEntry.entry_text, HistoryEntry.technician_name, HistoryEntry.created_at)
        .where(HistoryEntry.ticket_id == row.id)
        .order_by(HistoryEntry.created_at, HistoryEntry.id)
    ).all()
    part_ids = session.execute(select(TicketPart.part_id).where(TicketPart.ticket_id == row.id)).scalars()
    return (
        row.status,
        row.assigned_technician_name,
        _utc(row.updated_at),
        tuple(tuple(entry) for entry in entries),
        frozenset(part_ids),
    )


def _record(session: Session, ticket: Ticket, *parts: Part) -> None:
    """Parts and one history entry already recorded on the ticket, as an earlier resolution leaves them."""
    for part in parts:
        session.add(TicketPart(ticket_id=ticket.id, part_id=part.id))
    session.add(
        HistoryEntry(
            ticket_id=ticket.id,
            entry_text="Earlier note",
            technician_name="Moshe",
            created_at=LONG_AGO,
        )
    )
    session.commit()


def _detail(client: TestClient, headers: dict[str, str], ticket: Ticket) -> dict[str, Any]:
    response = client.get(f"{PREFIX}/tickets/{ticket.ticket_number}", headers=headers)
    assert response.status_code == 200
    return response.json()


def _names(body: dict[str, Any]) -> list[str]:
    return [part["name_en"] for part in body["parts_replaced"]]


# --------------------------------------------------------------------------------- success


def test_closing_without_parts_stores_the_status_the_entry_and_no_parts(
    client: TestClient, session: Session, headers, make_ticket
) -> None:
    ticket = make_ticket("in_progress", name="Moshe")
    resolution = _resolution(entry_text="  Replaced the washer  ", technician_name="  Dana  ")

    response = _close(client, headers, ticket, resolution)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "closed"
    assert body["parts_replaced"] == []
    (entry,) = body["history"]
    assert (entry["entry_text"], entry["technician_name"], entry["edited_at"]) == (
        "Replaced the washer",
        "Dana",
        None,
    )
    assert body["assigned_technician_name"] == "Moshe"  # the entry's name is not the assigned name
    status, name, updated, entries, parts = _state(session, ticket.ticket_number)
    assert (status, name, parts) == ("closed", "Moshe", frozenset())
    assert [entry[:2] for entry in entries] == [("Replaced the washer", "Dana")]
    assert updated > LONG_AGO
    assert _detail(client, headers, ticket) == body  # the answer is the detail the next GET shows


def test_closing_with_parts_returns_them_in_part_order_with_current_names(
    client: TestClient, session: Session, headers, make_ticket, make_part
) -> None:
    gasket, valve, filter_ = make_part("Gasket"), make_part("Valve"), make_part("Filter")
    ticket = make_ticket("in_progress")

    response = _close(client, headers, ticket, _with_parts(filter_, gasket))  # sent out of order

    assert response.status_code == 200
    body = response.json()
    assert _names(body) == ["Gasket", "Filter"]  # by part sequence, not by the order sent
    assert set(body["parts_replaced"][0]) == {"id", "name_he", "name_en"}
    assert body["parts_replaced"][0] == {"id": str(gasket.id), "name_he": "Gasket-he", "name_en": "Gasket"}
    assert _state(session, ticket.ticket_number)[4] == {gasket.id, filter_.id}
    assert valve.id not in _state(session, ticket.ticket_number)[4]


def test_the_detail_shows_current_names_also_of_a_part_deactivated_or_renamed_later(
    client: TestClient, session: Session, headers, make_ticket, make_part
) -> None:
    gasket = make_part("Gasket")
    ticket = make_ticket("in_progress")
    assert _close(client, headers, ticket, _with_parts(gasket)).status_code == 200

    session.execute(
        update(Part).where(Part.id == gasket.id).values(is_active=False, name_en="Gasket v2", name_he="אטם")
    )
    session.commit()

    body = _detail(client, headers, ticket)
    assert body["parts_replaced"] == [{"id": str(gasket.id), "name_he": "אטם", "name_en": "Gasket v2"}]


def test_twenty_parts_are_accepted(client: TestClient, headers, make_ticket, make_part) -> None:
    parts = [make_part(f"Part {index:02d}") for index in range(20)]
    ticket = make_ticket("in_progress")

    response = _close(client, headers, ticket, _with_parts(*reversed(parts)))

    assert response.status_code == 200
    assert _names(response.json()) == [part.name_en for part in parts]


def test_the_entry_is_in_the_staff_detail_history_with_the_given_technician_name(
    client: TestClient, session: Session, headers, make_ticket
) -> None:
    ticket = make_ticket("in_progress", name="Moshe")
    _record(session, ticket)  # one earlier note by Moshe

    assert _close(client, headers, ticket, _resolution(technician_name="Dana")).status_code == 200

    history = _detail(client, headers, ticket)["history"]
    assert [(entry["entry_text"], entry["technician_name"]) for entry in history] == [
        ("Earlier note", "Moshe"),
        ("Replaced the washer", "Dana"),
    ]


def test_a_reopen_takes_no_resolution_and_keeps_the_parts(
    client: TestClient, session: Session, headers, make_ticket, make_part
) -> None:
    gasket, valve = make_part("Gasket"), make_part("Valve")
    ticket = make_ticket("in_progress")
    assert _close(client, headers, ticket, _with_parts(gasket, valve)).status_code == 200

    reopened = _reopen(client, headers, ticket)

    assert (reopened.status_code, reopened.json()["status"]) == (200, "in_progress")
    assert _names(reopened.json()) == ["Gasket", "Valve"]
    assert len(reopened.json()["history"]) == 1  # the reopen adds no entry
    assert _state(session, ticket.ticket_number)[4] == {gasket.id, valve.id}


def test_a_second_resolution_replaces_the_parts_and_adds_a_second_entry(
    client: TestClient, session: Session, headers, make_ticket, make_part
) -> None:
    gasket, valve, filter_ = make_part("Gasket"), make_part("Valve"), make_part("Filter")
    ticket = make_ticket("in_progress")
    assert _close(client, headers, ticket, _with_parts(gasket, valve)).status_code == 200
    assert _reopen(client, headers, ticket).status_code == 200

    second = _close(client, headers, ticket, {**_with_parts(valve, filter_), "entry_text": "Second visit"})

    assert second.status_code == 200
    assert _names(second.json()) == ["Valve", "Filter"]  # the earlier set is gone, not merged
    assert [entry["entry_text"] for entry in second.json()["history"]] == [
        "Replaced the washer",
        "Second visit",
    ]
    assert _state(session, ticket.ticket_number)[4] == {valve.id, filter_.id}


def test_a_second_resolution_without_parts_removes_the_recorded_ones(
    client: TestClient, session: Session, headers, make_ticket, make_part
) -> None:
    gasket = make_part("Gasket")
    ticket = make_ticket("in_progress")
    assert _close(client, headers, ticket, _with_parts(gasket)).status_code == 200
    assert _reopen(client, headers, ticket).status_code == 200

    second = _close(client, headers, ticket, _resolution(entry_text="Nothing was replaced after all"))

    assert second.status_code == 200
    assert second.json()["parts_replaced"] == []
    assert _state(session, ticket.ticket_number)[4] == frozenset()
    assert _detail(client, headers, ticket)["parts_replaced"] == []


def test_the_staff_list_rows_carry_neither_parts_nor_history(
    client: TestClient, headers, make_ticket, make_part
) -> None:
    ticket = make_ticket("in_progress")
    assert _close(client, headers, ticket, _with_parts(make_part("Gasket"))).status_code == 200

    rows = client.get(f"{PREFIX}/tickets", headers=headers).json()["items"]

    assert rows and all("parts_replaced" not in row and "history" not in row for row in rows)


# --------------------------------------------------------------------------------- resolution required


@pytest.mark.parametrize("given", ["absent", "null", "string", "list"])
def test_closing_without_a_resolution_is_422_resolution_required_and_changes_nothing(
    client: TestClient, session: Session, headers, make_ticket, make_part, given: str
) -> None:
    ticket = make_ticket("in_progress")
    _record(session, ticket, make_part("Gasket"))
    before = _state(session, ticket.ticket_number)
    body: dict[str, Any] = {"expected_status": "in_progress", "target_status": "closed"}
    if given != "absent":
        body["resolution"] = {"null": None, "string": "done", "list": []}[given]

    response = client.post(_status_url(ticket), json=body, headers=headers)

    assert (response.status_code, _code(response)) == (422, "resolution_required")
    assert _state(session, ticket.ticket_number) == before


@pytest.mark.parametrize(
    ("stored", "expected", "target"),
    [("received", "received", "in_progress"), ("closed", "closed", "in_progress")],
)
def test_a_resolution_on_a_target_other_than_closed_is_422_unknown_field(
    client: TestClient,
    session: Session,
    headers,
    make_ticket,
    make_part,
    stored: str,
    expected: str,
    target: str,
) -> None:
    ticket = make_ticket(stored)
    _record(session, ticket, make_part("Gasket"))
    before = _state(session, ticket.ticket_number)

    response = client.post(
        _status_url(ticket),
        json={"expected_status": expected, "target_status": target, "resolution": _resolution()},
        headers=headers,
    )

    assert (response.status_code, _code(response)) == (422, "unknown_field")
    assert _fields(response) == {"resolution": "unknown_field"}
    assert _state(session, ticket.ticket_number) == before


def test_an_unknown_key_inside_the_resolution_is_422_unknown_field(
    client: TestClient, session: Session, headers, make_ticket
) -> None:
    ticket = make_ticket("in_progress")
    before = _state(session, ticket.ticket_number)

    response = _close(client, headers, ticket, {**_resolution(), "notes": "x"})

    assert (response.status_code, _code(response)) == (422, "unknown_field")
    assert _fields(response) == {"resolution": "unknown_field"}
    assert _state(session, ticket.ticket_number) == before


# --------------------------------------------------------------------------------- invalid resolutions


TEXT = "invalid_history_text"
NAME = "invalid_technician_name"
FLAG = "invalid_request"
PART = "invalid_part"

#: name -> (the resolution overrides, built from the parts of the test; the expected `fields`, in order).
INVALID: dict[str, tuple[Callable[[dict[str, Part]], dict[str, Any]], dict[str, str]]] = {
    "blank text": (lambda p: {"entry_text": " \t "}, {"entry_text": TEXT}),
    "empty text": (lambda p: {"entry_text": ""}, {"entry_text": TEXT}),
    "text over 2000": (lambda p: {"entry_text": "x" * 2001}, {"entry_text": TEXT}),
    "text not a string": (lambda p: {"entry_text": 5}, {"entry_text": TEXT}),
    "text null": (lambda p: {"entry_text": None}, {"entry_text": TEXT}),
    "text missing": (lambda p: {"entry_text": MISSING}, {"entry_text": TEXT}),
    "blank name": (lambda p: {"technician_name": "  "}, {"technician_name": NAME}),
    "name over 100": (lambda p: {"technician_name": "n" * 101}, {"technician_name": NAME}),
    "name not a string": (lambda p: {"technician_name": ["Dana"]}, {"technician_name": NAME}),
    "name missing": (lambda p: {"technician_name": MISSING}, {"technician_name": NAME}),
    "text and name": (
        lambda p: {"entry_text": "", "technician_name": ""},
        {"entry_text": TEXT, "technician_name": NAME},
    ),
    "flag missing": (lambda p: {"parts_replaced": MISSING}, {"parts_replaced": FLAG}),
    "flag null": (lambda p: {"parts_replaced": None}, {"parts_replaced": FLAG}),
    "flag string true": (lambda p: {"parts_replaced": "true"}, {"parts_replaced": FLAG}),
    "flag string false": (lambda p: {"parts_replaced": "false"}, {"parts_replaced": FLAG}),
    "flag one": (lambda p: {"parts_replaced": 1}, {"parts_replaced": FLAG}),
    "flag zero": (lambda p: {"parts_replaced": 0}, {"parts_replaced": FLAG}),
    "flag not boolean with parts": (
        lambda p: {"parts_replaced": "yes", "part_ids": [str(p["active"].id)]},
        {"parts_replaced": FLAG},  # the parts are not looked at
    ),
    "true and no list": (lambda p: {"parts_replaced": True, "part_ids": []}, {"part_ids": "part_required"}),
    "true and null list": (
        lambda p: {"parts_replaced": True, "part_ids": None},
        {"part_ids": "part_required"},
    ),
    "true and list missing": (
        lambda p: {"parts_replaced": True, "part_ids": MISSING},
        {"part_ids": "part_required"},
    ),
    "false with a part": (
        lambda p: {"parts_replaced": False, "part_ids": [str(p["active"].id)]},
        {"part_ids": PART},
    ),
    "list not a list": (lambda p: {"parts_replaced": True, "part_ids": "abc"}, {"part_ids": PART}),
    "list an object": (lambda p: {"parts_replaced": True, "part_ids": {"a": 1}}, {"part_ids": PART}),
    "malformed id": (lambda p: {"parts_replaced": True, "part_ids": ["not-a-uuid"]}, {"part_ids": PART}),
    "id hyphenless": (
        lambda p: {"parts_replaced": True, "part_ids": [p["active"].id.hex]},
        {"part_ids": PART},
    ),
    "id a number": (lambda p: {"parts_replaced": True, "part_ids": [7]}, {"part_ids": PART}),
    "id null": (lambda p: {"parts_replaced": True, "part_ids": [None]}, {"part_ids": PART}),
    "unknown id": (
        lambda p: {"parts_replaced": True, "part_ids": [str(uuid.uuid4())]},
        {"part_ids": PART},
    ),
    "inactive id": (
        lambda p: {"parts_replaced": True, "part_ids": [str(p["inactive"].id)]},
        {"part_ids": PART},
    ),
    "one good and one inactive": (
        lambda p: {"parts_replaced": True, "part_ids": [str(p["active"].id), str(p["inactive"].id)]},
        {"part_ids": PART},
    ),
    "repeated id": (
        lambda p: {"parts_replaced": True, "part_ids": [str(p["active"].id)] * 2},
        {"part_ids": PART},
    ),
    "repeated id in another case": (
        lambda p: {
            "parts_replaced": True,
            "part_ids": [str(p["active"].id), str(p["active"].id).upper()],
        },
        {"part_ids": PART},
    ),
    "21 ids": (
        lambda p: {"parts_replaced": True, "part_ids": [str(uuid.uuid4()) for _ in range(21)]},
        {"part_ids": PART},
    ),
    "all of them at once": (
        lambda p: {
            "entry_text": "",
            "technician_name": "n" * 101,
            "parts_replaced": True,
            "part_ids": [str(p["inactive"].id)],
        },
        {"entry_text": TEXT, "technician_name": NAME, "part_ids": PART},
    ),
    "text, name and a flag that is no boolean": (
        lambda p: {"entry_text": "", "technician_name": "", "parts_replaced": "yes"},
        {"entry_text": TEXT, "technician_name": NAME, "parts_replaced": FLAG},
    ),
    "text and no parts for a true flag": (
        lambda p: {"entry_text": " ", "parts_replaced": True, "part_ids": []},
        {"entry_text": TEXT, "part_ids": "part_required"},
    ),
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_an_invalid_resolution_reports_every_failing_field_and_changes_nothing(
    client: TestClient, session: Session, headers, make_ticket, make_part, case: str
) -> None:
    build, expected_fields = INVALID[case]
    parts = {"active": make_part("Gasket"), "inactive": make_part("Valve", active=False)}
    ticket = make_ticket("in_progress")
    _record(session, ticket, make_part("Recorded"))  # an earlier set and note that must survive
    before = _state(session, ticket.ticket_number)

    response = _close(client, headers, ticket, _resolution(**build(parts)))

    assert response.status_code == 422
    assert _fields(response) == expected_fields
    assert list(_fields(response)) == list(expected_fields)  # form order: text, name, then the parts
    assert _code(response) == next(iter(expected_fields.values()))
    assert _state(session, ticket.ticket_number) == before


# --------------------------------------------------------------------------------- check order


def test_the_lifecycle_checks_come_before_the_resolution_checks(
    client: TestClient, session: Session, headers, make_ticket
) -> None:
    bad = _resolution(entry_text="", technician_name="", parts_replaced="yes")
    stale = make_ticket("received")
    not_tabled = make_ticket("received")
    already = make_ticket("closed")
    before = [_state(session, t.ticket_number) for t in (stale, not_tabled, already)]

    # The ticket is no longer in the status the caller saw.
    changed = _close(client, headers, stale, bad, expected="in_progress")
    # The caller saw the right status, but received to closed is not a transition.
    invalid = _close(client, headers, not_tabled, bad, expected="received")
    same = _close(client, headers, already, bad, expected="closed")
    missing = client.post(
        f"{PREFIX}/tickets/99999/status",
        json={"expected_status": "in_progress", "target_status": "closed", "resolution": bad},
        headers=headers,
    )
    no_resolution = _close(client, headers, not_tabled, None, expected="received")

    assert (changed.status_code, _code(changed)) == (409, "ticket_status_changed")
    assert (invalid.status_code, _code(invalid)) == (409, "invalid_transition")
    assert (same.status_code, _code(same)) == (409, "invalid_transition")
    assert (missing.status_code, _code(missing)) == (404, "ticket_not_found")
    assert (no_resolution.status_code, _code(no_resolution)) == (409, "invalid_transition")
    assert [_state(session, t.ticket_number) for t in (stale, not_tabled, already)] == before


def test_a_missing_resolution_comes_before_the_checks_of_its_fields(
    client: TestClient, headers, make_ticket
) -> None:
    ticket = make_ticket("in_progress")

    response = _close(client, headers, ticket, None)

    assert (response.status_code, _code(response)) == (422, "resolution_required")
    assert "fields" not in response.json()["detail"]["error"]


# --------------------------------------------------------------------------------- races and atomicity


def _before_statement(session: Session, matches: Callable[[str], bool], action: Callable[[Any], None]):
    """Run `action(connection)` just before the first statement that `matches`; return the remover."""
    engine = session.get_bind()
    fired: list[str] = []

    def hook(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if not fired and matches(statement.lstrip().upper()):
            fired.append(statement)
            action(conn)

    event.listen(engine, "before_cursor_execute", hook)

    def remove() -> list[str]:
        event.remove(engine, "before_cursor_execute", hook)
        return fired

    return remove


def test_a_concurrent_status_change_leaves_the_history_and_the_parts_untouched(
    client: TestClient, session: Session, headers, make_ticket, make_part
) -> None:
    recorded, wanted = make_part("Recorded"), make_part("Gasket")
    ticket = make_ticket("in_progress")
    _record(session, ticket, recorded)
    number = ticket.ticket_number

    def interloper(conn: Any) -> None:  # the customer cancels between the read and the guarded UPDATE
        conn.exec_driver_sql(
            "UPDATE svc_tickets SET status = ? WHERE ticket_number = ?", ("cancelled", number)
        )

    executed: list[str] = []
    engine = session.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        executed.append(statement.lstrip().upper())

    event.listen(engine, "before_cursor_execute", record)
    remove = _before_statement(session, lambda sql: sql.startswith("UPDATE SVC_TICKETS"), interloper)
    try:
        response = _close(client, headers, ticket, _with_parts(wanted))
    finally:
        fired = remove()
        event.remove(engine, "before_cursor_execute", record)

    assert fired, "the interloper never ran, so the guarded UPDATE was not exercised"
    assert (response.status_code, _code(response)) == (409, "ticket_status_changed")
    # The interloper shares the request's connection, so the rollback of the refused request takes its
    # change back too: the end state cannot tell a leaked write from a refused one. The statements can.
    written = ("INSERT INTO SVC_HISTORY", "INSERT INTO SVC_TICKET_PARTS", "DELETE FROM SVC_TICKET_PARTS")
    writes = [sql for sql in executed if sql.startswith(written)]
    assert writes == [], "a history entry or the parts were written although the status change lost"
    status, _, _, entries, parts = _state(session, number)
    assert status == "in_progress"
    assert [entry[0] for entry in entries] == ["Earlier note"]  # no resolution entry
    assert parts == {recorded.id}  # the earlier set is not replaced


@pytest.mark.parametrize("failing_table", ["svc_history_entries", "svc_ticket_parts"])
def test_a_failure_after_the_status_change_stores_none_of_it(
    client: TestClient, session: Session, headers, make_ticket, make_part, failing_table: str
) -> None:
    recorded, wanted = make_part("Recorded"), make_part("Gasket")
    ticket = make_ticket("in_progress")
    _record(session, ticket, recorded)
    before = _state(session, ticket.ticket_number)

    def fail(_conn: Any) -> None:
        raise RuntimeError("forced failure")

    insert = f"INSERT INTO {failing_table.upper()}"
    remove = _before_statement(session, lambda sql: sql.startswith(insert), fail)
    try:
        response = _close(client, headers, ticket, _with_parts(wanted))
    finally:
        fired = remove()

    assert fired, "the forced failure never ran"
    assert response.status_code >= 500
    # The status UPDATE (and, for the parts insert, the history entry and the delete) ran in the same
    # transaction and were rolled back with it.
    assert _state(session, ticket.ticket_number) == before


# --------------------------------------------------------------------------------- who may resolve


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.TECHNICIAN])
def test_every_ticket_staff_role_may_resolve(
    client: TestClient, staff, make_ticket, role: UserRole
) -> None:
    _, role_headers = staff(role)
    ticket = make_ticket("in_progress")

    response = _close(client, role_headers, ticket, _resolution())

    assert (response.status_code, response.json()["status"]) == (200, "closed")
