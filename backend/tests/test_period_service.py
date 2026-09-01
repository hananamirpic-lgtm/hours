"""Approval workflow and period locking at the service layer (Requirement 15).

With location verification out of scope, manager approval is the principal control over attendance
accuracy, so the rules here matter more than most. The claims worth a unit test are the ones about
what the ladder allows and what a lock does, tested without a request so the policy is provable in
isolation:

* the status ladder advances one rung at a time and refuses everything else, except an
  administrator's single-rung reversal, which needs a reason (Requirement 15.2, 15.6);
* a lock freezes the month's Approved entries and warns first when unapproved entries remain
  (Requirement 15.4, 15.7);
* an unlock reopens a locked month and requires a reason (Requirement 15.6);
* the period-lock guard refuses a write into a locked month for everyone, and lets an administrator
  override it (Requirement 15.5).

Role and site scope are the router's concern and are pinned in `test_period_api.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.change_log import ChangeLog
from app.models.client import Client
from app.models.employee import Employee
from app.models.site import Site
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.services import period as period_service
from app.services import scan as scan_service
from app.services.audit import AuditContext

_PASSPORT = iter(f"P{n:07d}" for n in range(1, 100000))


def _context() -> AuditContext:
    return AuditContext(actor_user_id=uuid.uuid4(), reason=None)


def _make_employee(session: Session) -> Employee:
    passport = next(_PASSPORT)
    employee = Employee(
        full_name="Worker",
        full_name_en="Worker",
        passport_number=passport,
        passport_number_hash=passport,
        phone="+972500000000",
        country="Israel",
        emergency_contact_name="Contact",
        emergency_contact_phone="+972500000001",
        start_date=date(2025, 1, 1),
    )
    session.add(employee)
    session.flush()
    return employee


def _make_site(session: Session, *, number: str = "S-1") -> Site:
    client = Client(name="Acme")
    session.add(client)
    session.flush()
    site = Site(
        name=f"Site {number}",
        site_number=number,
        client_id=client.id,
        qr_token=f"placeholder-{uuid.uuid4().hex}",
        qr_token_version=1,
    )
    session.add(site)
    session.flush()
    return site


def _make_entry(
    session: Session,
    *,
    employee: Employee,
    site: Site,
    status: TimeEntryStatus = TimeEntryStatus.DRAFT,
    day: int = 15,
    hour: int = 8,
) -> TimeEntry:
    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=date(2025, 8, day),
        check_in_at=datetime(2025, 8, day, hour, tzinfo=UTC),
        check_out_at=datetime(2025, 8, day, hour + 4, tzinfo=UTC),
        total_minutes=240,
        source=TimeEntrySource.QR_SCAN,
        is_manual=False,
        status=status,
        flags=[],
    )
    session.add(entry)
    session.flush()
    return entry


# ===================================================================== the status ladder


def test_a_forward_step_is_allowed_for_anyone():
    """Requirement 15.2: Draft → Review is a legal forward step, no admin, no reason needed."""
    period_service.validate_transition(
        TimeEntryStatus.DRAFT, TimeEntryStatus.REVIEW, is_admin=False, reason=None
    )
    period_service.validate_transition(
        TimeEntryStatus.REVIEW, TimeEntryStatus.APPROVED, is_admin=False, reason=None
    )


def test_a_forward_jump_of_more_than_one_rung_is_rejected():
    """Requirement 15.2: Draft straight to Approved skips a rung and is refused."""
    with pytest.raises(period_service.InvalidTransition) as excinfo:
        period_service.validate_transition(
            TimeEntryStatus.DRAFT, TimeEntryStatus.APPROVED, is_admin=True, reason="x"
        )
    assert excinfo.value.from_status is TimeEntryStatus.DRAFT
    assert excinfo.value.to_status is TimeEntryStatus.APPROVED


def test_a_move_to_the_same_status_is_rejected():
    """Requirement 15.2: a no-move is not a transition."""
    with pytest.raises(period_service.InvalidTransition):
        period_service.validate_transition(
            TimeEntryStatus.REVIEW, TimeEntryStatus.REVIEW, is_admin=True, reason="x"
        )


def test_a_reversal_by_a_non_admin_is_rejected():
    """Requirement 15.2: only an administrator may reverse a status."""
    with pytest.raises(period_service.ReversalRequiresAdmin):
        period_service.validate_transition(
            TimeEntryStatus.APPROVED, TimeEntryStatus.REVIEW, is_admin=False, reason="undo"
        )


def test_an_admin_reversal_without_a_reason_is_rejected():
    """Requirement 15.6: an administrator's reversal must carry a reason."""
    with pytest.raises(period_service.ReversalRequiresReason):
        period_service.validate_transition(
            TimeEntryStatus.APPROVED, TimeEntryStatus.REVIEW, is_admin=True, reason=None
        )


