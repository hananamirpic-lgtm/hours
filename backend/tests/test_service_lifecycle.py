"""Example tests for the ticket status lifecycle (Requirements 13.1, 13.4, 13.7, 13.10, 14.1 to 14.12).

`lifecycle.change_status` runs on the SQLite test database. SQLite has no row locks, so the race of
R13.8 is covered twice: here by an interloper that changes the row between the read and the guarded
`UPDATE` (the `rowcount == 0` path), and on PostgreSQL by `tests/integration/test_service_lifecycle_race.py`.

The transition matrix below is written out literally, not derived from `lifecycle.TRANSITIONS`, so a
wrong table cannot agree with itself.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.user import UserRole
from app.service_tickets import error_codes
from app.service_tickets.models import Category, Customer, HistoryEntry, Part, Ticket, TicketPart
from app.service_tickets.services import lifecycle
from app.service_tickets.services.lifecycle import (
    CustomerActor,
    InvalidAssignedTechnicianName,
    InvalidExpectedStatus,
    InvalidTargetStatus,
    InvalidTransition,
    ResolutionRequired,
    TicketNotFound,
    TicketStatusChanged,
    UnexpectedResolution,
    change_status,
)
from app.service_tickets.services.resolution import Resolution, ResolutionValidationError
from app.service_tickets.services.staff_identity import StaffPrincipal

STATUSES = ("received", "in_progress", "closed", "cancelled")

#: (from, to, who). Requirement 14's table, written out.
ALLOWED = {
    ("received", "in_progress", "staff"),
    ("in_progress", "closed", "staff"),
    ("closed", "in_progress", "staff"),
    ("received", "cancelled", "customer"),
    ("in_progress", "cancelled", "customer"),
}

LONG_AGO = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
CHANGE_TIME = datetime(2031, 6, 15, 8, 30, tzinfo=UTC)
USERNAME = "tech.dana"

#: A valid resolution, required by every change to `closed` (R29.1). Without parts.
RESOLUTION = Resolution(
    entry_text="Replaced the washer", technician_name="Dana", parts_replaced=False, part_ids=[]
)

_numbers = itertools.count(10000)


def _utc(value: datetime) -> datetime:
    """SQLite returns timezone-aware columns as naive values; read them as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------------- fixtures


@pytest.fixture
def owner(session: Session) -> Customer:
    customer = Customer(phone="0501234567", full_name="Owner", full_name_key="owner", password_hash="x")
    session.add(customer)
    session.commit()
    return customer


@pytest.fixture
def stranger(session: Session) -> Customer:
    customer = Customer(phone="0527654321", full_name="Other", full_name_key="other", password_hash="x")
    session.add(customer)
    session.commit()
    return customer


@pytest.fixture
def category(session: Session) -> Category:
    row = Category(seq=1, name_he="חשמל", name_en="Electric", name_he_key="חשמל", name_en_key="electric")
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def staff() -> StaffPrincipal:
    return StaffPrincipal(
        user_id=uuid.uuid4(),
        username=USERNAME,
        role=UserRole.TECHNICIAN,
        must_change_password=False,
        is_2fa_enrolment_required=False,
    )


@pytest.fixture
def owning_customer(owner: Customer) -> CustomerActor:
    return CustomerActor(customer_id=owner.id)


