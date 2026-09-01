"""Employee service rules that do not need HTTP (Requirement 3, and 16.9 for the resolver).

The claims here are about the service and the model: that a passport is unique across non-terminated
employees and the conflicting person is named, that terminating an employee frees their passport for
reuse, that the rate history is a non-overlapping chain, and — the load-bearing one for payroll — that
the resolver returns the rate in force on a given date, splitting a month at a mid-month rate change.

These run against the in-memory SQLite session the suite already provides. The partial unique index
and the GiST exclusion constraint are PostgreSQL-only and are the database backstop; what is tested
here is the application enforcement that has to hold on both engines.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.change_log import REDACTED, ChangeLog
from app.models.employee import Employee, EmployeeStatus
from app.schemas.employee import EmployeeCreate, EmployeeRateInput, EmployeeUpdate
from app.services import employee as employee_service
from app.services.audit import AuditContext


def _context() -> AuditContext:
    return AuditContext(actor_user_id=uuid.uuid4(), reason=None)


def _rate(
    hourly: str,
    effective_from: date,
    effective_to: date | None = None,
    *,
    overtime: str | None = None,
) -> EmployeeRateInput:
    return EmployeeRateInput(
        hourly_wage=Decimal(hourly),
        overtime_rate=Decimal(overtime if overtime is not None else "0"),
        shabbat_holiday_rate=Decimal("0"),
        effective_from=effective_from,
        effective_to=effective_to,
    )


def _create_payload(passport: str = "A1234567", **overrides) -> EmployeeCreate:
    fields: dict[str, object] = {
        "full_name": "Ahmed Khalil",
        "full_name_en": "Ahmed Khalil",
        "passport_number": passport,
        "phone": "+972500000000",
        "country": "Jordan",
        "emergency_contact_name": "Layla Khalil",
        "emergency_contact_phone": "+972500000001",
        "start_date": date(2025, 1, 1),
    }
    fields.update(overrides)
    return EmployeeCreate(**fields)


def _make_employee(session: Session, **overrides) -> Employee:
    employee = employee_service.create_employee(session, _create_payload(**overrides), context=_context())
    session.commit()
    return employee


# --------------------------------------------------------------------------- mandatory fields
# Requirement 3.5: the validation is at the schema, so a blank mandatory field never reaches the
# service. Asserting it here keeps the rule pinned to the field list the requirement names.


@pytest.mark.parametrize(
    "field",
    ["full_name", "full_name_en", "country", "emergency_contact_name", "passport_number", "phone"],
)
def test_a_blank_mandatory_field_is_rejected(field: str):
    with pytest.raises(ValueError):
        _create_payload(**{field: "   "})


def test_a_missing_mandatory_field_is_rejected():
    with pytest.raises(ValueError):
        EmployeeCreate(full_name="Only a name")  # type: ignore[call-arg]


# --------------------------------------------------------------------------- passport uniqueness


def test_a_duplicate_passport_is_rejected_and_names_the_conflict(session: Session):
    """Requirement 3.6. The same number, differently cased and spaced, still collides — the hash
    normalises case and surrounding whitespace — and the conflicting employee is reported."""
    first = _make_employee(session, passport="A1234567")

    with pytest.raises(employee_service.DuplicatePassport) as raised:
        employee_service.create_employee(
            session, _create_payload(passport=" a1234567 "), context=_context()
        )

    assert raised.value.conflicting.id == first.id


def test_a_terminated_employees_passport_can_be_reused(session: Session):
    """Requirement 3.6 read with 3.8: uniqueness is over non-terminated employees, so terminating one
    frees the number for a genuinely different person or a rehire."""
    first = _make_employee(session, passport="A1234567")
    employee_service.change_status(
        session, first.id, EmployeeStatus.TERMINATED, context=_context()
    )
    session.commit()

    reused = employee_service.create_employee(
        session, _create_payload(passport="A1234567", full_name="Someone Else"), context=_context()
    )
    session.commit()

    assert reused.id != first.id


def test_updating_a_passport_to_one_in_use_is_rejected(session: Session):
    first = _make_employee(session, passport="A1111111")
    second = _make_employee(session, passport="B2222222", full_name="Other Person")

    with pytest.raises(employee_service.DuplicatePassport) as raised:
        employee_service.update_employee(
            session, second.id, EmployeeUpdate(passport_number="A1111111"), context=_context()
        )
    assert raised.value.conflicting.id == first.id


def test_updating_other_fields_does_not_trip_the_passport_check(session: Session):
    """A patch that leaves the passport unchanged must not collide the row with itself."""
    employee = _make_employee(session, passport="A1234567")
    updated = employee_service.update_employee(
        session, employee.id, EmployeeUpdate(position="Foreman"), context=_context()
    )
    assert updated.position == "Foreman"


# --------------------------------------------------------------------------- status transitions


def test_terminated_is_a_one_way_door(session: Session):
    employee = _make_employee(session)
    employee_service.change_status(session, employee.id, EmployeeStatus.TERMINATED, context=_context())
    session.commit()

    with pytest.raises(employee_service.InvalidStatusTransition):
        employee_service.change_status(session, employee.id, EmployeeStatus.ACTIVE, context=_context())


def test_active_and_leave_move_freely(session: Session):
    employee = _make_employee(session)
    employee_service.change_status(session, employee.id, EmployeeStatus.ON_LEAVE, context=_context())
    back = employee_service.change_status(
        session, employee.id, EmployeeStatus.ACTIVE, context=_context()
    )
    assert back.status is EmployeeStatus.ACTIVE


def test_no_hard_delete_the_row_survives_termination(session: Session):
    """Requirement 3.8: there is no delete path; the row stays so time entries keep a parent."""
    employee = _make_employee(session)
    employee_service.change_status(session, employee.id, EmployeeStatus.TERMINATED, context=_context())
    session.commit()

    still_there = session.get(Employee, employee.id)
    assert still_there is not None
    assert still_there.status is EmployeeStatus.TERMINATED


# --------------------------------------------------------------------------- rate history + resolver


def test_rate_resolution_across_a_mid_month_change(session: Session):
    """Requirement 16.9. A rate of 30 to 15 August and 35 from 16 August must resolve to 30 for a
    date in the first half and 35 for a date in the second — this is what splits a month's pay."""
    employee = _make_employee(session)
    employee_service.replace_rate_history(
        session,
        employee.id,
        [
            _rate("30.00", date(2025, 8, 1), date(2025, 8, 15)),
            _rate("35.00", date(2025, 8, 16)),
        ],
        context=_context(),
    )
    session.commit()

    early = employee_service.resolve_rate(employee, date(2025, 8, 10))
    late = employee_service.resolve_rate(employee, date(2025, 8, 20))
    boundary_low = employee_service.resolve_rate(employee, date(2025, 8, 15))
    boundary_high = employee_service.resolve_rate(employee, date(2025, 8, 16))

    assert early is not None and early.hourly_wage == Decimal("30.00")
    assert late is not None and late.hourly_wage == Decimal("35.00")
    assert boundary_low is not None and boundary_low.hourly_wage == Decimal("30.00")
    assert boundary_high is not None and boundary_high.hourly_wage == Decimal("35.00")


