"""The report's `parts` column and `part_filter` (task 30.6; R29.7, R29.8).

The real application, with each request on its own session of the in-memory test database. Tickets are
seeded directly. Five tickets, oldest first, make the sets the filters must pick out:

    t1  closed       Alpha-cat   parts: Beta and Alpha (written in that order; reported by part seq)
    t2  closed       Beta-cat    parts: Alpha
    t3  closed       Alpha-cat   parts: Gamma, which is inactive
    t4  received     Alpha-cat   no parts
    t5  in_progress  Beta-cat    no parts, city Akko
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from service_report_support import REPORT, STAFF, Seeder, bearer, code, make_client, new_uuid, report_numbers
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models.user import UserRole
from app.service_tickets.models import Category, Part, Ticket, TicketPart


@dataclass
class World:
    seed: Seeder
    alpha_cat: Category
    beta_cat: Category
    alpha: Part
    beta: Part
    gamma: Part
    t1: Ticket
    t2: Ticket
    t3: Ticket
    t4: Ticket
    t5: Ticket

    @property
    def numbers(self) -> list[int]:
        return [t.ticket_number for t in (self.t1, self.t2, self.t3, self.t4, self.t5)]


@pytest.fixture
def client(settings, session: Session) -> TestClient:
    return make_client(settings, session)


@pytest.fixture
def headers(make_user) -> dict[str, str]:
    user = make_user(role=UserRole.TECHNICIAN, is_2fa_enabled=True, must_change_password=False)
    return bearer(user, UserRole.TECHNICIAN)


@pytest.fixture
def world(session: Session) -> World:
    seed = Seeder(session)
    alpha_cat = seed.category("Alpha-cat")
    beta_cat = seed.category("Beta-cat")
    alpha = seed.part("Alpha")
    beta = seed.part("Beta")
    gamma = seed.part("Gamma", active=False)
    t1 = seed.ticket(alpha_cat, status="closed", parts=(beta, alpha))
    t2 = seed.ticket(beta_cat, status="closed", parts=(alpha,))
    t3 = seed.ticket(alpha_cat, status="closed", parts=(gamma,))
    t4 = seed.ticket(alpha_cat, status="received")
    t5 = seed.ticket(beta_cat, status="in_progress", city="Akko")
    return World(seed, alpha_cat, beta_cat, alpha, beta, gamma, t1, t2, t3, t4, t5)


def _report(client: TestClient, headers: dict[str, str], **params: str) -> Any:
    return client.get(REPORT, params=params, headers=headers)


def _table(body: dict[str, Any]) -> list[int]:
    return [item["ticket_number"] for item in body["tickets"]["items"]]


def _check_counts_follow(body: dict[str, Any], expected: list[Ticket]) -> None:
    """The totals, the status counts, the open count and the category counts are those of `expected`."""
    statuses = dict.fromkeys(("received", "in_progress", "closed", "cancelled"), 0)
    categories: dict[str, int] = {}
    for ticket in expected:
        statuses[ticket.status] += 1
        categories[str(ticket.category_id)] = categories.get(str(ticket.category_id), 0) + 1
    assert body["total"] == len(expected)
    assert body["tickets"]["total"] == len(expected)
    assert body["by_status"] == statuses
    assert body["open"] == statuses["received"] + statuses["in_progress"]
    assert {item["category"]["id"]: item["count"] for item in body["by_category"]} == categories
    assert sum(item["count"] for item in body["by_category"]) == body["total"]


# --------------------------------------------------------------------------- the parts column


def test_each_report_row_lists_its_parts_ordered_by_part_sequence(client, headers, world: World) -> None:
    body = _report(client, headers).json()
    rows = {item["ticket_number"]: item for item in body["tickets"]["items"]}

    def ref(part: Part) -> dict[str, str]:
        return {"id": str(part.id), "name_he": part.name_he, "name_en": part.name_en}

    assert rows[world.t1.ticket_number]["parts"] == [ref(world.alpha), ref(world.beta)]
    assert rows[world.t2.ticket_number]["parts"] == [ref(world.alpha)]
    assert rows[world.t3.ticket_number]["parts"] == [ref(world.gamma)]  # an inactive part is still named
    assert rows[world.t4.ticket_number]["parts"] == []
    assert rows[world.t5.ticket_number]["parts"] == []


def test_a_part_shows_its_current_names(client, headers, world: World, session: Session) -> None:
    world.alpha.name_en = "Alpha renamed"
    session.commit()

    rows = _report(client, headers).json()["tickets"]["items"]

    assert {name for row in rows for name in (p["name_en"] for p in row["parts"])} == {
        "Alpha renamed",
        "Beta",
        "Gamma",
    }


def test_the_report_row_extends_the_staff_row_and_the_list_row_has_no_parts(
    client, headers, world: World
) -> None:
    listed = client.get(f"{STAFF}/tickets", headers=headers).json()["items"]
    reported = _report(client, headers).json()["tickets"]["items"]

    assert len(listed) == len(reported) == 5
    assert all("parts" not in row for row in listed)
    assert all(set(row) == set(listed[0]) | {"parts"} for row in reported)
    for list_row, report_row in zip(listed, reported, strict=True):
        assert {k: v for k, v in report_row.items() if k != "parts"} == list_row


def test_rows_with_a_part_carry_no_other_part_data(client, headers, world: World) -> None:
    row = _report(client, headers).json()["tickets"]["items"][-1]  # the oldest, t1

    assert row["ticket_number"] == world.t1.ticket_number
    assert all(set(part) == {"id", "name_he", "name_en"} for part in row["parts"])


def test_the_parts_of_a_page_cost_the_same_number_of_statements_for_few_and_many_tickets(
    client, headers, world: World, session: Session
) -> None:
    def count_statements() -> int:
        seen: list[str] = []

        def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
            seen.append(statement)

        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            assert _report(client, headers).status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(seen)

    few = count_statements()
    for _ in range(18):
        world.seed.ticket(world.alpha_cat, status="closed", parts=(world.alpha, world.beta))

    many = count_statements()

    assert many == few


# --------------------------------------------------------------------------- part_filter values


def _expected(world: World, *names: str) -> list[Ticket]:
    return [getattr(world, name) for name in names]


@pytest.mark.parametrize("raw", [None, "", "   ", "any", " any "])
def test_part_filter_absent_blank_or_any_restricts_nothing(client, headers, world: World, raw) -> None:
    params = {} if raw is None else {"part_filter": raw}

    body = _report(client, headers, **params).json()

    assert sorted(_table(body)) == sorted(world.numbers)
    _check_counts_follow(body, _expected(world, "t1", "t2", "t3", "t4", "t5"))


def test_part_filter_yes_keeps_tickets_with_a_part_once_each(client, headers, world: World) -> None:
    body = _report(client, headers, part_filter="yes").json()

    assert _table(body) == [world.t3.ticket_number, world.t2.ticket_number, world.t1.ticket_number]
    _check_counts_follow(body, _expected(world, "t1", "t2", "t3"))  # t1 has two parts and counts once


def test_part_filter_no_keeps_tickets_without_parts(client, headers, world: World) -> None:
    body = _report(client, headers, part_filter="no").json()

    assert _table(body) == [world.t5.ticket_number, world.t4.ticket_number]
    _check_counts_follow(body, _expected(world, "t4", "t5"))


@pytest.mark.parametrize(
    ("part", "expected"),
    [("alpha", ("t1", "t2")), ("beta", ("t1",)), ("gamma", ("t3",))],  # gamma is inactive
)
def test_part_filter_by_part_id_keeps_tickets_with_that_part(
    client, headers, world: World, part: str, expected: tuple[str, ...]
) -> None:
    body = _report(client, headers, part_filter=str(getattr(world, part).id)).json()

    assert sorted(_table(body)) == sorted(t.ticket_number for t in _expected(world, *expected))
    _check_counts_follow(body, _expected(world, *expected))


def test_part_filter_by_a_part_no_ticket_uses_is_an_empty_report(
    client, headers, world: World
) -> None:
    unused = world.seed.part("Delta")

    body = _report(client, headers, part_filter=str(unused.id)).json()

    assert body["total"] == 0 and body["open"] == 0 and body["tickets"]["items"] == []
    assert body["by_category"] == []


def test_part_filter_uuid_is_accepted_in_upper_case(client, headers, world: World) -> None:
    body = _report(client, headers, part_filter=str(world.beta.id).upper()).json()

    assert _table(body) == [world.t1.ticket_number]


@pytest.mark.parametrize(
    "raw",
    [
        new_uuid(),  # a well-formed id that names no part
        "not-a-uuid",
        "YES",  # the words are exact
        "No",
        "maybe",
        "1",
        "true",
        "yes,no",
        "yes no",
        "0" * 31,
        "'; drop table svc_tickets; --",
    ],
)
def test_part_filter_any_other_value_is_invalid_filter(client, headers, world: World, raw: str) -> None:
    response = _report(client, headers, part_filter=raw)

    assert response.status_code == 422
    assert code(response) == "invalid_filter"
    assert client.get(f"{STAFF}/tickets", headers=headers).json()["total"] == 5  # nothing was touched


# --------------------------------------------------------------------------- AND with the other filters


def test_part_filter_combines_by_and_with_category(client, headers, world: World) -> None:
    yes = _report(client, headers, part_filter="yes", category_id=str(world.alpha_cat.id)).json()
    no = _report(client, headers, part_filter="no", category_id=str(world.beta_cat.id)).json()
    one_part = str(world.alpha.id)
    part = _report(client, headers, part_filter=one_part, category_id=str(world.beta_cat.id)).json()

    assert sorted(_table(yes)) == sorted(t.ticket_number for t in _expected(world, "t1", "t3"))
    _check_counts_follow(yes, _expected(world, "t1", "t3"))
    assert _table(no) == [world.t5.ticket_number]
    _check_counts_follow(no, _expected(world, "t5"))
    assert _table(part) == [world.t2.ticket_number]
    _check_counts_follow(part, _expected(world, "t2"))


def test_part_filter_combines_by_and_with_city_and_dates(client, headers, world: World) -> None:
    by_city_no = _report(client, headers, part_filter="no", city="akko").json()
    by_city_yes = _report(client, headers, part_filter="yes", city="akko").json()
    by_date = _report(client, headers, part_filter="yes", date_from="2025-03-10", date_to="2025-03-10").json()
    nothing = _report(client, headers, part_filter="yes", date_from="2030-01-01").json()

    assert _table(by_city_no) == [world.t5.ticket_number]
    assert by_city_yes["total"] == 0
    assert sorted(_table(by_date)) == sorted(world.numbers[:3])
    assert nothing["total"] == 0 and nothing["tickets"]["items"] == []


def test_part_filter_combines_by_and_with_settlement(client, headers, world: World) -> None:
    haifa = world.seed.locality("Haifa-l")
    with_place = world.seed.ticket(world.alpha_cat, status="closed", parts=(world.alpha,), locality=haifa)
    world.seed.ticket(world.alpha_cat, status="closed", locality=haifa)

    body = _report(client, headers, part_filter="yes", locality_id=str(haifa.id)).json()

    assert _table(body) == [with_place.ticket_number]
    assert _table(_report(client, headers, part_filter="no", locality_id=str(haifa.id)).json()) == [
        world.seed.tickets[-1].ticket_number
    ]


def test_an_invalid_other_filter_still_wins_when_part_filter_is_valid(client, headers, world: World) -> None:
    response = _report(client, headers, part_filter="yes", category_id=new_uuid())

    assert response.status_code == 422 and code(response) == "invalid_filter"


def test_filtered_table_pages_over_25_rows_and_counts_follow(client, headers, world: World) -> None:
    for minute in range(30):
        world.seed.ticket(
            world.beta_cat,
            status="closed",
            parts=(world.beta,),
            created_at=datetime(2025, 6, 1, 10, minute, tzinfo=UTC),
        )

    first = _report(client, headers, part_filter="yes").json()
    second = _report(client, headers, part_filter="yes", page="2").json()

    assert first["total"] == 33  # three of the five seeded tickets plus thirty
    assert (len(first["tickets"]["items"]), len(second["tickets"]["items"])) == (25, 8)
    assert len(report_numbers(client, headers, part_filter="yes")) == 33
    assert first["by_status"]["closed"] == 33 and first["open"] == 0


# --------------------------------------------------------------------------- the filter is built once


def test_the_part_filters_are_exists_subqueries_not_joins(session: Session) -> None:
    from app.service_tickets.services import reports

    def sql_of(part_filter: Any) -> str:
        conditions = reports.matching_conditions(session, part_filter=part_filter)
        return str(select(Ticket.id).where(*conditions).compile())

    for part_filter in ("yes", "no"):
        sql = sql_of(part_filter)
        assert "EXISTS" in sql
        assert "JOIN" not in sql
    seeded = Seeder(session).part("Zeta")
    by_id = sql_of(seeded.id)
    assert "EXISTS" in by_id and "JOIN" not in by_id
    assert sql_of("any") == str(select(Ticket.id).compile())


def test_the_parts_loader_takes_a_condition_on_ticket_parts(session: Session) -> None:
    """`load_parts` is one statement for any number of tickets; no per-ticket call exists."""
    from app.service_tickets.services.reports import load_parts

    seed = Seeder(session)
    category = seed.category("Cat")
    part = seed.part("P")
    tickets = [seed.ticket(category, parts=(part,)) for _ in range(5)]

    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        seen.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        loaded = load_parts(session, TicketPart.ticket_id.in_([t.id for t in tickets]))
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(seen) == 1
    assert set(loaded) == {t.id for t in tickets}

