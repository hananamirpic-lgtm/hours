"""The ticket queries, the report and the dashboard against real PostgreSQL.

What SQLite cannot show: that `count(*) OVER ()`, the `CASE` ordering, `count(*) FILTER (WHERE ...)` and
the report's `UNION ALL` of two differently shaped selects are accepted by PostgreSQL and give the same
answers as a plain-Python reference, on a database created with `--locale=C` (city matching goes through
`city_key`, so it must not depend on the database's case folding). Skips without `TEST_DATABASE_URL`.

Requirements 12.1, 12.9, 15.1 to 15.8, 16.9, 17.1 to 17.3, 18.1 to 18.9.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.service_tickets.grammars import InvalidFilter, created_date_for, fold_key
from app.service_tickets.models import Category, Customer, HistoryEntry, Ticket
from app.service_tickets.services.dashboard import get_dashboard
from app.service_tickets.services.reports import get_report
from app.service_tickets.services.ticket_queries import (
    TicketNotFound,
    get_customer_ticket,
    get_staff_ticket,
    list_customer_tickets,
    list_staff_tickets,
)

STATUSES = ("received", "in_progress", "closed", "cancelled")
OPEN = ("received", "in_progress")
CITIES = ("Haifa", "חיפה", "Straße")
BASE_TIME = datetime(2025, 1, 5, 6, 30, tzinfo=UTC)
CUSTOMER_TICKETS = 60
OTHER_TICKETS = 4
NAME_PREFIX = "pgq-"


@dataclass(frozen=True)
class Population:
    customer_a: uuid.UUID
    customer_b: uuid.UUID
    categories: tuple[uuid.UUID, uuid.UUID]  # the second one is inactive
    tickets: tuple[dict, ...]


def _clean(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM svc_history_entries"))
        connection.execute(sa.text("DELETE FROM svc_tickets"))
        connection.execute(sa.text("DELETE FROM svc_customers"))
        connection.execute(
            sa.text("DELETE FROM svc_categories WHERE name_en LIKE :p"), {"p": f"{NAME_PREFIX}%"}
        )


@pytest.fixture
def population(migrated_engine: Engine) -> Iterator[Population]:
    """64 tickets: 60 for customer A and 4 for B, mixed statuses, cities, categories and urgency.

    Creation times come in equal pairs so the ticket number has to break ties, and the numbers are not
    in creation order. The second category is deactivated after the tickets exist.
    """
    _clean(migrated_engine)
    with Session(migrated_engine) as session:
        customers = []
        for index, name in enumerate(("Dana Levi", "Eli Cohen")):
            row = Customer(
                phone=f"050000000{index}",
                full_name=name,
                full_name_key=fold_key(name),
                password_hash="x" * 60,
            )
            session.add(row)
            customers.append(row)
        categories = []
        for name in ("Electric", "Plumbing"):
            full = f"{NAME_PREFIX}{name}"
            row = Category(
                name_he=f"{full}-he",
                name_en=full,
                name_he_key=fold_key(f"{full}-he"),
                name_en_key=fold_key(full),
            )
            session.add(row)
            categories.append(row)
        session.flush()

        tickets: list[dict] = []
        for index in range(CUSTOMER_TICKETS + OTHER_TICKETS):
            owner = customers[0] if index < CUSTOMER_TICKETS else customers[1]
            created_at = BASE_TIME + timedelta(hours=(index // 2) * 7)
            city = CITIES[index % 3]
            row = Ticket(
                ticket_number=10000 + (index * 37) % 1000,
                customer_id=owner.id,
                category_id=categories[index % 2].id,
                city=f" {city} " if index % 4 == 0 else city,
                city_key=fold_key(city),
                apartment="12",
                phone="0501234567",
                description="x",
                urgent=index % 5 == 0,
                status=STATUSES[index % 4],
                assigned_technician_name="",
                created_at=created_at,
                created_date=created_date_for(created_at, "Asia/Jerusalem"),
                updated_at=created_at,
            )
            session.add(row)
            tickets.append(
                {
                    "number": row.ticket_number,
                    "customer": owner.id,
                    "category": row.category_id,
                    "city_key": row.city_key,
                    "urgent": row.urgent,
                    "status": row.status,
                    "created_at": created_at,
                    "created_date": row.created_date,
                }
            )
        session.commit()
        built = Population(
            customer_a=customers[0].id,
            customer_b=customers[1].id,
            categories=(categories[0].id, categories[1].id),
            tickets=tuple(tickets),
        )
        session.execute(sa.update(Category).where(Category.id == categories[1].id).values(is_active=False))
        session.commit()
    try:
        yield built
    finally:
        _clean(migrated_engine)


def _newest_first(tickets) -> list[int]:
    return [t["number"] for t in sorted(tickets, key=lambda t: (t["created_at"], t["number"]), reverse=True)]


def _urgent_first(tickets) -> list[int]:
    def key(t):
        rank = 0 if t["urgent"] and t["status"] in OPEN else 1
        return (rank, -t["created_at"].timestamp(), -t["number"])

    return [t["number"] for t in sorted(tickets, key=key)]


def _numbers(page) -> list[int]:
    return [row.ticket_number for row in page.items]


def test_customer_list_pages_and_total_equal_the_reference(
    migrated_engine: Engine, population: Population
) -> None:
    mine = [t for t in population.tickets if t["customer"] == population.customer_a]
    expected = _newest_first(mine)
    with Session(migrated_engine) as session:
        first = list_customer_tickets(session, population.customer_a, page=1)
        second = list_customer_tickets(session, population.customer_a, page=2)
        beyond = list_customer_tickets(session, population.customer_a, page=3)
        other = list_customer_tickets(session, population.customer_b)

    assert _numbers(first) == expected[:50]
    assert _numbers(second) == expected[50:]
    assert (first.total, second.total, beyond.total, beyond.items) == (60, 60, 60, ())
    assert other.total == OTHER_TICKETS


def test_customer_detail_hides_other_customers_tickets(
    migrated_engine: Engine, population: Population
) -> None:
    mine = next(t for t in population.tickets if t["customer"] == population.customer_a)
    theirs = next(t for t in population.tickets if t["customer"] == population.customer_b)
    with Session(migrated_engine) as session:
        assert get_customer_ticket(session, population.customer_a, mine["number"]).status == mine["status"]
        with pytest.raises(TicketNotFound):
            get_customer_ticket(session, population.customer_a, theirs["number"])


def test_staff_list_filters_search_and_ordering_equal_the_reference(
    migrated_engine: Engine, population: Population
) -> None:
    everything = list(population.tickets)
    total = len(everything)
    with Session(migrated_engine) as session:
        default = [list_staff_tickets(session, page=n) for n in (1, 2, 3)]
        urgent_first = [list_staff_tickets(session, page=n, urgent_first=True) for n in (1, 2, 3)]
        beyond = list_staff_tickets(session, page=4)
        by_status = list_staff_tickets(session, statuses=("received", "closed"))
        by_category = list_staff_tickets(session, category_id=population.categories[1])
        by_city = list_staff_tickets(session, city="  STRASSE ")
        by_hebrew_city = list_staff_tickets(session, city="חיפה")
        urgent_only = list_staff_tickets(session, urgent_only=True)
        start, end = date(2025, 1, 8), date(2025, 1, 14)
        by_dates = list_staff_tickets(session, date_from=start, date_to=end)
        combined = list_staff_tickets(
            session, statuses=("in_progress",), urgent_only=True, city="haifa", urgent_first=True
        )
        search = list_staff_tickets(
            session, search=everything[3]["number"], statuses=("closed",), urgent_only=True
        )

    assert [len(page.items) for page in default] == [25, 25, 14]
    assert {page.total for page in default} == {total}
    assert [n for page in default for n in _numbers(page)] == _newest_first(everything)
    assert [n for page in urgent_first for n in _numbers(page)] == _urgent_first(everything)
    assert (beyond.items, beyond.total) == ((), total)

    def matches(page, wanted, order=_newest_first) -> None:
        assert _numbers(page) == order(wanted)[:25]
        assert page.total == len(wanted)

    matches(by_status, [t for t in everything if t["status"] in ("received", "closed")])
    matches(by_category, [t for t in everything if t["category"] == population.categories[1]])
    matches(by_city, [t for t in everything if t["city_key"] == "strasse"])
    matches(by_hebrew_city, [t for t in everything if t["city_key"] == "חיפה"])
    matches(urgent_only, [t for t in everything if t["urgent"]])
    matches(by_dates, [t for t in everything if start <= t["created_date"] <= end])
    matches(
        combined,
        [t for t in everything if t["status"] == "in_progress" and t["urgent"] and t["city_key"] == "haifa"],
        _urgent_first,
    )
    assert _numbers(search) == [everything[3]["number"]]
    assert search.total == 1


def test_staff_filter_by_unknown_category_is_invalid(migrated_engine: Engine, population: Population) -> None:
    with Session(migrated_engine) as session, pytest.raises(InvalidFilter):
        list_staff_tickets(session, category_id=uuid.uuid4())


def test_staff_detail_orders_history_oldest_first(migrated_engine: Engine, population: Population) -> None:
    number = population.tickets[0]["number"]
    with Session(migrated_engine) as session:
        ticket_id = session.execute(sa.select(Ticket.id).where(Ticket.ticket_number == number)).scalar_one()
        for text, minutes in (("third", 3), ("first", 1), ("second", 2)):
            session.add(
                HistoryEntry(
                    ticket_id=ticket_id,
                    entry_text=text,
                    technician_name="Tech",
                    created_at=BASE_TIME + timedelta(minutes=minutes),
                )
            )
        session.commit()
        detail = get_staff_ticket(session, number)

    assert [entry.entry_text for entry in detail.history] == ["first", "second", "third"]
    assert detail.customer_name == "Dana Levi"


def test_dashboard_counts_equal_the_reference(migrated_engine: Engine, population: Population) -> None:
    everything = population.tickets
    with Session(migrated_engine) as session:
        counts = get_dashboard(session, config_staff=True)

    assert counts.open == len([t for t in everything if t["status"] in OPEN])
    assert counts.in_progress == len([t for t in everything if t["status"] == "in_progress"])
    assert counts.urgent == len([t for t in everything if t["status"] in OPEN and t["urgent"]])
    # Nothing in this database has taken a ticket number, so the real sequence has 90,000 left.
    assert counts.ticket_numbers_remaining is None


def test_dashboard_on_an_empty_table_is_all_zero(migrated_engine: Engine) -> None:
    _clean(migrated_engine)
    with Session(migrated_engine) as session:
        counts = get_dashboard(session)

    assert (counts.open, counts.in_progress, counts.urgent, counts.ticket_numbers_remaining) == (
        0,
        0,
        0,
        None,
    )


def test_report_counts_equal_the_reference_with_and_without_filters(
    migrated_engine: Engine, population: Population
) -> None:
    everything = list(population.tickets)
    with Session(migrated_engine) as session:
        full = get_report(session)
        filtered = get_report(
            session, date_from=date(2025, 1, 8), date_to=date(2025, 1, 20), city=" HAIFA ", page=1
        )
        by_category = get_report(session, category_id=population.categories[1])
        beyond = get_report(session, page=4)
        empty = get_report(session, date_from=date(2030, 1, 1))
        with pytest.raises(InvalidFilter):
            get_report(session, category_id=uuid.uuid4())

    assert full.total == len(everything)
    assert full.by_status == {s: len([t for t in everything if t["status"] == s]) for s in STATUSES}
    assert full.open == full.by_status["received"] + full.by_status["in_progress"]
    assert [(c.category.id, c.count) for c in full.by_category] == [
        (cid, len([t for t in everything if t["category"] == cid])) for cid in population.categories
    ]
    assert sum(c.count for c in full.by_category) == full.total
    assert [row.ticket_number for row in full.tickets] == _newest_first(everything)[:25]

    wanted = [
        t
        for t in everything
        if date(2025, 1, 8) <= t["created_date"] <= date(2025, 1, 20) and t["city_key"] == "haifa"
    ]
    assert filtered.total == len(wanted)
    assert sum(filtered.by_status.values()) == sum(c.count for c in filtered.by_category) == len(wanted)
    assert [row.ticket_number for row in filtered.tickets] == _newest_first(wanted)[:25]

    inactive = [t for t in everything if t["category"] == population.categories[1]]
    assert by_category.total == len(inactive)
    assert [c.category.id for c in by_category.by_category] == [population.categories[1]]

    assert (beyond.tickets, beyond.total) == ((), len(everything))
    assert (empty.total, empty.by_category, empty.tickets) == (0, (), ())
    assert empty.by_status == dict.fromkeys(STATUSES, 0)