def test_resolver_returns_none_before_any_rate_takes_effect(session: Session):
    employee = _make_employee(session)
    employee_service.replace_rate_history(
        session, employee.id, [_rate("30.00", date(2025, 8, 1))], context=_context()
    )
    session.commit()

    assert employee_service.resolve_rate(employee, date(2025, 7, 31)) is None


def test_overlapping_submitted_rates_are_rejected(session: Session):
    employee = _make_employee(session)
    with pytest.raises(employee_service.OverlappingRates):
        employee_service.replace_rate_history(
            session,
            employee.id,
            [
                _rate("30.00", date(2025, 8, 1), date(2025, 8, 20)),
                _rate("35.00", date(2025, 8, 15)),
            ],
            context=_context(),
        )


def test_replacing_history_leaves_a_clean_chain(session: Session):
    """A second replace supersedes the first rather than appending, so no stale row lingers."""
    employee = _make_employee(session)
    employee_service.replace_rate_history(
        session, employee.id, [_rate("30.00", date(2025, 1, 1))], context=_context()
    )
    session.commit()
    employee_service.replace_rate_history(
        session, employee.id, [_rate("40.00", date(2025, 6, 1))], context=_context()
    )
    session.commit()

    session.refresh(employee)
    assert [rate.hourly_wage for rate in employee.rates] == [Decimal("40.00")]


# --------------------------------------------------------------------------- audit


def test_creation_writes_an_audit_row_per_field_with_the_passport_redacted(session: Session):
    """Requirement 3.9 and 3.10: every field change is audited, and the passport — a sensitive
    column — is redacted in the audit rather than written in plaintext."""
    employee = _make_employee(session)

    rows = list(
        session.scalars(
            select(ChangeLog).where(
                ChangeLog.entity_type == "employees", ChangeLog.entity_id == employee.id
            )
        )
    )
    fields = {row.field for row in rows}
    assert "full_name" in fields
    assert "start_date" in fields

    passport_row = next(row for row in rows if row.field == "passport_number")
    assert passport_row.new_value == REDACTED


def test_a_field_edit_writes_one_row_for_the_changed_field(session: Session):
    employee = _make_employee(session)
    before = list(
        session.scalars(select(ChangeLog).where(ChangeLog.entity_id == employee.id))
    )
    employee_service.update_employee(
        session, employee.id, EmployeeUpdate(position="Site Lead"), context=_context()
    )
    session.commit()

    after = list(session.scalars(select(ChangeLog).where(ChangeLog.entity_id == employee.id)))
    new_rows = [row for row in after if row not in before]
    assert [row.field for row in new_rows] == ["position"]
    assert new_rows[0].new_value == "Site Lead"