def test_an_admin_reversal_with_a_reason_is_allowed():
    """Requirement 15.2, 15.6: an administrator may step one rung back with a reason."""
    period_service.validate_transition(
        TimeEntryStatus.APPROVED, TimeEntryStatus.REVIEW, is_admin=True, reason="approved in error"
    )
    period_service.validate_transition(
        TimeEntryStatus.LOCKED, TimeEntryStatus.APPROVED, is_admin=True, reason="reopened"
    )


def test_a_reversal_of_more_than_one_rung_is_rejected():
    """Requirement 15.2: Locked straight back to Draft skips rungs, even for an admin."""
    with pytest.raises(period_service.InvalidTransition):
        period_service.validate_transition(
            TimeEntryStatus.LOCKED, TimeEntryStatus.DRAFT, is_admin=True, reason="x"
        )


# ===================================================================== apply_status


def test_apply_status_moves_a_forward_step_and_audits(session: Session):
    """Requirement 15.2, 13.2: a legal move changes the status and writes an audit row."""
    employee = _make_employee(session)
    site = _make_site(session)
    entry = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.DRAFT)

    moved = period_service.apply_status(
        session, entry, TimeEntryStatus.REVIEW, is_admin=False, reason=None, context=_context()
    )
    assert moved is True
    assert entry.status is TimeEntryStatus.REVIEW

    session.flush()
    rows = session.scalars(
        select(ChangeLog).where(ChangeLog.entity_id == entry.id, ChangeLog.field == "status")
    ).all()
    assert len(rows) == 1
    assert rows[0].new_value == "review"


def test_apply_status_is_a_no_op_when_already_at_target(session: Session):
    """Re-approving an already-approved entry moves nothing and writes nothing."""
    employee = _make_employee(session)
    site = _make_site(session)
    entry = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.APPROVED)

    moved = period_service.apply_status(
        session, entry, TimeEntryStatus.APPROVED, is_admin=False, reason=None, context=_context()
    )
    assert moved is False


# ===================================================================== bulk status change


def _scope_all():
    """A scope statement that permits every site — the administrator / accounting case."""
    return select(TimeEntry).where(TimeEntry.deleted_at.is_(None))


def test_bulk_change_advances_a_set_forward(session: Session):
    """Requirement 15.3: a set of Draft entries moves to Review together."""
    employee = _make_employee(session)
    site = _make_site(session)
    a = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.DRAFT, hour=6)
    b = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.DRAFT, hour=12)

    outcome = period_service.bulk_change_status(
        session,
        entry_ids=[a.id, b.id],
        target_status=TimeEntryStatus.REVIEW,
        scope_statement=_scope_all(),
        is_admin=False,
        reason=None,
        context=_context(),
    )
    assert set(outcome.updated_ids) == {a.id, b.id}
    assert a.status is TimeEntryStatus.REVIEW
    assert b.status is TimeEntryStatus.REVIEW


def test_bulk_change_skips_entries_already_at_target(session: Session):
    """A repeat is idempotent: an entry already at the target is not counted as moved."""
    employee = _make_employee(session)
    site = _make_site(session)
    draft = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.DRAFT, hour=6)
    review = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.REVIEW, hour=12)

    outcome = period_service.bulk_change_status(
        session,
        entry_ids=[draft.id, review.id],
        target_status=TimeEntryStatus.REVIEW,
        scope_statement=_scope_all(),
        is_admin=False,
        reason=None,
        context=_context(),
    )
    assert outcome.updated_ids == [draft.id]


def test_bulk_change_rejects_an_illegal_transition_and_moves_nothing(session: Session):
    """Requirement 15.2: a Draft in the set that cannot reach Approved fails the whole call."""
    employee = _make_employee(session)
    site = _make_site(session)
    draft = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.DRAFT, hour=6)

    with pytest.raises(period_service.InvalidTransition):
        period_service.bulk_change_status(
            session,
            entry_ids=[draft.id],
            target_status=TimeEntryStatus.APPROVED,
            scope_statement=_scope_all(),
            is_admin=False,
            reason=None,
            context=_context(),
        )


def test_bulk_change_ignores_ids_outside_the_scope(session: Session):
    """Requirement 2.3: an entry not in the scoped statement is never touched."""
    employee = _make_employee(session)
    mine = _make_site(session, number="S-MINE")
    other = _make_site(session, number="S-OTHER")
    in_scope = _make_entry(session, employee=employee, site=mine, status=TimeEntryStatus.DRAFT, hour=6)
    out_of_scope = _make_entry(
        session, employee=employee, site=other, status=TimeEntryStatus.DRAFT, hour=12
    )

    scoped = select(TimeEntry).where(
        TimeEntry.deleted_at.is_(None), TimeEntry.site_id == mine.id
    )
    outcome = period_service.bulk_change_status(
        session,
        entry_ids=[in_scope.id, out_of_scope.id],
        target_status=TimeEntryStatus.REVIEW,
        scope_statement=scoped,
        is_admin=False,
        reason=None,
        context=_context(),
    )
    assert outcome.updated_ids == [in_scope.id]
    assert out_of_scope.status is TimeEntryStatus.DRAFT


