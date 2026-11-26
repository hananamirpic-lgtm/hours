"""Example tests for the dashboard and report services (Requirements 10.9, 17.1 to 17.3, 18.1 to 18.9).

Both services run on the SQLite test database. `FILTER (WHERE ...)` and `UNION ALL` behave the same
there as on PostgreSQL. SQLite has no sequences, so the numbers warning reads a one-row table named like
the sequence relation, as `tests/test_service_numbering.py` does.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.service_tickets.grammars import InvalidDateRange, InvalidFilter, InvalidPage, parse_date_range
from app.service_tickets.models import Category, Customer, Ticket
from app.service_tickets.services.dashboard import get_dashboard
from app.service_tickets.services.reports import get_report

STATUSES = ("received", "in_progress", "closed", "cancelled")
BASE_TIME = datetime(2025, 3, 10, 9, 0, tzinfo=UTC)

_numbers = itertools.count(10000)
_seq = itertools.count(1)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------------- fixtures


@pytest.fixture
def customer(session: Session) -> Customer:
    row = Customer(phone="0501234567", full_name="Dana Levi", full_name_key="dana levi", password_hash="x")
    session.add(row)
    session.commit()
    return row


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
        urgent: bool = False,
        city: str = "Haifa",
        created_date: date = date(2025, 3, 10),
        created_at: datetime | None = None,
        in_category: Category | None = None,
        name: str = "",
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
            created_at=created_at or BASE_TIME,
            created_date=created_date,
            updated_at=BASE_TIME,
        )
        session.add(ticket)
        session.commit()
        return ticket

    return _make


@pytest.fixture
def sequence(session: Session):
    """The stand-in for `svc_ticket_number_seq`: `set_last(n)` means number n was the last one handed out."""
    session.execute(text("CREATE TABLE svc_ticket_number_seq (last_value INTEGER, is_called BOOLEAN)"))
    session.execute(text("INSERT INTO svc_ticket_number_seq VALUES (10000, 0)"))
    session.commit()

    def set_last(number: int) -> None:
        session.execute(
            text("UPDATE svc_ticket_number_seq SET last_value = :n, is_called = 1"), {"n": number}
        )
        session.commit()

    return set_last


# --------------------------------------------------------------------------------- dashboard


def test_dashboard_with_no_tickets_is_all_zero(session: Session) -> None:
    result = get_dashboard(session)
    assert (result.open, result.in_progress, result.urgent) == (0, 0, 0)
    assert result.ticket_numbers_remaining is None


def test_dashboard_counts_each_status_and_urgent_flag(session: Session, make_ticket) -> None:
    make_ticket("received")
    make_ticket("received", urgent=True)
    make_ticket("in_progress")
    make_ticket("in_progress", urgent=True)
    make_ticket("in_progress", urgent=True)

    result = get_dashboard(session)

    assert result.open == 5
    assert result.in_progress == 3
    assert result.urgent == 3


def test_dashboard_excludes_closed_and_cancelled_from_all_three_counts(session: Session, make_ticket) -> None:
    make_ticket("closed", urgent=True)
    make_ticket("cancelled", urgent=True)
    make_ticket("closed")
    make_ticket("cancelled")

    result = get_dashboard(session)

    assert (result.open, result.in_progress, result.urgent) == (0, 0, 0)


def test_dashboard_open_is_received_plus_in_progress_and_bounds_others(session: Session, make_ticket) -> None:
    for status in ("received", "received", "in_progress", "closed", "cancelled", "in_progress"):
        make_ticket(status, urgent=status != "closed")

    result = get_dashboard(session)
    received = 2

    assert result.open == received + result.in_progress
    assert result.open >= result.in_progress
    assert result.open >= result.urgent


def test_dashboard_runs_one_statement_for_the_counts(session: Session, make_ticket) -> None:
    from sqlalchemy import event

    make_ticket("received")
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_rest) -> None:
        statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        get_dashboard(session)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(statements) == 1
    assert statements[0].upper().count("FILTER") == 3


@pytest.mark.parametrize(
    ("last_taken", "warning"),
    [
        (98999, 1000),  # 1000 remain: not below the threshold
        (99000, 999),
        (99999, 0),
    ],
)
def test_dashboard_numbers_warning_only_below_one_thousand(
    session: Session, sequence, last_taken: int, warning: int
) -> None:
    sequence(last_taken)

    result = get_dashboard(session, config_staff=True)

    assert result.ticket_numbers_remaining == (None if warning >= 1000 else warning)


def test_dashboard_numbers_warning_is_never_given_to_other_staff(session: Session, sequence) -> None:
    sequence(99500)

    assert get_dashboard(session).ticket_numbers_remaining is None
    assert get_dashboard(session, config_staff=False).ticket_numbers_remaining is None


def test_dashboard_for_other_staff_does_not_read_the_sequence(session: Session) -> None:
    """No sequence table exists in this test, so reading it would raise."""
    assert get_dashboard(session, config_staff=False).open == 0


# --------------------------------------------------------------------------------- reports


def test_report_with_no_tickets_is_all_zero_with_all_four_statuses(session: Session) -> None:
    report = get_report(session)

    assert report.total == 0
    assert report.open == 0
    assert report.by_status == {"received": 0, "in_progress": 0, "closed": 0, "cancelled": 0}
    assert report.by_category == ()
    assert report.tickets == ()


def test_report_with_all_filters_empty_covers_every_ticket_and_status(session: Session, make_ticket) -> None:
    for status in STATUSES:
        make_ticket(status)
    make_ticket("cancelled")

    report = get_report(session, date_from=None, date_to=None, city=None, category_id=None)

    assert report.total == 5
    assert report.by_status == {"received": 1, "in_progress": 1, "closed": 1, "cancelled": 2}
    assert report.open == 2
    assert len(report.tickets) == 5


def test_report_empty_and_blank_city_is_no_filter(session: Session, make_ticket) -> None:
    make_ticket(city="Haifa")
    make_ticket(city="Tel Aviv")

    assert get_report(session, city="").total == 2
    assert get_report(session, city="   ").total == 2


def test_report_counts_sum_to_total_and_open_matches(session: Session, make_ticket, make_category) -> None:
    other = make_category("Plumbing")
    make_ticket("received")
    make_ticket("in_progress", in_category=other)
    make_ticket("closed", in_category=other)
    make_ticket("cancelled")
    make_ticket("received", in_category=other)

    report = get_report(session)

    assert sum(report.by_status.values()) == report.total == 5
    assert sum(item.count for item in report.by_category) == report.total
    assert report.open == report.by_status["received"] + report.by_status["in_progress"] == 3


def test_report_single_day_range_selects_that_day_only(session: Session, make_ticket) -> None:
    make_ticket(created_date=date(2025, 3, 9))
    wanted = make_ticket(created_date=date(2025, 3, 10))
    make_ticket(created_date=date(2025, 3, 11))

    report = get_report(session, date_from=date(2025, 3, 10), date_to=date(2025, 3, 10))

    assert report.total == 1
    assert [row.ticket_number for row in report.tickets] == [wanted.ticket_number]


def test_report_one_sided_date_ranges_are_inclusive(session: Session, make_ticket) -> None:
    for day in (9, 10, 11):
        make_ticket(created_date=date(2025, 3, day))

    assert get_report(session, date_from=date(2025, 3, 10)).total == 2
    assert get_report(session, date_to=date(2025, 3, 10)).total == 2


def test_report_from_after_to_is_invalid_date_range(session: Session) -> None:
    with pytest.raises(InvalidDateRange) as raised:
        get_report(session, date_from=date(2025, 3, 11), date_to=date(2025, 3, 10))
    assert raised.value.code == "invalid_date_range"


def test_report_filter_dates_use_the_stored_jerusalem_date(session: Session, make_ticket) -> None:
    """00:30 on 11 March in Jerusalem is 22:30 UTC on the 10th; the stored date wins, not the UTC date."""
    ticket = make_ticket(created_at=datetime(2025, 3, 10, 22, 30, tzinfo=UTC), created_date=date(2025, 3, 11))

    on_eleventh = get_report(session, date_from=date(2025, 3, 11), date_to=date(2025, 3, 11))
    on_tenth = get_report(session, date_from=date(2025, 3, 10), date_to=date(2025, 3, 10))

    assert [row.ticket_number for row in on_eleventh.tickets] == [ticket.ticket_number]
    assert on_tenth.total == 0


def test_report_unparsed_dates_go_through_the_grammar_parser(session: Session) -> None:
    start, end = parse_date_range("2025-03-01", "2025-03-31")
    assert get_report(session, date_from=start, date_to=end).total == 0
    with pytest.raises(InvalidDateRange):
        parse_date_range("2025-02-29", None)


def test_report_city_is_trimmed_and_case_insensitive(session: Session, make_ticket) -> None:
    make_ticket(city="Haifa")
    make_ticket(city="  HAIFA ")
    make_ticket(city="Haifa Bay")

    assert get_report(session, city="haifa").total == 2
    assert get_report(session, city="  hAiFa  ").total == 2
    assert get_report(session, city="Hai").total == 0


def test_report_city_folds_non_ascii_text(session: Session, make_ticket) -> None:
    make_ticket(city="Éilat")
    make_ticket(city="Москва")

    assert get_report(session, city="éILAT").total == 1
    assert get_report(session, city="МОСКВА").total == 1


def test_report_category_filter_accepts_an_inactive_category(
    session: Session, make_ticket, make_category
) -> None:
    retired = make_category("Retired", active=False)
    make_ticket(in_category=retired)
    make_ticket()

    report = get_report(session, category_id=retired.id)

    assert report.total == 1
    assert [item.category.id for item in report.by_category] == [retired.id]
    assert report.tickets[0].category.name_en == "Retired"


def test_report_nonexistent_category_is_invalid_filter(session: Session, make_ticket) -> None:
    make_ticket()

    with pytest.raises(InvalidFilter) as raised:
        get_report(session, category_id=uuid.uuid4())
    assert raised.value.code == "invalid_filter"


def test_report_by_category_lists_only_matching_categories_oldest_first(
    session: Session, make_ticket, make_category, category: Category
) -> None:
    second = make_category("Plumbing")
    make_category("Unused")
    make_ticket(in_category=second)
    make_ticket(in_category=second)
    make_ticket()

    report = get_report(session)

    assert [(item.category.name_en, item.count) for item in report.by_category] == [
        ("Electric", 1),
        ("Plumbing", 2),
    ]


def test_report_filters_combine_with_and(session: Session, make_ticket, make_category) -> None:
    other = make_category("Plumbing")
    make_ticket(city="Haifa", created_date=date(2025, 3, 10), in_category=other)
    make_ticket(city="Haifa", created_date=date(2025, 3, 10))
    make_ticket(city="Eilat", created_date=date(2025, 3, 10), in_category=other)
    make_ticket(city="Haifa", created_date=date(2025, 4, 1), in_category=other)

    report = get_report(
        session,
        date_from=date(2025, 3, 1),
        date_to=date(2025, 3, 31),
        city="haifa",
        category_id=other.id,
    )

    assert report.total == 1
    assert sum(item.count for item in report.by_category) == 1


def test_report_table_pages_of_25_newest_first_ties_by_number(session: Session, make_ticket) -> None:
    tickets = [make_ticket(created_at=BASE_TIME + timedelta(minutes=minute // 2)) for minute in range(60)]
    expected = sorted(tickets, key=lambda t: (t.created_at, t.ticket_number), reverse=True)

    first = get_report(session, page=1)
    second = get_report(session, page=2)
    third = get_report(session, page=3)
    beyond = get_report(session, page=4)

    assert first.total == second.total == third.total == beyond.total == 60
    sizes = [len(page.tickets) for page in (first, second, third, beyond)]
    assert sizes == [25, 25, 10, 0]
    assert first.page_size == 25
    shown = [row.ticket_number for page in (first, second, third) for row in page.tickets]
    assert shown == [t.ticket_number for t in expected]
    assert beyond.by_status["received"] == 60


def test_report_row_carries_the_staff_list_fields(session: Session, make_ticket, customer: Customer) -> None:
    ticket = make_ticket("in_progress", urgent=True, name="Yossi")

    row = get_report(session).tickets[0]

    assert row.ticket_number == ticket.ticket_number
    assert _utc(row.created_at) == BASE_TIME
    assert row.customer_name == customer.full_name
    assert (row.city, row.apartment) == ("Haifa", "12")
    assert row.category.name_he == "Electric-he"
    assert row.status == "in_progress"
    assert row.assigned_technician_name == "Yossi"
    assert row.urgent is True


@pytest.mark.parametrize("page", [0, -1])
def test_report_page_below_one_is_invalid_page(session: Session, page: int) -> None:
    with pytest.raises(InvalidPage):
        get_report(session, page=page)


def test_report_does_not_write(session: Session, make_ticket) -> None:
    make_ticket("received")
    session.commit()

    get_report(session)
    get_dashboard(session)

    assert not session.new
    assert not session.dirty