@pytest.fixture
def make_ticket(session: Session, owner: Customer, category: Category):
    def _make(status: str = "received", *, name: str = "", customer: Customer | None = None) -> Ticket:
        ticket = Ticket(
            ticket_number=next(_numbers),
            customer_id=(customer or owner).id,
            category_id=category.id,
            city="Haifa",
            city_key="haifa",
            apartment="12",
            phone="0501234567",
            description="leaking tap",
            urgent=True,
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


def _row(session: Session, number: int) -> dict[str, object]:
    """The stored row, read through Core so the session's identity map cannot hide a difference."""
    table = Ticket.__table__
    return dict(session.execute(select(table).where(table.c.ticket_number == number)).mappings().one())


def _history(session: Session) -> list[tuple]:
    table = HistoryEntry.__table__
    return sorted(tuple(row) for row in session.execute(select(table)).all())


@pytest.fixture
def statements(session: Session) -> Iterator[list[str]]:
    engine: Engine = session.get_bind()  # type: ignore[assignment]
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _updates(seen: list[str]) -> list[str]:
    return [statement for statement in seen if statement.lstrip().upper().startswith("UPDATE")]


# --------------------------------------------------------------------------------- the table


def test_the_transition_table_has_exactly_the_five_rows_of_requirement_14() -> None:
    table = {(source, to, who) for (source, to), who in lifecycle.TRANSITIONS.items()}
    assert table == ALLOWED
    assert not any(source == "cancelled" for source, _, _ in table)
    with pytest.raises(TypeError):
        lifecycle.TRANSITIONS[("closed", "received")] = "staff"  # type: ignore[index]


def test_every_error_carries_its_api_code() -> None:
    assert TicketNotFound.code == error_codes.CODE_TICKET_NOT_FOUND == "ticket_not_found"
    assert TicketStatusChanged.code == error_codes.CODE_TICKET_STATUS_CHANGED == "ticket_status_changed"
    assert InvalidTransition.code == error_codes.CODE_INVALID_TRANSITION == "invalid_transition"
    assert InvalidExpectedStatus.code == error_codes.CODE_INVALID_EXPECTED_STATUS == "invalid_expected_status"
    assert InvalidTargetStatus.code == error_codes.CODE_INVALID_TARGET_STATUS == "invalid_target_status"
    assert (
        InvalidAssignedTechnicianName.code
        == error_codes.CODE_INVALID_ASSIGNED_TECHNICIAN_NAME
        == "invalid_assigned_technician_name"
    )


# --------------------------------------------------------------------------------- the matrix


@pytest.mark.parametrize("who", ["staff", "customer"])
@pytest.mark.parametrize("target", STATUSES)
@pytest.mark.parametrize("stored", STATUSES)
def test_transition_matrix_cell(
    session: Session,
    make_ticket,
    staff: StaffPrincipal,
    owning_customer: CustomerActor,
    statements: list[str],
    stored: str,
    target: str,
    who: str,
) -> None:
    actor = staff if who == "staff" else owning_customer
    ticket = make_ticket(stored)
    before = _row(session, ticket.ticket_number)
    statements.clear()
    resolution = RESOLUTION if target == "closed" else None

    if (stored, target, who) in ALLOWED:
        changed = change_status(
            session, ticket.ticket_number, stored, target, actor, resolution=resolution, now=CHANGE_TIME
        )
        after = _row(session, ticket.ticket_number)
        assert changed.status == target == after["status"]
        assert _utc(after["updated_at"]) == CHANGE_TIME  # type: ignore[arg-type]
        assert len(_updates(statements)) == 1
    else:
        with pytest.raises(InvalidTransition) as raised:
            change_status(
                session, ticket.ticket_number, stored, target, actor, resolution=resolution, now=CHANGE_TIME
            )
        assert raised.value.code == "invalid_transition"
        assert _row(session, ticket.ticket_number) == before
        assert _updates(statements) == []  # a rejected request never reaches the UPDATE


@pytest.mark.parametrize("target", STATUSES)
@pytest.mark.parametrize("stored", STATUSES)
def test_staff_can_never_cancel_and_cancelled_never_changes(
    session: Session, make_ticket, staff: StaffPrincipal, stored: str, target: str
) -> None:
    ticket = make_ticket(stored)
    if target == "cancelled":
        with pytest.raises(InvalidTransition):
            change_status(session, ticket.ticket_number, stored, target, staff)
        assert _row(session, ticket.ticket_number)["status"] == stored
    if stored == "cancelled":
        with pytest.raises(InvalidTransition):
            change_status(session, ticket.ticket_number, stored, target, staff)
        assert _row(session, ticket.ticket_number)["status"] == "cancelled"


def test_a_customer_cannot_move_a_ticket_to_a_staff_status(
    session: Session, make_ticket, owning_customer: CustomerActor
) -> None:
    # The 403 of R14.5 belongs to the router; the service refuses the pair as not tabled for the actor.
    ticket = make_ticket("received")
    for target in ("in_progress", "closed", "received"):
        with pytest.raises(InvalidTransition):
            change_status(session, ticket.ticket_number, "received", target, owning_customer)
    assert _row(session, ticket.ticket_number)["status"] == "received"


# --------------------------------------------------------------------------------- check order


@pytest.mark.parametrize("who", ["staff", "customer"])
@pytest.mark.parametrize("stored", STATUSES)
def test_ticket_status_changed_precedes_invalid_transition(
    session: Session,
    make_ticket,
    staff: StaffPrincipal,
    owning_customer: CustomerActor,
    statements: list[str],
    stored: str,
    who: str,
) -> None:
    actor = staff if who == "staff" else owning_customer
    ticket = make_ticket(stored)
    before = _row(session, ticket.ticket_number)
    statements.clear()
    for expected, target in itertools.product(STATUSES, STATUSES):
        if expected == stored:
            continue
        # Includes targets that would be `invalid_transition` (the stored status itself, staff cancel).
        with pytest.raises(TicketStatusChanged) as raised:
            change_status(session, ticket.ticket_number, expected, target, actor)
        assert raised.value.code == "ticket_status_changed"
    assert _row(session, ticket.ticket_number) == before
    assert _updates(statements) == []


def test_a_stale_expected_status_loses_even_when_the_session_holds_the_old_row(
    session: Session, make_ticket, staff: StaffPrincipal
) -> None:
    ticket = make_ticket("received")
    session.get(Ticket, ticket.id)  # the identity map now holds status "received"
    table = Ticket.__table__
    where = table.c.ticket_number == ticket.ticket_number
    session.execute(table.update().where(where).values(status="closed"))
    # A stale read would see "received", pass the expected-status check and fail later (as
    # `invalid_transition` for a staff cancel, or at the guarded UPDATE): the stored "closed" must be
    # what the check order sees, so both requests get `ticket_status_changed` from step 3.
    for target in ("cancelled", "in_progress"):
        with pytest.raises(TicketStatusChanged):
            change_status(session, ticket.ticket_number, "received", target, staff)
    assert _row(session, ticket.ticket_number)["status"] == "closed"


# --------------------------------------------------------------------------------- 422 shapes


@pytest.mark.parametrize(
    "bad", [None, "", " received", "Received", "RECEIVED", "open", "in progress", 1, True, ["received"]]
)
def test_missing_or_unknown_expected_status_is_invalid_expected_status(
    session: Session, make_ticket, staff: StaffPrincipal, bad: object
) -> None:
    ticket = make_ticket("received")
    before = _row(session, ticket.ticket_number)
    with pytest.raises(InvalidExpectedStatus) as raised:
        change_status(session, ticket.ticket_number, bad, "in_progress", staff)  # type: ignore[arg-type]
    assert raised.value.code == "invalid_expected_status"
    assert _row(session, ticket.ticket_number) == before


@pytest.mark.parametrize("bad", [None, "", "Closed", "done", "open", 2, False])
def test_missing_or_unknown_target_is_invalid_target_status(
    session: Session, make_ticket, staff: StaffPrincipal, bad: object
) -> None:
    ticket = make_ticket("received")
    before = _row(session, ticket.ticket_number)
    with pytest.raises(InvalidTargetStatus) as raised:
        change_status(session, ticket.ticket_number, "received", bad, staff)  # type: ignore[arg-type]
    assert raised.value.code == "invalid_target_status"
    assert _row(session, ticket.ticket_number) == before


def test_input_errors_come_before_the_ticket_lookup_and_expected_comes_first(
    session: Session, staff: StaffPrincipal
) -> None:
    with pytest.raises(InvalidExpectedStatus):
        change_status(session, 99999, None, None, staff)  # type: ignore[arg-type]
    with pytest.raises(InvalidTargetStatus):
        change_status(session, 99999, "received", None, staff)  # type: ignore[arg-type]
    with pytest.raises(InvalidAssignedTechnicianName):
        change_status(session, 99999, "received", "in_progress", staff, "x" * 101)
    with pytest.raises(TicketNotFound):
        change_status(session, 99999, "received", "in_progress", staff)


# --------------------------------------------------------------------------------- 404


def test_an_unknown_ticket_number_is_not_found(session: Session, staff: StaffPrincipal) -> None:
    with pytest.raises(TicketNotFound) as raised:
        change_status(session, 54321, "received", "in_progress", staff)
    assert raised.value.code == "ticket_not_found"


def test_not_found_precedes_the_expected_status_check(session: Session, staff: StaffPrincipal) -> None:
    with pytest.raises(TicketNotFound):
        change_status(session, 54321, "closed", "closed", staff)


def test_a_ticket_of_another_customer_looks_exactly_like_a_missing_one(
    session: Session, make_ticket, stranger: Customer, owning_customer: CustomerActor, statements: list[str]
) -> None:
    foreign = make_ticket("received", customer=stranger)
    before = _row(session, foreign.ticket_number)
    statements.clear()

    with pytest.raises(TicketNotFound) as missing:
        change_status(session, 54321, "received", "cancelled", owning_customer)
    # Even a wrong expected status must not reveal that the ticket exists.
    for expected in ("received", "closed"):
        with pytest.raises(TicketNotFound) as foreign_error:
            change_status(session, foreign.ticket_number, expected, "cancelled", owning_customer)
        assert type(foreign_error.value) is type(missing.value)
        assert foreign_error.value.args == missing.value.args
        assert foreign_error.value.code == missing.value.code == "ticket_not_found"

    assert _row(session, foreign.ticket_number) == before
    assert _updates(statements) == []


def test_staff_can_change_any_customers_ticket(
    session: Session, make_ticket, stranger: Customer, staff: StaffPrincipal
) -> None:
    foreign = make_ticket("received", customer=stranger)
    changed = change_status(session, foreign.ticket_number, "received", "in_progress", staff)
    assert changed.status == "in_progress"


def test_the_owning_customer_is_matched_by_id_not_by_phone(
    session: Session, make_ticket, owner: Customer, stranger: Customer
) -> None:
    ticket = make_ticket("received")
    with pytest.raises(TicketNotFound):
        change_status(session, ticket.ticket_number, "received", "cancelled", CustomerActor(stranger.id))
    cancelled = change_status(session, ticket.ticket_number, "received", "cancelled", CustomerActor(owner.id))
    assert cancelled.status == "cancelled"


def test_an_unsupported_actor_is_a_programming_error(session: Session, make_ticket) -> None:
    ticket = make_ticket("received")
    with pytest.raises(TypeError):
        change_status(session, ticket.ticket_number, "received", "in_progress", object())  # type: ignore[arg-type]
    assert _row(session, ticket.ticket_number)["status"] == "received"


# --------------------------------------------------------------------------------- the assigned name


def _approve(session: Session, ticket: Ticket, staff: StaffPrincipal, name: str | None) -> Ticket:
    return change_status(session, ticket.ticket_number, "received", "in_progress", staff, name)


def test_approval_sets_a_supplied_name_trimmed(session: Session, make_ticket, staff: StaffPrincipal) -> None:
    ticket = make_ticket("received")
    assert _approve(session, ticket, staff, "  Dana Levi \n").assigned_technician_name == "Dana Levi"
    assert _row(session, ticket.ticket_number)["assigned_technician_name"] == "Dana Levi"


def test_approval_replaces_an_existing_name_with_the_supplied_one(
    session: Session, make_ticket, staff: StaffPrincipal
) -> None:
    ticket = make_ticket("received", name="Old Name")
    assert _approve(session, ticket, staff, "New Name").assigned_technician_name == "New Name"


def test_approval_accepts_hebrew_and_the_one_character_and_100_character_limits(
    session: Session, make_ticket, staff: StaffPrincipal
) -> None:
    for supplied in ("דנה כהן", "x", "y" * 100, "  " + "z" * 100 + "  "):
        ticket = make_ticket("received")
        assert _approve(session, ticket, staff, supplied).assigned_technician_name == supplied.strip()


@pytest.mark.parametrize("supplied", [None, "", " ", "\t\n  "])
def test_approval_without_a_name_defaults_an_empty_name_to_the_username(
    session: Session, make_ticket, staff: StaffPrincipal, supplied: str | None
) -> None:
    ticket = make_ticket("received", name="")
    assert _approve(session, ticket, staff, supplied).assigned_technician_name == USERNAME
    assert _row(session, ticket.ticket_number)["assigned_technician_name"] == USERNAME


@pytest.mark.parametrize("supplied", [None, "", "   "])
def test_approval_without_a_name_keeps_an_existing_name(
    session: Session, make_ticket, staff: StaffPrincipal, supplied: str | None
) -> None:
    ticket = make_ticket("received", name="Existing")
    assert _approve(session, ticket, staff, supplied).assigned_technician_name == "Existing"


def test_a_username_longer_than_the_column_allows_is_capped(session: Session, make_ticket) -> None:
    long_user = StaffPrincipal(uuid.uuid4(), "u" * 120, UserRole.TECHNICIAN, False, False)
    ticket = make_ticket("received")
    assert _approve(session, ticket, long_user, None).assigned_technician_name == "u" * 100


@pytest.mark.parametrize("supplied", ["x" * 101, "  " + "x" * 101, "é" * 101, 5])
def test_a_name_over_100_characters_after_trimming_is_rejected_and_nothing_changes(
    session: Session, make_ticket, staff: StaffPrincipal, statements: list[str], supplied: object
) -> None:
    ticket = make_ticket("received", name="Keep")
    before = _row(session, ticket.ticket_number)
    statements.clear()
    with pytest.raises(InvalidAssignedTechnicianName) as raised:
        _approve(session, ticket, staff, supplied)  # type: ignore[arg-type]
    assert raised.value.code == "invalid_assigned_technician_name"
    assert _row(session, ticket.ticket_number) == before
    assert _updates(statements) == []


@pytest.mark.parametrize(
    ("stored", "target"), [("received", "in_progress"), ("in_progress", "closed"), ("closed", "in_progress")]
)
def test_an_over_long_name_is_rejected_for_every_staff_change(
    session: Session, make_ticket, staff: StaffPrincipal, stored: str, target: str
) -> None:
    ticket = make_ticket(stored, name="Keep")
    before = _row(session, ticket.ticket_number)
    with pytest.raises(InvalidAssignedTechnicianName):
        change_status(session, ticket.ticket_number, stored, target, staff, "x" * 101)
    assert _row(session, ticket.ticket_number) == before


def test_closing_ignores_a_supplied_name(session: Session, make_ticket, staff: StaffPrincipal) -> None:
    ticket = make_ticket("in_progress", name="Dana")
    changed = change_status(
        session, ticket.ticket_number, "in_progress", "closed", staff, "Someone Else", RESOLUTION
    )
    assert changed.assigned_technician_name == "Dana"


def test_closing_does_not_fill_an_empty_name(session: Session, make_ticket, staff: StaffPrincipal) -> None:
    ticket = make_ticket("in_progress", name="")
    closed = change_status(session, ticket.ticket_number, "in_progress", "closed", staff, None, RESOLUTION)
    assert closed.assigned_technician_name == ""


@pytest.mark.parametrize("name", ["Dana", ""])
def test_reopen_keeps_the_name_even_when_one_is_supplied_or_the_name_is_empty(
    session: Session, make_ticket, staff: StaffPrincipal, name: str
) -> None:
    ticket = make_ticket("closed", name=name)
    reopened = change_status(session, ticket.ticket_number, "closed", "in_progress", staff, "Replacement")
    assert reopened.status == "in_progress"
    assert reopened.assigned_technician_name == name  # an empty name stays empty: only the approval fills it


def test_customer_cancel_ignores_a_supplied_name(
    session: Session, make_ticket, owning_customer: CustomerActor
) -> None:
    ticket = make_ticket("in_progress", name="Dana")
    number = ticket.ticket_number
    cancelled = change_status(session, number, "in_progress", "cancelled", owning_customer, "Evil")
    assert cancelled.assigned_technician_name == "Dana"
    unassigned = make_ticket("received", name="")
    number = unassigned.ticket_number
    cancelled = change_status(session, number, "received", "cancelled", owning_customer, "Evil")
    assert cancelled.assigned_technician_name == ""


# --------------------------------------------------------------------------------- last-modified time


def test_a_successful_change_sets_updated_at_to_the_change_time_and_touches_nothing_else(
    session: Session, make_ticket, staff: StaffPrincipal
) -> None:
    ticket = make_ticket("received")
    before = _row(session, ticket.ticket_number)
    change_status(session, ticket.ticket_number, "received", "in_progress", staff, now=CHANGE_TIME)
    after = _row(session, ticket.ticket_number)
    assert _utc(after["updated_at"]) == CHANGE_TIME  # type: ignore[arg-type]
    changed = {key for key in before if before[key] != after[key]}
    assert changed == {"status", "updated_at", "assigned_technician_name"}
    assert _utc(after["created_at"]) == LONG_AGO  # type: ignore[arg-type]


def test_without_an_injected_clock_updated_at_is_the_current_utc_time(
    session: Session, make_ticket, staff: StaffPrincipal
) -> None:
    ticket = make_ticket("received")
    start = datetime.now(UTC) - timedelta(seconds=1)
    change_status(session, ticket.ticket_number, "received", "in_progress", staff)
    stamped = _utc(_row(session, ticket.ticket_number)["updated_at"])  # type: ignore[arg-type]
    assert start <= stamped <= datetime.now(UTC) + timedelta(seconds=1)
    assert stamped > LONG_AGO


def test_each_kind_of_change_sets_updated_at(
    session: Session, make_ticket, staff: StaffPrincipal, owning_customer: CustomerActor
) -> None:
    closed = make_ticket("in_progress")
    change_status(
        session, closed.ticket_number, "in_progress", "closed", staff, resolution=RESOLUTION, now=CHANGE_TIME
    )
    reopened_at = CHANGE_TIME + timedelta(hours=1)
    change_status(session, closed.ticket_number, "closed", "in_progress", staff, now=reopened_at)
    assert _utc(_row(session, closed.ticket_number)["updated_at"]) == reopened_at  # type: ignore[arg-type]
    cancelled = make_ticket("received")
    change_status(session, cancelled.ticket_number, "received", "cancelled", owning_customer, now=CHANGE_TIME)
    assert _utc(_row(session, cancelled.ticket_number)["updated_at"]) == CHANGE_TIME  # type: ignore[arg-type]


# --------------------------------------------------------------------------------- history and commit


def _add_history(session: Session, ticket: Ticket) -> None:
    for index in range(2):
        session.add(
            HistoryEntry(
                ticket_id=ticket.id,
                entry_text=f"note {index}",
                technician_name="Dana",
                created_at=LONG_AGO + timedelta(minutes=index),
            )
        )
    session.commit()


def test_only_the_resolution_adds_a_history_entry_and_existing_entries_stay_unchanged(
    session: Session, make_ticket, staff: StaffPrincipal, owning_customer: CustomerActor
) -> None:
    ticket = make_ticket("received")
    _add_history(session, ticket)
    entries = _history(session)
    assert len(entries) == 2

    number = ticket.ticket_number
    change_status(session, number, "received", "in_progress", staff, "Dana")
    assert _history(session) == entries  # approval adds none
    change_status(session, number, "in_progress", "closed", staff, resolution=RESOLUTION)
    resolved = _history(session)
    assert len(resolved) == 3 and all(entry in resolved for entry in entries)  # the resolution adds one
    change_status(session, number, "closed", "in_progress", staff)  # reopen
    change_status(session, number, "in_progress", "cancelled", owning_customer)
    assert _history(session) == resolved  # reopen and cancel add none
    assert session.scalar(select(func.count()).select_from(HistoryEntry)) == 3


def test_rejected_changes_leave_history_unchanged_too(
    session: Session, make_ticket, staff: StaffPrincipal
) -> None:
    ticket = make_ticket("closed")
    _add_history(session, ticket)
    entries = _history(session)
    with pytest.raises(InvalidTransition):
        change_status(session, ticket.ticket_number, "closed", "closed", staff)
    with pytest.raises(TicketStatusChanged):
        change_status(session, ticket.ticket_number, "received", "in_progress", staff)
    assert _history(session) == entries


def test_the_service_never_commits(session: Session, make_ticket, staff: StaffPrincipal) -> None:
    ticket = make_ticket("received", name="")
    before = _row(session, ticket.ticket_number)
    change_status(session, ticket.ticket_number, "received", "in_progress", staff, "Dana")
    assert _row(session, ticket.ticket_number)["status"] == "in_progress"
    session.rollback()  # the router owns the commit; without it nothing was stored
    assert _row(session, ticket.ticket_number) == before


# --------------------------------------------------------------------------------- the guarded UPDATE


def _interloper(session: Session, number: int, status: str) -> Iterator[list[str]]:
    """On the first `UPDATE svc_tickets`, set `status` through the same connection, just before it runs.

    This is what a concurrent request that committed between the read and the write looks like to the
    guarded `UPDATE`.
    """
    engine: Engine = session.get_bind()  # type: ignore[assignment]
    fired: list[str] = []

    def hook(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if statement.lstrip().upper().startswith("UPDATE SVC_TICKETS") and not fired:
            fired.append(statement)
            conn.exec_driver_sql(
                "UPDATE svc_tickets SET status = ? WHERE ticket_number = ?", (status, number)
            )

    event.listen(engine, "before_cursor_execute", hook)
    try:
        yield fired
    finally:
        event.remove(engine, "before_cursor_execute", hook)


def test_the_update_is_guarded_by_the_expected_status(
    session: Session, make_ticket, staff: StaffPrincipal
) -> None:
    ticket = make_ticket("received", name="")
    gen = _interloper(session, ticket.ticket_number, "cancelled")
    fired = next(gen)
    try:
        with pytest.raises(TicketStatusChanged) as raised:
            change_status(
                session, ticket.ticket_number, "received", "in_progress", staff, "Dana", now=CHANGE_TIME
            )
    finally:
        gen.close()
    assert fired, "the interloper never ran, so the guard was not exercised"
    assert raised.value.code == "ticket_status_changed"
    after = _row(session, ticket.ticket_number)
    # The winner's state stands: the losing request wrote neither status, name nor time.
    assert after["status"] == "cancelled"
    assert after["assigned_technician_name"] == ""
    assert _utc(after["updated_at"]) == LONG_AGO  # type: ignore[arg-type]


def test_a_customer_cancel_that_loses_the_race_to_staff_gets_ticket_status_changed(
    session: Session, make_ticket, owning_customer: CustomerActor
) -> None:
    ticket = make_ticket("received")
    gen = _interloper(session, ticket.ticket_number, "in_progress")
    fired = next(gen)
    try:
        with pytest.raises(TicketStatusChanged):
            change_status(session, ticket.ticket_number, "received", "cancelled", owning_customer)
    finally:
        gen.close()
    assert fired
    assert _row(session, ticket.ticket_number)["status"] == "in_progress"


def test_the_guarded_update_names_the_ticket_number_and_the_expected_status(
    session: Session, make_ticket, staff: StaffPrincipal, statements: list[str]
) -> None:
    ticket = make_ticket("received")
    statements.clear()
    change_status(session, ticket.ticket_number, "received", "in_progress", staff)
    (update,) = _updates(statements)
    where = update.upper().split("WHERE", 1)[1]
    assert "TICKET_NUMBER" in where and "STATUS" in where
    assert "COALESCE" in update.upper()


# --------------------------------------------------------------------------------- the resolution (R29)


def _part(session: Session, name: str, seq: int, *, active: bool = True) -> Part:
    part = Part(
        seq=seq,
        name_he=f"{name}-he",
        name_en=name,
        name_he_key=f"{name}-he".casefold(),
        name_en_key=name.casefold(),
        is_active=active,
    )
    session.add(part)
    session.commit()
    return part


def _part_ids(session: Session, ticket: Ticket) -> set[uuid.UUID]:
    table = TicketPart.__table__
    rows = session.execute(select(table.c.part_id).where(table.c.ticket_id == ticket.id)).all()
    return {row[0] for row in rows}


def test_the_resolution_errors_carry_their_api_codes() -> None:
    assert ResolutionRequired.code == error_codes.CODE_RESOLUTION_REQUIRED == "resolution_required"
    assert UnexpectedResolution.code == error_codes.CODE_UNKNOWN_FIELD == "unknown_field"


def test_closing_without_a_resolution_is_refused_before_the_update(
    session: Session, make_ticket, staff: StaffPrincipal, statements: list[str]
) -> None:
    ticket = make_ticket("in_progress")
    before = _row(session, ticket.ticket_number)
    statements.clear()
    with pytest.raises(ResolutionRequired) as raised:
        change_status(session, ticket.ticket_number, "in_progress", "closed", staff)
    assert raised.value.code == "resolution_required"
    assert _row(session, ticket.ticket_number) == before
    assert _history(session) == []
    assert _updates(statements) == []


@pytest.mark.parametrize(
    ("stored", "target"), [("received", "in_progress"), ("closed", "in_progress")]
)
def test_a_resolution_with_any_other_staff_target_is_refused_and_changes_nothing(
    session: Session, make_ticket, staff: StaffPrincipal, statements: list[str], stored: str, target: str
) -> None:
    ticket = make_ticket(stored)
    before = _row(session, ticket.ticket_number)
    statements.clear()
    with pytest.raises(UnexpectedResolution) as raised:
        change_status(session, ticket.ticket_number, stored, target, staff, resolution=RESOLUTION)
    assert raised.value.code == "unknown_field"
    assert _row(session, ticket.ticket_number) == before
    assert _history(session) == []
    assert _updates(statements) == []


def test_a_resolution_with_a_customer_cancel_is_refused(
    session: Session, make_ticket, owning_customer: CustomerActor
) -> None:
    ticket = make_ticket("received")
    with pytest.raises(UnexpectedResolution):
        change_status(
            session, ticket.ticket_number, "received", "cancelled", owning_customer, resolution=RESOLUTION
        )
    assert _row(session, ticket.ticket_number)["status"] == "received"


def test_the_lifecycle_checks_come_before_the_resolution_checks(
    session: Session, make_ticket, staff: StaffPrincipal
) -> None:
    bad = Resolution(entry_text="", technician_name="", parts_replaced="yes", part_ids=None)
    ticket = make_ticket("received")
    with pytest.raises(TicketNotFound):
        change_status(session, 54321, "in_progress", "closed", staff, resolution=bad)
    with pytest.raises(TicketStatusChanged):
        change_status(session, ticket.ticket_number, "in_progress", "closed", staff, resolution=bad)
    with pytest.raises(InvalidTransition):
        change_status(session, ticket.ticket_number, "received", "closed", staff, resolution=bad)
    with pytest.raises(InvalidTransition):  # not even `resolution_required`
        change_status(session, ticket.ticket_number, "received", "closed", staff)


def test_closing_adds_the_entry_with_the_clock_of_the_change_and_stores_the_parts(
    session: Session, make_ticket, staff: StaffPrincipal
) -> None:
    gasket, valve = _part(session, "Gasket", 1), _part(session, "Valve", 2)
    ticket = make_ticket("in_progress", name="Moshe")
    resolution = Resolution("  Swapped parts ", " Dana ", True, [str(valve.id), str(gasket.id)])

    closed = change_status(
        session, ticket.ticket_number, "in_progress", "closed", staff, resolution=resolution, now=CHANGE_TIME
    )

    assert (closed.status, closed.assigned_technician_name) == ("closed", "Moshe")
    ((_, _, entry_text, technician_name, created_at, edited_at),) = _history(session)
    assert (entry_text, technician_name, edited_at) == ("Swapped parts", "Dana", None)
    assert _utc(created_at) == CHANGE_TIME
    assert _part_ids(session, ticket) == {gasket.id, valve.id}


def test_a_failed_resolution_reports_all_fields_in_form_order_and_changes_nothing(
    session: Session, make_ticket, staff: StaffPrincipal, statements: list[str]
) -> None:
    inactive = _part(session, "Retired", 1, active=False)
    kept = _part(session, "Kept", 2)
    ticket = make_ticket("in_progress")
    session.add(TicketPart(ticket_id=ticket.id, part_id=kept.id))
    session.commit()
    before = _row(session, ticket.ticket_number)
    statements.clear()
    bad = Resolution("", "x" * 101, True, [str(inactive.id)])

    with pytest.raises(ResolutionValidationError) as raised:
        change_status(session, ticket.ticket_number, "in_progress", "closed", staff, resolution=bad)

    assert raised.value.fields == {
        "entry_text": "invalid_history_text",
        "technician_name": "invalid_technician_name",
        "part_ids": "invalid_part",
    }
    assert list(raised.value.fields) == ["entry_text", "technician_name", "part_ids"]
    assert raised.value.code == "invalid_history_text"
    assert _row(session, ticket.ticket_number) == before
    assert _history(session) == []
    assert _part_ids(session, ticket) == {kept.id}
    assert _updates(statements) == []


def test_a_resolution_that_loses_the_race_leaves_no_entry_and_no_parts(
    session: Session, make_ticket, staff: StaffPrincipal
) -> None:
    gasket = _part(session, "Gasket", 1)
    ticket = make_ticket("in_progress")
    gen = _interloper(session, ticket.ticket_number, "cancelled")
    fired = next(gen)
    try:
        with pytest.raises(TicketStatusChanged):
            change_status(
                session,
                ticket.ticket_number,
                "in_progress",
                "closed",
                staff,
                resolution=Resolution("Done", "Dana", True, [str(gasket.id)]),
            )
    finally:
        gen.close()
    assert fired
    assert _row(session, ticket.ticket_number)["status"] == "cancelled"
    assert _history(session) == []
    assert _part_ids(session, ticket) == set()


def test_reopening_keeps_the_parts_and_a_later_resolution_replaces_them(
    session: Session, make_ticket, staff: StaffPrincipal
) -> None:
    gasket, valve = _part(session, "Gasket", 1), _part(session, "Valve", 2)
    ticket = make_ticket("in_progress")
    number = ticket.ticket_number

    def resolve(resolution: Resolution) -> None:
        change_status(session, number, "in_progress", "closed", staff, resolution=resolution)

    resolve(Resolution("A", "Dana", True, [gasket.id]))
    change_status(session, number, "closed", "in_progress", staff)
    assert _part_ids(session, ticket) == {gasket.id}
    resolve(Resolution("B", "Dana", True, [valve.id]))
    assert _part_ids(session, ticket) == {valve.id}
    change_status(session, number, "closed", "in_progress", staff)
    resolve(RESOLUTION)  # parts_replaced is false
    assert _part_ids(session, ticket) == set()
    assert len(_history(session)) == 3