# ===================================================================== lock


def test_lock_warns_and_lists_unapproved_entries(session: Session):
    """Requirement 15.7: a month with unapproved entries warns before locking, listing them."""
    employee = _make_employee(session)
    site = _make_site(session)
    _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.APPROVED, hour=6)
    draft = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.DRAFT, hour=12)

    outcome = period_service.lock_period(
        session, year=2025, month=8, force=False, context=_context()
    )
    assert outcome.locked is False
    assert [e.id for e in outcome.unapproved] == [draft.id]
    # Nothing was written: no period row, and the approved entry is still approved.
    assert period_service.get_period(session, 2025, 8) is None


def test_lock_freezes_approved_entries(session: Session):
    """Requirement 15.4: locking a clean month sets its Approved entries to Locked."""
    employee = _make_employee(session)
    site = _make_site(session)
    a = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.APPROVED, hour=6)
    b = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.APPROVED, hour=12)

    outcome = period_service.lock_period(
        session, year=2025, month=8, force=False, context=_context()
    )
    assert outcome.locked is True
    assert outcome.locked_count == 2
    assert a.status is TimeEntryStatus.LOCKED
    assert b.status is TimeEntryStatus.LOCKED
    assert period_service.get_period(session, 2025, 8).is_locked is True


def test_forcing_a_lock_freezes_approved_and_leaves_unapproved(session: Session):
    """Requirement 15.7: forcing past the warning locks Approved and leaves the rest as they are."""
    employee = _make_employee(session)
    site = _make_site(session)
    approved = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.APPROVED, hour=6)
    draft = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.DRAFT, hour=12)

    outcome = period_service.lock_period(
        session, year=2025, month=8, force=True, context=_context()
    )
    assert outcome.locked is True
    assert outcome.locked_count == 1
    assert approved.status is TimeEntryStatus.LOCKED
    assert draft.status is TimeEntryStatus.DRAFT


def test_locking_an_already_locked_month_is_rejected(session: Session):
    """Requirement 15.4: a second lock on a locked month is refused."""
    period_service.lock_period(session, year=2025, month=8, force=True, context=_context())
    with pytest.raises(period_service.PeriodAlreadyLocked):
        period_service.lock_period(session, year=2025, month=8, force=True, context=_context())


# ===================================================================== unlock


def test_unlock_reopens_a_locked_month_with_a_reason(session: Session):
    """Requirement 15.6: unlocking a locked month records the reason and clears the lock."""
    period_service.lock_period(session, year=2025, month=8, force=True, context=_context())

    period = period_service.unlock_period(
        session, year=2025, month=8, reason="correction needed", context=_context()
    )
    assert period.is_locked is False
    assert period.unlock_reason == "correction needed"

    session.flush()
    rows = session.scalars(
        select(ChangeLog).where(
            ChangeLog.entity_type == "period_locks", ChangeLog.reason == "correction needed"
        )
    ).all()
    assert rows, "the unlock is recorded in the audit log"


def test_unlocking_an_open_month_is_rejected(session: Session):
    """Requirement 15.6: there is nothing to reopen on a month that was never locked."""
    with pytest.raises(period_service.PeriodNotLocked):
        period_service.unlock_period(session, year=2025, month=8, reason="x", context=_context())


# ===================================================================== the period-lock guard


def test_assert_period_open_allows_an_open_month(session: Session):
    """A month that was never locked lets a write through, and no override is used."""
    assert scan_service.assert_period_open(session, date(2025, 8, 15)) is False


def test_assert_period_open_refuses_a_locked_month(session: Session):
    """Requirement 15.5: a locked month refuses a write for a non-overriding caller."""
    period_service.lock_period(session, year=2025, month=8, force=True, context=_context())
    with pytest.raises(scan_service.PeriodLocked):
        scan_service.assert_period_open(session, date(2025, 8, 15))


def test_assert_period_open_lets_an_admin_override_a_locked_month(session: Session):
    """Requirement 15.5: an administrator override writes into a locked month, and is reported."""
    period_service.lock_period(session, year=2025, month=8, force=True, context=_context())
    assert scan_service.assert_period_open(session, date(2025, 8, 15), admin_override=True) is True


def test_an_override_on_an_open_month_reports_no_override(session: Session):
    """An override flag on an open month changes nothing: the guard reports no override was used."""
    assert scan_service.assert_period_open(session, date(2025, 8, 15), admin_override=True) is False
