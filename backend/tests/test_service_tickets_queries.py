"""Example tests for the ticket list and detail queries (Requirements 12.1, 12.2, 12.3, 12.9, 15.1 to 15.3,
15.5, 15.6, 15.8, 16.1, 16.9).

The queries run on the SQLite test database; `count(*) OVER ()` and `CASE` behave the same there as on
PostgreSQL. The PostgreSQL run is `tests/integration/test_service_queries_pg.py`.
"""

from __future__ import annotations

import dataclasses
import itertools
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.service_tickets.grammars import InvalidDateRange, InvalidFilter, InvalidPage
from app.service_tickets.models import Category, Customer, HistoryEntry, Ticket
from app.service_tickets.services.ticket_queries import (
    TicketNotFound,
    get_customer_ticket,
    get_staff_ticket,
    list_customer_tickets,
    list_staff_tickets,
)

BASE_TIME = datetime(2025, 3, 10, 9, 0, tzinfo=UTC)

_numbers = itertools.count(10000)
_seq = itertools.count(1)
_phones = itertools.count(1)


# --------------------------------------------------------------------------------- fixtures


@pytest.fixture
def make_customer(session: Session):
    def _make(name: str = "Dana Levi") -> Customer:
        row = Customer(
            phone=f"05{next(_phones):08d}", full_name=name, full_name_key=name.casefold(), password_hash="x"
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
    def _make(name: str, *, active: bool = True) -> Category:
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
    return make_category("Electric")


@pytest.fixture
def make_ticket(session: Session, customer: Customer, category: Category):
    def _make(
        status: str = "received",
        *,
        owner: Customer | None = None,
        urgent: bool = False,
        city: str = "Haifa",
        created_date: date = date(2025, 3, 10),
        created_at: datetime = BASE_TIME,
        in_category: Category | None = None,
        number: int | None = None,
        description: str = "leaking tap",
        photo_key: str | None = None,
    ) -> Ticket:
        ticket = Ticket(
            ticket_number=number if number is not None else next(_numbers),
            customer_id=(owner or customer).id,
            category_id=(in_category or category).id,
            city=city,
            city_key=city.strip().casefold(),
            apartment="12",
            phone="0501234567",
            description=description,
            photo_key=photo_key,
            urgent=urgent,
            status=status,
            assigned_technician_name="",
            created_at=created_at,
            created_date=created_date,
            updated_at=BASE_TIME,
        )
        session.add(ticket)
        session.commit()
        return ticket

    return _make


def _numbers_of(page) -> list[int]:
    return [row.ticket_number for row in page.items]


# --------------------------------------------------------------------------------- customer list


def test_customer_list_shows_only_own_tickets(session, customer, make_customer, make_ticket) -> None:
    other = make_customer("Other")
    mine = make_ticket()
    make_ticket(owner=other)

    page = list_customer_tickets(session, customer.id)

    assert _numbers_of(page) == [mine.ticket_number]
    assert (page.page, page.page_size, page.total) == (1, 50, 1)


def test_customer_list_orders_newest_first_with_number_descending_on_equal_times(
    session, customer, make_ticket
) -> None:
    older = make_ticket(created_at=BASE_TIME - timedelta(hours=1))
    tie_a = make_ticket(created_at=BASE_TIME)
    tie_b = make_ticket(created_at=BASE_TIME)
    newest = make_ticket(created_at=BASE_TIME + timedelta(hours=1))

    page = list_customer_tickets(session, customer.id)

    assert _numbers_of(page) == [
        newest.ticket_number,
        tie_b.ticket_number,
        tie_a.ticket_number,
        older.ticket_number,
    ]


def test_customer_list_includes_every_status_and_the_row_has_exactly_five_values(
    session, customer, category, make_ticket
) -> None:
    for status in ("received", "in_progress", "closed", "cancelled"):
        make_ticket(status, urgent=status == "closed")

    page = list_customer_tickets(session, customer.id)

    assert sorted(row.status for row in page.items) == ["cancelled", "closed", "in_progress", "received"]
    assert {field.name for field in dataclasses.fields(page.items[0])} == {
        "ticket_number",
        "category",
        "created_date",
        "status",
        "urgent",
    }
    assert page.items[0].category.id == category.id


def test_customer_list_pages_of_fifty_and_a_page_past_the_end(session, customer, make_ticket) -> None:
    for index in range(51):
        make_ticket(created_at=BASE_TIME + timedelta(minutes=index))

    first = list_customer_tickets(session, customer.id, page=1)
    second = list_customer_tickets(session, customer.id, page=2)
    beyond = list_customer_tickets(session, customer.id, page=3)

    assert (len(first.items), first.total) == (50, 51)
    assert (len(second.items), second.total) == (1, 51)
    assert (beyond.items, beyond.total, beyond.page) == ((), 51, 3)
    assert not set(_numbers_of(first)) & set(_numbers_of(second))


def test_customer_with_no_tickets_gets_an_empty_page_with_total_zero(session, customer) -> None:
    page = list_customer_tickets(session, customer.id)

    assert (page.items, page.total) == ((), 0)
    assert list_customer_tickets(session, customer.id, page=4).total == 0


@pytest.mark.parametrize("bad", [0, -1, True, "1", 1.0, None])
def test_customer_list_rejects_a_page_that_is_not_a_positive_integer(session, customer, bad) -> None:
    with pytest.raises(InvalidPage):
        list_customer_tickets(session, customer.id, page=bad)


# --------------------------------------------------------------------------------- customer detail


def test_customer_detail_returns_the_values_stored_on_the_ticket(session, customer, make_ticket) -> None:
    ticket = make_ticket(urgent=True, city="Tel Aviv", description="a\nb", photo_key="p/1.jpg")

    detail = get_customer_ticket(session, customer.id, ticket.ticket_number)

    assert (detail.ticket_number, detail.city, detail.apartment, detail.phone) == (
        ticket.ticket_number,
        "Tel Aviv",
        "12",
        "0501234567",
    )
    assert (detail.description, detail.has_photo, detail.urgent, detail.status) == (
        "a\nb",
        True,
        True,
        "received",
    )
    assert detail.created_date == date(2025, 3, 10)
    assert {field.name for field in dataclasses.fields(detail)} == {
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


def test_customer_detail_has_photo_is_false_without_a_photo(session, customer, make_ticket) -> None:
    ticket = make_ticket()

    assert get_customer_ticket(session, customer.id, ticket.ticket_number).has_photo is False


def test_not_owned_missing_and_malformed_tickets_give_the_same_error(
    session, customer, make_customer, make_ticket
) -> None:
    other = make_customer("Other")
    theirs = make_ticket(owner=other)
    missing = theirs.ticket_number + 1000

    outcomes = []
    for value in (
        theirs.ticket_number,
        missing,
        "abcde",
        "1234",
        "100000",
        "٢٣٤٥٦",
        "10000 ",
        None,
        True,
        5,
        2**70,
    ):
        with pytest.raises(TicketNotFound) as caught:
            get_customer_ticket(session, customer.id, value)
        outcomes.append((type(caught.value), caught.value.code, str(caught.value)))

    assert len(set(outcomes)) == 1
    assert outcomes[0][1] == "ticket_not_found"


def test_customer_detail_accepts_the_raw_path_text(session, customer, make_ticket) -> None:
    ticket = make_ticket()

    assert (
        get_customer_ticket(session, customer.id, str(ticket.ticket_number)).ticket_number
        == ticket.ticket_number
    )


# --------------------------------------------------------------------------------- staff list


def test_staff_list_default_order_and_row_values(session, customer, category, make_ticket) -> None:
    older = make_ticket(created_at=BASE_TIME - timedelta(days=1))
    tie_a = make_ticket(created_at=BASE_TIME)
    tie_b = make_ticket(created_at=BASE_TIME)

    page = list_staff_tickets(session)

    assert _numbers_of(page) == [tie_b.ticket_number, tie_a.ticket_number, older.ticket_number]
    row = page.items[0]
    assert (row.customer_name, row.city, row.apartment, row.status) == (
        "Dana Levi",
        "Haifa",
        "12",
        "received",
    )
    assert (row.assigned_technician_name, row.urgent, row.category.id) == ("", False, category.id)
    assert (page.page, page.page_size, page.total) == (1, 25, 3)


def test_staff_list_covers_every_customer_status_and_inactive_category(
    session, make_customer, make_category, make_ticket
) -> None:
    retired = make_category("Retired", active=False)
    other = make_customer("Other")
    make_ticket("cancelled")
    make_ticket("closed", owner=other, in_category=retired)

    page = list_staff_tickets(session)

    assert page.total == 2
    assert {row.customer_name for row in page.items} == {"Dana Levi", "Other"}
    assert retired.id in {row.category.id for row in page.items}


def test_staff_list_pages_of_twenty_five_and_a_page_past_the_end_has_the_right_total(
    session, make_ticket
) -> None:
    for index in range(26):
        make_ticket(created_at=BASE_TIME + timedelta(minutes=index))

    first = list_staff_tickets(session, page=1)
    second = list_staff_tickets(session, page=2)
    beyond = list_staff_tickets(session, page=3)

    assert (len(first.items), first.total) == (25, 26)
    assert (len(second.items), second.total) == (1, 26)
    assert (beyond.items, beyond.total) == ((), 26)
    assert list_staff_tickets(session, page=1_000_000_000).total == 26


def test_staff_list_past_the_end_with_filters_counts_only_matches(session, make_ticket) -> None:
    for _ in range(3):
        make_ticket(urgent=True)
    make_ticket(urgent=False)

    page = list_staff_tickets(session, urgent_only=True, page=2)

    assert (page.items, page.total) == ((), 3)


def test_staff_list_empty_result_has_total_zero(session) -> None:
    page = list_staff_tickets(session)

    assert (page.items, page.total) == ((), 0)


def test_search_returns_only_that_ticket_and_overrides_every_filter(
    session, make_ticket, make_category
) -> None:
    other_category = make_category("Plumbing")
    target = make_ticket("closed", city="Haifa", created_date=date(2024, 1, 1))
    make_ticket("received", in_category=other_category, urgent=True)

    page = list_staff_tickets(
        session,
        search=target.ticket_number,
        statuses=("received",),
        category_id=other_category.id,
        city="Elsewhere",
        urgent_only=True,
        date_from=date(2025, 1, 1),
        date_to=date(2025, 12, 31),
    )

    assert _numbers_of(page) == [target.ticket_number]
    assert page.total == 1


def test_search_for_an_unused_number_is_an_empty_list_with_total_zero(session, make_ticket) -> None:
    make_ticket()

    page = list_staff_tickets(session, search=99999)

    assert (page.items, page.total) == ((), 0)


def test_search_still_rejects_an_impossible_filter(session, make_ticket) -> None:
    ticket = make_ticket()

    with pytest.raises(InvalidDateRange):
        list_staff_tickets(
            session, search=ticket.ticket_number, date_from=date(2025, 2, 2), date_to=date(2025, 1, 1)
        )
    with pytest.raises(InvalidFilter):
        list_staff_tickets(session, search=ticket.ticket_number, category_id=uuid.uuid4())


def test_status_filter_takes_a_set(session, make_ticket) -> None:
    received = make_ticket("received")
    progress = make_ticket("in_progress")
    make_ticket("closed")
    make_ticket("cancelled")

    page = list_staff_tickets(session, statuses=("received", "in_progress", "received"))

    assert set(_numbers_of(page)) == {received.ticket_number, progress.ticket_number}
    assert page.total == 2


def test_unknown_status_is_an_invalid_filter(session) -> None:
    with pytest.raises(InvalidFilter):
        list_staff_tickets(session, statuses=("open",))


def test_category_filter_includes_inactive_categories_and_unknown_category_is_invalid(
    session, make_category, make_ticket
) -> None:
    retired = make_category("Retired", active=False)
    make_ticket()
    expected = make_ticket(in_category=retired)

    assert _numbers_of(list_staff_tickets(session, category_id=retired.id)) == [expected.ticket_number]
    with pytest.raises(InvalidFilter):
        list_staff_tickets(session, category_id=uuid.uuid4())


@pytest.mark.parametrize("typed", ["haifa", "  HAIFA  ", "Haifa", "\tHaIfA\n"])
def test_city_filter_is_case_insensitive_and_trimmed(session, make_ticket, typed) -> None:
    match = make_ticket(city="  Haifa ")
    make_ticket(city="Haifa North")
    make_ticket(city="Tel Aviv")

    page = list_staff_tickets(session, city=typed)

    assert _numbers_of(page) == [match.ticket_number]


def test_blank_city_is_no_filter(session, make_ticket) -> None:
    make_ticket(city="Haifa")
    make_ticket(city="Tel Aviv")

    assert list_staff_tickets(session, city="   ").total == 2


def test_urgent_only_filter(session, make_ticket) -> None:
    urgent = make_ticket(urgent=True)
    closed_urgent = make_ticket("closed", urgent=True)
    make_ticket(urgent=False)

    page = list_staff_tickets(session, urgent_only=True)

    assert set(_numbers_of(page)) == {urgent.ticket_number, closed_urgent.ticket_number}


def test_date_range_is_inclusive_and_either_bound_may_be_absent(session, make_ticket) -> None:
    days = {day: make_ticket(created_date=date(2025, 3, day)) for day in (9, 10, 11, 12)}

    both = list_staff_tickets(session, date_from=date(2025, 3, 10), date_to=date(2025, 3, 11))
    only_from = list_staff_tickets(session, date_from=date(2025, 3, 11))
    only_to = list_staff_tickets(session, date_to=date(2025, 3, 9))
    one_day = list_staff_tickets(session, date_from=date(2025, 3, 12), date_to=date(2025, 3, 12))

    assert set(_numbers_of(both)) == {days[10].ticket_number, days[11].ticket_number}
    assert set(_numbers_of(only_from)) == {days[11].ticket_number, days[12].ticket_number}
    assert _numbers_of(only_to) == [days[9].ticket_number]
    assert _numbers_of(one_day) == [days[12].ticket_number]


def test_a_range_that_starts_after_it_ends_is_invalid(session) -> None:
    with pytest.raises(InvalidDateRange):
        list_staff_tickets(session, date_from=date(2025, 3, 11), date_to=date(2025, 3, 10))


def test_filters_combine_with_and(session, make_ticket) -> None:
    match = make_ticket("in_progress", urgent=True, city="Haifa", created_date=date(2025, 3, 10))
    make_ticket("received", urgent=True, city="Haifa", created_date=date(2025, 3, 10))
    make_ticket("in_progress", urgent=False, city="Haifa", created_date=date(2025, 3, 10))
    make_ticket("in_progress", urgent=True, city="Acre", created_date=date(2025, 3, 10))
    make_ticket("in_progress", urgent=True, city="Haifa", created_date=date(2025, 3, 11))

    page = list_staff_tickets(
        session,
        statuses=("in_progress",),
        urgent_only=True,
        city="haifa",
        date_from=date(2025, 3, 10),
        date_to=date(2025, 3, 10),
    )

    assert _numbers_of(page) == [match.ticket_number]


def test_urgent_first_orders_across_pages_and_leaves_the_total_alone(session, make_ticket) -> None:
    created = [make_ticket(created_at=BASE_TIME + timedelta(minutes=index)) for index in range(29)]
    urgent_open = [make_ticket("in_progress", urgent=True, created_at=BASE_TIME - timedelta(days=1))]
    urgent_open += [make_ticket("received", urgent=True, created_at=BASE_TIME - timedelta(days=2))]
    closed_urgent = make_ticket("closed", urgent=True, created_at=BASE_TIME + timedelta(days=1))
    total = len(created) + len(urgent_open) + 1

    plain = list_staff_tickets(session, page=1)
    first = list_staff_tickets(session, urgent_first=True, page=1)
    second = list_staff_tickets(session, urgent_first=True, page=2)

    assert plain.total == first.total == second.total == total
    assert _numbers_of(first)[:2] == [urgent_open[0].ticket_number, urgent_open[1].ticket_number]
    assert _numbers_of(plain)[0] == closed_urgent.ticket_number
    # A closed urgent ticket is not an open one: it sorts with the others by creation time.
    assert _numbers_of(first)[2] == closed_urgent.ticket_number
    rest = _numbers_of(first)[2:] + _numbers_of(second)
    assert rest == [
        row.ticket_number
        for row in sorted(
            [closed_urgent, *created], key=lambda t: (t.created_at, t.ticket_number), reverse=True
        )
    ]


def test_urgent_first_combines_with_filters(session, make_ticket) -> None:
    make_ticket("received", urgent=False, city="Haifa")
    urgent = make_ticket("received", urgent=True, city="Haifa", created_at=BASE_TIME - timedelta(days=1))
    make_ticket("received", urgent=True, city="Acre")

    page = list_staff_tickets(session, city="Haifa", urgent_first=True)

    assert page.total == 2
    assert _numbers_of(page)[0] == urgent.ticket_number


@pytest.mark.parametrize("bad", [0, -3, True, "2", None])
def test_staff_list_rejects_a_page_that_is_not_a_positive_integer(session, bad) -> None:
    with pytest.raises(InvalidPage):
        list_staff_tickets(session, page=bad)


# --------------------------------------------------------------------------------- staff detail


def test_staff_detail_uses_the_customers_current_name_and_orders_history_oldest_first(
    session, customer, category, make_ticket
) -> None:
    ticket = make_ticket(photo_key="p/1.jpg", description="<b>x</b>")
    for text, offset in (("third", 3), ("first", 1), ("second", 2)):
        session.add(
            HistoryEntry(
                ticket_id=ticket.id,
                entry_text=text,
                technician_name="Tech",
                created_at=BASE_TIME + timedelta(minutes=offset),
            )
        )
    session.commit()
    customer.full_name = "Dana Cohen"
    session.commit()

    detail = get_staff_ticket(session, ticket.ticket_number)

    assert detail.customer_name == "Dana Cohen"
    assert [entry.entry_text for entry in detail.history] == ["first", "second", "third"]
    assert (detail.phone, detail.description, detail.has_photo) == ("0501234567", "<b>x</b>", True)
    assert (detail.category.id, detail.updated_at.replace(tzinfo=UTC)) == (category.id, BASE_TIME)


def test_staff_detail_without_history_has_an_empty_tuple(session, make_ticket) -> None:
    ticket = make_ticket()

    detail = get_staff_ticket(session, ticket.ticket_number)

    assert detail.history == ()
    assert detail.has_photo is False


def test_staff_detail_sees_a_cancelled_ticket_of_any_customer(session, make_customer, make_ticket) -> None:
    ticket = make_ticket("cancelled", owner=make_customer("Other"))

    detail = get_staff_ticket(session, ticket.ticket_number)

    assert (detail.status, detail.customer_name) == ("cancelled", "Other")


def test_staff_detail_missing_and_malformed_numbers_give_the_same_error(session, make_ticket) -> None:
    make_ticket()

    outcomes = set()
    for value in (99999, "99999", "abcde", "1234", None, True, 2**70):
        with pytest.raises(TicketNotFound) as caught:
            get_staff_ticket(session, value)
        outcomes.add((caught.value.code, str(caught.value)))

    assert outcomes == {("ticket_not_found", "ticket_not_found")}
