"""Example tests for the assigned technician name and the treatment history (Requirement 16.2 to 16.11)."""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.service_tickets import error_codes
from app.service_tickets.models import Category, Customer, HistoryEntry, Ticket
from app.service_tickets.services import history
from app.service_tickets.services.history import (
    HistoryEntryNotFound,
    HistoryValidationError,
    InvalidAssignedTechnicianName,
    TicketNotFound,
    add_entry,
    edit_entry,
    list_entries,
    set_assigned_technician_name,
)

LONG_AGO = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
T1 = datetime(2031, 6, 15, 8, 30, tzinfo=UTC)
T2 = T1 + timedelta(hours=1)
T3 = T1 + timedelta(hours=2)

_numbers = itertools.count(20000)


def _utc(value: datetime | None) -> datetime | None:
    """SQLite returns timezone-aware columns as naive values; read them as UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@pytest.fixture
def make_ticket(session: Session):
    customer = Customer(phone="0501234567", full_name="Owner", full_name_key="owner", password_hash="x")
    category = Category(seq=1, name_he="חשמל", name_en="Electric", name_he_key="חשמל", name_en_key="electric")
    session.add_all([customer, category])
    session.commit()

    def _make(status: str = "received", *, name: str = "") -> Ticket:
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


def _entry_rows(session: Session) -> list[tuple]:
    table = HistoryEntry.__table__
    return sorted(tuple(row) for row in session.execute(select(table)).all())


def _updated_at(session: Session, number: int) -> datetime:
    value = session.execute(select(Ticket.updated_at).where(Ticket.ticket_number == number)).scalar_one()
    return _utc(value)  # type: ignore[return-value]


def _assigned(session: Session, number: int) -> str:
    return session.execute(
        select(Ticket.assigned_technician_name).where(Ticket.ticket_number == number)
    ).scalar_one()


# ------------------------------------------------------------------------- error codes


def test_errors_carry_their_api_codes() -> None:
    assert HistoryEntryNotFound.code == error_codes.CODE_HISTORY_ENTRY_NOT_FOUND == "history_entry_not_found"
    assert TicketNotFound.code == "ticket_not_found"
    assert InvalidAssignedTechnicianName.code == "invalid_assigned_technician_name"
    assert history.ENTRY_TEXT_MAX == 2000


# ------------------------------------------------------------------------- assigned name


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("Dana", "Dana"),
        ("  Dana Levi \t", "Dana Levi"),
        ("", ""),
        ("   \n\t ", ""),
        ("x" * 100, "x" * 100),
        ("  " + "x" * 100 + "  ", "x" * 100),
    ],
)
def test_assigned_name_is_trimmed_and_blank_is_stored_empty(
    session: Session, make_ticket, raw: str, stored: str
) -> None:
    ticket = make_ticket(name="Old name")

    result = set_assigned_technician_name(session, ticket.ticket_number, raw, now=T1)
    session.commit()

    assert result.assigned_technician_name == stored
    assert _assigned(session, ticket.ticket_number) == stored
    assert _updated_at(session, ticket.ticket_number) == T1


def test_assigned_name_over_100_is_rejected_and_nothing_changes(session: Session, make_ticket) -> None:
    ticket = make_ticket(name="Old name")

    with pytest.raises(InvalidAssignedTechnicianName):
        set_assigned_technician_name(session, ticket.ticket_number, "x" * 101, now=T1)

    assert _assigned(session, ticket.ticket_number) == "Old name"
    assert _updated_at(session, ticket.ticket_number) == LONG_AGO


def test_assigned_name_over_100_counts_after_trimming(session: Session, make_ticket) -> None:
    ticket = make_ticket()

    with pytest.raises(InvalidAssignedTechnicianName):
        set_assigned_technician_name(session, ticket.ticket_number, " " + "x" * 101, now=T1)
    set_assigned_technician_name(session, ticket.ticket_number, " " + "x" * 100 + " ", now=T1)

    assert _assigned(session, ticket.ticket_number) == "x" * 100


def test_assigned_name_for_a_missing_ticket_is_ticket_not_found(session: Session, make_ticket) -> None:
    make_ticket()

    with pytest.raises(TicketNotFound):
        set_assigned_technician_name(session, 99999, "Dana")


def test_assigned_name_non_text_is_invalid(session: Session, make_ticket) -> None:
    ticket = make_ticket(name="Old name")

    with pytest.raises(InvalidAssignedTechnicianName):
        set_assigned_technician_name(session, ticket.ticket_number, None)

    assert _assigned(session, ticket.ticket_number) == "Old name"


# ------------------------------------------------------------------------- add_entry


@pytest.mark.parametrize(
    ("length", "ok"), [(0, False), (1, True), (2000, True), (2001, False)]
)
def test_entry_text_limits(session: Session, make_ticket, length: int, ok: bool) -> None:
    ticket = make_ticket()
    text = "t" * length

    if ok:
        entry = add_entry(session, ticket.ticket_number, text, "Dana", now=T1)
        assert entry.entry_text == text
    else:
        with pytest.raises(HistoryValidationError) as caught:
            add_entry(session, ticket.ticket_number, text, "Dana", now=T1)
        assert caught.value.fields == {"entry_text": "invalid_history_text"}
        assert caught.value.code == "invalid_history_text"
        assert _entry_rows(session) == []


@pytest.mark.parametrize(
    ("length", "ok"), [(0, False), (1, True), (100, True), (101, False)]
)
def test_entry_technician_name_limits(session: Session, make_ticket, length: int, ok: bool) -> None:
    ticket = make_ticket()
    name = "n" * length

    if ok:
        entry = add_entry(session, ticket.ticket_number, "note", name, now=T1)
        assert entry.technician_name == name
    else:
        with pytest.raises(HistoryValidationError) as caught:
            add_entry(session, ticket.ticket_number, "note", name, now=T1)
        assert caught.value.fields == {"technician_name": "invalid_technician_name"}
        assert caught.value.code == "invalid_technician_name"
        assert _entry_rows(session) == []


def test_limits_apply_after_trimming(session: Session, make_ticket) -> None:
    ticket = make_ticket()

    entry = add_entry(session, ticket.ticket_number, "  " + "t" * 2000 + "\n", " " + "n" * 100 + " ", now=T1)
    assert (entry.entry_text, entry.technician_name) == ("t" * 2000, "n" * 100)

    for text, name in [("   \n", "Dana"), ("note", "   ")]:
        with pytest.raises(HistoryValidationError):
            add_entry(session, ticket.ticket_number, text, name, now=T1)
    assert len(_entry_rows(session)) == 1


def test_both_failing_fields_are_reported_text_first(session: Session, make_ticket) -> None:
    ticket = make_ticket()

    with pytest.raises(HistoryValidationError) as caught:
        add_entry(session, ticket.ticket_number, "", "n" * 101)

    assert list(caught.value.fields.items()) == [
        ("entry_text", "invalid_history_text"),
        ("technician_name", "invalid_technician_name"),
    ]
    assert caught.value.code == "invalid_history_text"
    assert _entry_rows(session) == []


def test_non_text_values_are_invalid(session: Session, make_ticket) -> None:
    ticket = make_ticket()

    with pytest.raises(HistoryValidationError) as caught:
        add_entry(session, ticket.ticket_number, None, 5)

    assert set(caught.value.fields) == {"entry_text", "technician_name"}


def test_add_stores_trimmed_values_server_time_and_no_edit_time(session: Session, make_ticket) -> None:
    ticket = make_ticket()

    entry = add_entry(session, ticket.ticket_number, "  Replaced the washer \n", "  Dana ", now=T1)
    session.commit()

    stored = session.execute(select(HistoryEntry)).scalar_one()
    assert stored.id == entry.id
    assert stored.ticket_id == ticket.id
    assert (stored.entry_text, stored.technician_name) == ("Replaced the washer", "Dana")
    assert _utc(stored.created_at) == T1
    assert stored.edited_at is None


def test_add_without_a_clock_override_uses_the_server_clock(session: Session, make_ticket) -> None:
    ticket = make_ticket()
    before = datetime.now(UTC)

    entry = add_entry(session, ticket.ticket_number, "note", "Dana")

    assert before <= _utc(entry.created_at) <= datetime.now(UTC)  # type: ignore[operator]


def test_add_does_not_touch_the_ticket_last_modified_time(session: Session, make_ticket) -> None:
    ticket = make_ticket()

    add_entry(session, ticket.ticket_number, "note", "Dana", now=T1)
    session.commit()

    assert _updated_at(session, ticket.ticket_number) == LONG_AGO


def test_add_to_a_missing_ticket_is_ticket_not_found(session: Session, make_ticket) -> None:
    make_ticket()

    with pytest.raises(TicketNotFound):
        add_entry(session, 99999, "note", "Dana")

    assert _entry_rows(session) == []


def test_validation_is_checked_before_the_ticket(session: Session) -> None:
    with pytest.raises(HistoryValidationError):
        add_entry(session, 99999, "", "Dana")


# ------------------------------------------------------------------------- listing


def test_entries_are_listed_oldest_first_and_only_for_their_ticket(session: Session, make_ticket) -> None:
    ticket = make_ticket()
    other = make_ticket()
    add_entry(session, ticket.ticket_number, "second", "Dana", now=T2)
    add_entry(session, ticket.ticket_number, "third", "Dana", now=T3)
    add_entry(session, ticket.ticket_number, "first", "Dana", now=T1)
    add_entry(session, other.ticket_number, "elsewhere", "Dana", now=T1)
    session.commit()

    assert [e.entry_text for e in list_entries(session, ticket.ticket_number)] == ["first", "second", "third"]
    assert [e.entry_text for e in list_entries(session, other.ticket_number)] == ["elsewhere"]


def test_listing_a_missing_ticket_is_ticket_not_found(session: Session) -> None:
    with pytest.raises(TicketNotFound):
        list_entries(session, 99999)


# ------------------------------------------------------------------------- edit_entry


def test_edit_replaces_values_sets_edit_time_and_keeps_creation_time(session: Session, make_ticket) -> None:
    ticket = make_ticket()
    entry = add_entry(session, ticket.ticket_number, "old text", "Old", now=T1)
    session.commit()

    edited = edit_entry(session, ticket.ticket_number, entry.id, "  new text ", " New ", now=T2)
    session.commit()

    assert edited.id == entry.id
    assert (edited.entry_text, edited.technician_name) == ("new text", "New")
    assert _utc(edited.created_at) == T1
    assert _utc(edited.edited_at) == T2
    assert len(_entry_rows(session)) == 1  # no copy of the previous values
    assert _updated_at(session, ticket.ticket_number) == LONG_AGO


def test_last_save_wins(session: Session, make_ticket) -> None:
    ticket = make_ticket()
    entry = add_entry(session, ticket.ticket_number, "old", "Old", now=T1)

    edit_entry(session, ticket.ticket_number, entry.id, "first save", "A", now=T2)
    final = edit_entry(session, ticket.ticket_number, entry.id, "second save", "B", now=T3)

    assert (final.entry_text, final.technician_name) == ("second save", "B")
    assert _utc(final.edited_at) == T3


@pytest.mark.parametrize(
    ("text", "name", "fields"),
    [
        ("", "Dana", {"entry_text": "invalid_history_text"}),
        ("t" * 2001, "Dana", {"entry_text": "invalid_history_text"}),
        ("note", "", {"technician_name": "invalid_technician_name"}),
        ("note", "n" * 101, {"technician_name": "invalid_technician_name"}),
        (" ", " ", {"entry_text": "invalid_history_text", "technician_name": "invalid_technician_name"}),
    ],
)
def test_invalid_edit_changes_nothing(
    session: Session, make_ticket, text: str, name: str, fields: dict[str, str]
) -> None:
    ticket = make_ticket()
    entry = add_entry(session, ticket.ticket_number, "old", "Old", now=T1)
    session.commit()
    before = _entry_rows(session)

    with pytest.raises(HistoryValidationError) as caught:
        edit_entry(session, ticket.ticket_number, entry.id, text, name, now=T2)

    assert caught.value.fields == fields
    assert _entry_rows(session) == before


def test_edit_of_an_entry_of_another_ticket_is_not_found_and_changes_nothing(
    session: Session, make_ticket
) -> None:
    ticket = make_ticket()
    other = make_ticket()
    entry = add_entry(session, other.ticket_number, "old", "Old", now=T1)
    session.commit()
    before = _entry_rows(session)

    with pytest.raises(HistoryEntryNotFound):
        edit_entry(session, ticket.ticket_number, entry.id, "new", "New", now=T2)

    assert _entry_rows(session) == before


def test_edit_of_an_unknown_entry_is_not_found(session: Session, make_ticket) -> None:
    ticket = make_ticket()

    with pytest.raises(HistoryEntryNotFound):
        edit_entry(session, ticket.ticket_number, uuid.uuid4(), "new", "New")


def test_edit_on_a_missing_ticket_is_ticket_not_found(session: Session, make_ticket) -> None:
    ticket = make_ticket()
    entry = add_entry(session, ticket.ticket_number, "old", "Old", now=T1)
    session.commit()

    with pytest.raises(TicketNotFound):
        edit_entry(session, 99999, entry.id, "new", "New")


# ------------------------------------------------------------------------- every status, no audit


@pytest.mark.parametrize("status", ["received", "in_progress", "closed", "cancelled"])
def test_every_ticket_status_accepts_name_and_history_changes(
    session: Session, make_ticket, status: str
) -> None:
    ticket = make_ticket(status)

    set_assigned_technician_name(session, ticket.ticket_number, "Dana", now=T1)
    entry = add_entry(session, ticket.ticket_number, "note", "Dana", now=T1)
    edited = edit_entry(session, ticket.ticket_number, entry.id, "better note", "Dana", now=T2)
    session.commit()

    assert _assigned(session, ticket.ticket_number) == "Dana"
    assert edited.entry_text == "better note"
    stored_status = session.execute(
        select(Ticket.status).where(Ticket.ticket_number == ticket.ticket_number)
    ).scalar_one()
    assert stored_status == status


def test_no_audit_rows_are_written(session: Session, make_ticket) -> None:
    from app.db.base import Base

    audit_tables = [t for name, t in Base.metadata.tables.items() if "audit" in name or "change_log" in name]
    ticket = make_ticket()
    counts_before = [session.execute(select(func.count()).select_from(t)).scalar_one() for t in audit_tables]

    set_assigned_technician_name(session, ticket.ticket_number, "Dana", now=T1)
    entry = add_entry(session, ticket.ticket_number, "note", "Dana", now=T1)
    edit_entry(session, ticket.ticket_number, entry.id, "better", "Dana", now=T2)
    session.commit()

    counts_after = [session.execute(select(func.count()).select_from(t)).scalar_one() for t in audit_tables]
    assert counts_after == counts_before
