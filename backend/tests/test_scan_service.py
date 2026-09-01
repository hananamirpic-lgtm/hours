"""Scan resolution at the service level (Requirement 9, 7.3, 7.4).

The HTTP tests in `test_scan_api.py` cover the wiring; here the subject is the state machine and the
rules the router only translates: that the server time is what is recorded and injectable so a test
can pin it, that `work_date` is the local date of check-in, that the duplicate window is measured
against the recorded time, that each guard raises its own error, and that a check-in writes an audit
row in the caller's transaction.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.change_log import ChangeLog
from app.models.client import Client
from app.models.employee import Employee, EmployeeStatus
from app.models.period_lock import PeriodLock
from app.models.setting import Setting, SettingValueType
from app.models.site import AssignmentMode, EmployeeSite, Site, SiteStatus
from app.models.time_entry import TimeEntry
from app.schemas.scan import ScanAction, ScanRequest
from app.services import scan as scan_service
from app.services.audit import AuditContext


def _context() -> AuditContext:
    return AuditContext(request_id="test-request")


_PASSPORT = iter(f"P{n:07d}" for n in range(1, 100000))


def _employee(session: Session, *, status: EmployeeStatus = EmployeeStatus.ACTIVE) -> Employee:
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
        status=status,
    )
    session.add(employee)
    session.commit()
    return employee


def _site(
    session: Session,
    *,
    number: str = "S-1",
    status: SiteStatus = SiteStatus.ACTIVE,
    assignment_mode: AssignmentMode = AssignmentMode.OPEN,
) -> Site:
    client = Client(name="Acme")
    session.add(client)
    session.flush()
    site = Site(
        name=f"Site {number}",
        site_number=number,
        client_id=client.id,
        status=status,
        assignment_mode=assignment_mode,
        qr_token=f"placeholder-{number}",
        qr_token_version=1,
    )
    session.add(site)
    session.commit()
    return site


def _seed_window(session: Session, seconds: int = 60) -> None:
    """Seed the duplicate-window setting the service reads (it falls back to 60 without it)."""
    session.add(
        Setting(
            key="duplicate_scan_window_seconds",
            value=str(seconds),
            value_type=SettingValueType.INTEGER,
        )
    )
    session.commit()


def _request(site: Site) -> ScanRequest:
    from app.core.qr_token import mint

    return ScanRequest(qr_token=mint(site.id, site.qr_token_version))


# --------------------------------------------------------------------------- check-in


def test_check_in_records_the_injected_server_time(session: Session):
    """Requirement 9.2: the recorded time is the server's, here pinned through `now`."""
    employee = _employee(session)
    site = _site(session)
    _seed_window(session)
    moment = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)

    outcome = scan_service.resolve_scan(
        session, employee_id=employee.id, request=_request(site), context=_context(), now=moment
    )
    session.commit()

    assert outcome.action is ScanAction.CHECK_IN
    assert outcome.entry.check_in_at == moment
    assert outcome.at == moment


def test_work_date_is_the_local_date_of_check_in(session: Session):
    """Requirement 10.4: a check-in near midnight UTC is attributed to its local calendar day.

    At 22:30 UTC on 9 March, Asia/Jerusalem is already 10 March (UTC+2), so the work date is the
    local day, not the UTC one.
    """
    employee = _employee(session)
    site = _site(session)
    _seed_window(session)
    moment = datetime(2025, 3, 9, 22, 30, tzinfo=UTC)

    outcome = scan_service.resolve_scan(
        session, employee_id=employee.id, request=_request(site), context=_context(), now=moment
    )
    session.commit()

    local = moment.astimezone(ZoneInfo("Asia/Jerusalem")).date()
    assert outcome.entry.work_date == local
    assert local == date(2025, 3, 10)


def test_check_in_writes_one_audit_row_in_the_same_transaction(session: Session):
    """Requirement 13.2: the entry's creation is audited, and the row shares the transaction."""
    employee = _employee(session)
    site = _site(session)
    _seed_window(session)

    scan_service.resolve_scan(
        session, employee_id=employee.id, request=_request(site), context=_context()
    )
    # Not yet committed: the audit rows live in this transaction. Flush so the query sees the pending
    # rows (the test session has autoflush off); they still share this uncommitted transaction, so a
    # rollback would take them with it.
    session.flush()
    rows = list(session.scalars(select(ChangeLog).where(ChangeLog.entity_type == "time_entries")))
    assert rows, "a check-in must leave an audit trail"
    assert all(row.reason == "scan_check_in" for row in rows)


# --------------------------------------------------------------------------- duplicate window


def test_a_repeat_inside_the_window_returns_the_same_entry(session: Session):
    """Requirement 9.3: an identical scan inside the window is a no-op returning the first entry."""
    employee = _employee(session)
    site = _site(session)
    _seed_window(session, seconds=60)
    first_moment = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)

    first = scan_service.resolve_scan(
        session, employee_id=employee.id, request=_request(site), context=_context(), now=first_moment
    )
    session.commit()

    # 30 seconds later, inside the 60s window.
    second = scan_service.resolve_scan(
        session,
        employee_id=employee.id,
        request=_request(site),
        context=_context(),
        now=first_moment + timedelta(seconds=30),
    )
    session.commit()

    assert second.action is ScanAction.DUPLICATE_IGNORED
    assert second.entry.id == first.entry.id
    assert len(list(session.scalars(select(TimeEntry)))) == 1


def test_a_repeat_outside_the_window_checks_out_the_open_shift(session: Session):
    """Outside the window the duplicate guard does not fire; a second scan at the site closes it.

    A second scan at the same site after the window is a check-out — the first shift is still open. This
    proves the window is not what suppresses the second scan, and that a same-site rescan now writes
    the close rather than merely deciding one.
    """
    employee = _employee(session)
    site = _site(session)
    _seed_window(session, seconds=60)
    first_moment = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)

    scan_service.resolve_scan(
        session, employee_id=employee.id, request=_request(site), context=_context(), now=first_moment
    )
    session.commit()

    second_moment = first_moment + timedelta(minutes=5)
    second = scan_service.resolve_scan(
        session,
        employee_id=employee.id,
        request=_request(site),
        context=_context(),
        now=second_moment,
    )
    assert second.action is ScanAction.CHECK_OUT
    assert second.entry.check_out_at == second_moment
    assert second.entry.total_minutes == 5


# --------------------------------------------------------------------------- guards


def test_inactive_employee_raises(session: Session):
    employee = _employee(session, status=EmployeeStatus.INACTIVE)
    site = _site(session)
    _seed_window(session)
    with pytest.raises(scan_service.EmployeeNotActive):
        scan_service.resolve_scan(
            session, employee_id=employee.id, request=_request(site), context=_context()
        )


def test_inactive_site_raises(session: Session):
    employee = _employee(session)
    site = _site(session, status=SiteStatus.COMPLETED)
    _seed_window(session)
    with pytest.raises(scan_service.SiteNotActive):
        scan_service.resolve_scan(
            session, employee_id=employee.id, request=_request(site), context=_context()
        )


def test_locked_period_raises(session: Session):
    employee = _employee(session)
    site = _site(session)
    _seed_window(session)
    moment = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)
    session.add(PeriodLock(year=2025, month=3, locked_at=datetime.now(UTC)))
    session.commit()
    with pytest.raises(scan_service.PeriodLocked):
        scan_service.resolve_scan(
            session, employee_id=employee.id, request=_request(site), context=_context(), now=moment
        )


def test_an_unlocked_period_row_does_not_block(session: Session):
    """A row that was locked and since unlocked leaves the month open."""
    employee = _employee(session)
    site = _site(session)
    _seed_window(session)
    moment = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)
    session.add(
        PeriodLock(
            year=2025,
            month=3,
            locked_at=datetime.now(UTC),
            unlocked_at=datetime.now(UTC),
            unlock_reason="correction",
        )
    )
    session.commit()

    outcome = scan_service.resolve_scan(
        session, employee_id=employee.id, request=_request(site), context=_context(), now=moment
    )
    assert outcome.action is ScanAction.CHECK_IN


def test_strict_site_rejects_unassigned(session: Session):
    employee = _employee(session)
    site = _site(session, assignment_mode=AssignmentMode.STRICT)
    _seed_window(session)
    with pytest.raises(scan_service.UnassignedSiteRejected):
        scan_service.resolve_scan(
            session, employee_id=employee.id, request=_request(site), context=_context()
        )


def test_strict_site_allows_an_assigned_employee(session: Session):
    """Requirement 7.4: strict mode admits an employee who is assigned to the site."""
    employee = _employee(session)
    site = _site(session, assignment_mode=AssignmentMode.STRICT)
    _seed_window(session)
    session.add(EmployeeSite(employee_id=employee.id, site_id=site.id, assigned_from=date(2025, 1, 1)))
    session.commit()

    outcome = scan_service.resolve_scan(
        session, employee_id=employee.id, request=_request(site), context=_context()
    )
    assert outcome.action is ScanAction.CHECK_IN
    assert scan_service.FLAG_UNASSIGNED_SITE not in outcome.entry.flags


def test_open_site_flags_unassigned(session: Session):
    """Requirement 7.3: open mode allows and flags an unassigned check-in."""
    employee = _employee(session)
    site = _site(session, assignment_mode=AssignmentMode.OPEN)
    _seed_window(session)

    outcome = scan_service.resolve_scan(
        session, employee_id=employee.id, request=_request(site), context=_context()
    )
    assert scan_service.FLAG_UNASSIGNED_SITE in outcome.entry.flags


# --------------------------------------------------------------------------- conflict and token


def test_open_shift_elsewhere_raises_naming_the_other_site(session: Session):
    """Requirement 11.4: a scan at a different open-shift site raises, carrying Site A."""
    employee = _employee(session)
    site_a = _site(session, number="S-A")
    site_b = _site(session, number="S-B")
    _seed_window(session)

    scan_service.resolve_scan(
        session, employee_id=employee.id, request=_request(site_a), context=_context()
    )
    session.commit()

    with pytest.raises(scan_service.OpenShiftElsewhere) as raised:
        scan_service.resolve_scan(
            session, employee_id=employee.id, request=_request(site_b), context=_context()
        )
    assert raised.value.other_site.id == site_a.id


def test_a_forged_token_raises_invalid_qr(session: Session):
    employee = _employee(session)
    _seed_window(session)
    with pytest.raises(scan_service.InvalidQr):
        scan_service.resolve_scan(
            session,
            employee_id=employee.id,
            request=ScanRequest(qr_token="site:forged.signature"),
            context=_context(),
        )


def test_a_stale_version_token_raises_invalid_qr(session: Session):
    """Requirement 8.6: a token minted at an old version is refused after regeneration."""
    from app.core.qr_token import mint

    employee = _employee(session)
    site = _site(session)
    _seed_window(session)
    stale = ScanRequest(qr_token=mint(site.id, site.qr_token_version))

    # Rotate the site's token version out from under the printed code.
    site.qr_token_version = 2
    session.commit()

    with pytest.raises(scan_service.InvalidQr):
        scan_service.resolve_scan(
            session, employee_id=employee.id, request=stale, context=_context()
        )


# --------------------------------------------------------------------------- status


def test_current_open_shift_is_none_without_one(session: Session):
    employee = _employee(session)
    assert scan_service.current_open_shift(session, employee.id) is None


def test_current_open_shift_returns_the_open_entry(session: Session):
    employee = _employee(session)
    site = _site(session)
    _seed_window(session)
    scan_service.resolve_scan(
        session, employee_id=employee.id, request=_request(site), context=_context()
    )
    session.commit()

    shift = scan_service.current_open_shift(session, employee.id)
    assert shift is not None
    assert shift.site_id == site.id


def test_a_caller_with_no_employee_scanning_raises(session: Session):
    with pytest.raises(scan_service.NoEmployeeForCaller):
        scan_service.resolve_scan(
            session,
            employee_id=None,
            request=ScanRequest(qr_token="site:whatever"),
            context=_context(),
        )


# --------------------------------------------------------------------------- check-out

# The implausible-shift threshold the check-out path reads (Requirement 10.5). Seeded here so a test
# can pin it; the service falls back to 16 hours without it.
def _seed_implausible_hours(session: Session, hours: int = 16) -> None:
    session.add(
        Setting(
            key="implausible_shift_hours",
            value=str(hours),
            value_type=SettingValueType.INTEGER,
        )
    )
    session.commit()


def _open_shift(session: Session, employee: Employee, site: Site, at: datetime) -> None:
    """Open a shift by scanning in at `at`, then commit."""
    scan_service.resolve_scan(
        session, employee_id=employee.id, request=_request(site), context=_context(), now=at
    )
    session.commit()


def test_check_out_writes_the_time_and_the_total_minutes(session: Session):
    """Requirement 10.1, 10.2: a same-site rescan closes the shift with whole-minute duration.

    Four hours twenty-nine minutes is 269 minutes, the brief's awkward duration — proving the total
    is whole-minute truncation from the UTC timestamps, not fractional hours.
    """
    employee = _employee(session)
    site = _site(session)
    _seed_window(session)
    _seed_implausible_hours(session)
    check_in = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)
    _open_shift(session, employee, site, check_in)

    check_out = check_in + timedelta(hours=4, minutes=29)
    outcome = scan_service.resolve_scan(
        session, employee_id=employee.id, request=_request(site), context=_context(), now=check_out
    )
    session.commit()

    assert outcome.action is ScanAction.CHECK_OUT
    assert outcome.at == check_out
    assert outcome.entry.check_out_at == check_out
    assert outcome.entry.total_minutes == 269
    assert scan_service.FLAG_IMPLAUSIBLE_DURATION not in outcome.entry.flags


def test_check_out_truncates_partial_minutes(session: Session):
    """Requirement 10.2: seconds are truncated, not rounded — 90 seconds is one whole minute."""
    employee = _employee(session)
    site = _site(session)
    _seed_window(session)
    _seed_implausible_hours(session)
    check_in = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)
    _open_shift(session, employee, site, check_in)

    outcome = scan_service.resolve_scan(
        session,
        employee_id=employee.id,
        request=_request(site),
        context=_context(),
        now=check_in + timedelta(seconds=90),
    )
    assert outcome.entry.total_minutes == 1


def test_a_shift_over_the_threshold_is_flagged_not_refused(session: Session):
    """Requirement 10.5: a 17-hour shift is accepted and flagged for review, not rejected."""
    employee = _employee(session)
    site = _site(session)
    _seed_window(session)
    _seed_implausible_hours(session, hours=16)
    check_in = datetime(2025, 3, 10, 6, 0, tzinfo=UTC)
    _open_shift(session, employee, site, check_in)

    outcome = scan_service.resolve_scan(
        session,
        employee_id=employee.id,
        request=_request(site),
        context=_context(),
        now=check_in + timedelta(hours=17),
    )
    session.commit()

    assert outcome.action is ScanAction.CHECK_OUT
    assert outcome.entry.total_minutes == 17 * 60
    assert scan_service.FLAG_IMPLAUSIBLE_DURATION in outcome.entry.flags


def test_a_shift_at_exactly_the_threshold_is_flagged(session: Session):
    """The threshold is inclusive: a shift of exactly 16 hours is flagged (Requirement 10.5)."""
    employee = _employee(session)
    site = _site(session)
    _seed_window(session)
    _seed_implausible_hours(session, hours=16)
    check_in = datetime(2025, 3, 10, 6, 0, tzinfo=UTC)
    _open_shift(session, employee, site, check_in)

    outcome = scan_service.resolve_scan(
        session,
        employee_id=employee.id,
        request=_request(site),
        context=_context(),
        now=check_in + timedelta(hours=16),
    )
    assert scan_service.FLAG_IMPLAUSIBLE_DURATION in outcome.entry.flags


def test_check_out_writes_an_audit_row_in_the_same_transaction(session: Session):
    """Requirement 13.2: closing an entry audits the change in the caller's transaction."""
    employee = _employee(session)
    site = _site(session)
    _seed_window(session)
    _seed_implausible_hours(session)
    check_in = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)
    _open_shift(session, employee, site, check_in)

    scan_service.resolve_scan(
        session,
        employee_id=employee.id,
        request=_request(site),
        context=_context(),
        now=check_in + timedelta(hours=2),
    )
    session.flush()

    rows = list(
        session.scalars(
            select(ChangeLog).where(
                ChangeLog.entity_type == "time_entries", ChangeLog.reason == "scan_check_out"
            )
        )
    )
    assert rows, "a check-out must leave an audit trail"
    fields = {row.field for row in rows}
    assert "check_out_at" in fields
    assert "total_minutes" in fields


# --------------------------------------------------------------------------- explicit check-out button


def test_explicit_check_out_closes_the_open_shift(session: Session):
    """Requirement 10.6: the button path closes the current open shift with no QR."""
    employee = _employee(session)
    site = _site(session)
    _seed_window(session)
    _seed_implausible_hours(session)
    check_in = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)
    _open_shift(session, employee, site, check_in)

    check_out = check_in + timedelta(hours=3)
    outcome = scan_service.check_out(
        session, employee_id=employee.id, context=_context(), now=check_out
    )
    session.commit()

    assert outcome.action is ScanAction.CHECK_OUT
    assert outcome.entry.site_id == site.id
    assert outcome.entry.check_out_at == check_out
    assert outcome.entry.total_minutes == 180


def test_explicit_check_out_without_an_open_shift_is_rejected(session: Session):
    """Requirement 10.6: checking out with nothing open is refused with no_open_shift."""
    employee = _employee(session)
    with pytest.raises(scan_service.NoOpenShift):
        scan_service.check_out(session, employee_id=employee.id, context=_context())


def test_check_out_not_after_check_in_is_rejected(session: Session):
    """Requirement 10.3: a check-out at or before the check-in is refused, leaving the shift open."""
    employee = _employee(session)
    site = _site(session)
    _seed_window(session)
    _seed_implausible_hours(session)
    check_in = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)
    _open_shift(session, employee, site, check_in)

    with pytest.raises(scan_service.CheckOutNotAfterCheckIn):
        scan_service.check_out(
            session, employee_id=employee.id, context=_context(), now=check_in
        )

    still_open = scan_service.current_open_shift(session, employee.id)
    assert still_open is not None
    assert still_open.check_out_at is None


def test_explicit_check_out_with_no_employee_raises(session: Session):
    with pytest.raises(scan_service.NoEmployeeForCaller):
        scan_service.check_out(session, employee_id=None, context=_context())


# --------------------------------------------------------------------------- transition


def test_transition_closes_site_a_and_opens_site_b_at_the_same_instant(session: Session):
    """Requirement 11.5: the transition closes the open shift and opens the target in one go.

    The close and the open meet at one instant — the server time of the transition — so the day has
    no gap and no overlap at the seam.
    """
    from app.models.time_entry import TimeEntrySource

    employee = _employee(session)
    site_a = _site(session, number="S-A")
    site_b = _site(session, number="S-B")
    _seed_window(session)
    _seed_implausible_hours(session)
    check_in = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)
    _open_shift(session, employee, site_a, check_in)

    at = check_in + timedelta(hours=2)
    outcome = scan_service.transition(
        session, employee_id=employee.id, request=_request(site_b), context=_context(), now=at
    )
    session.commit()

    # The new open shift is at Site B, opened at the transition instant.
    assert outcome.action is ScanAction.CHECK_IN
    assert outcome.entry.site_id == site_b.id
    assert outcome.entry.check_in_at == at
    assert outcome.entry.check_out_at is None

    # Reload everything so both entries come back from the store consistently (SQLite hands
    # timestamps back naive; comparing a reloaded row to a live in-memory one would mismatch on zone).
    session.expire_all()
    entries = list(session.scalars(select(TimeEntry).order_by(TimeEntry.check_in_at)))
    assert len(entries) == 2
    closed, opened = entries
    # Site A is closed at the same instant Site B opens: no gap, no overlap.
    assert closed.site_id == site_a.id
    assert closed.check_out_at is not None
    assert closed.total_minutes == 120
    assert opened.check_in_at == closed.check_out_at
    # Exactly one open entry remains.
    assert sum(1 for e in entries if e.check_out_at is None) == 1
    # The closed entry is marked a system transition (Requirement 11.8).
    assert closed.source is TimeEntrySource.SYSTEM_TRANSITION


def test_transition_audits_the_system_close(session: Session):
    """Requirement 11.8: the closed entry is system-closed with the transition as its stated reason."""
    employee = _employee(session)
    site_a = _site(session, number="S-A")
    site_b = _site(session, number="S-B")
    _seed_window(session)
    _seed_implausible_hours(session)
    check_in = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)
    _open_shift(session, employee, site_a, check_in)

    scan_service.transition(
        session,
        employee_id=employee.id,
        request=_request(site_b),
        context=_context(),
        now=check_in + timedelta(hours=2),
    )
    session.flush()

    close_rows = list(
        session.scalars(
            select(ChangeLog).where(
                ChangeLog.entity_type == "time_entries",
                ChangeLog.reason == "scan_transition_close",
            )
        )
    )
    assert close_rows, "the system close must leave an audit trail explaining the transition"
    fields = {row.field for row in close_rows}
    # The trail explains both that the entry was closed and that it became a system transition.
    assert "check_out_at" in fields
    assert "source" in fields
    source_row = next(row for row in close_rows if row.field == "source")
    assert source_row.new_value == "system_transition"


def test_transition_with_no_open_shift_is_a_plain_check_in(session: Session):
    """With nothing open, a transition opens the target rather than inventing a close."""
    employee = _employee(session)
    site_b = _site(session, number="S-B")
    _seed_window(session)

    outcome = scan_service.transition(
        session, employee_id=employee.id, request=_request(site_b), context=_context()
    )
    session.commit()

    assert outcome.action is ScanAction.CHECK_IN
    assert outcome.entry.site_id == site_b.id
    assert len(list(session.scalars(select(TimeEntry)))) == 1


def test_transition_to_the_same_site_is_rejected(session: Session):
    """A transition to the site already open is a check-out, not a move; it is refused."""
    employee = _employee(session)
    site_a = _site(session, number="S-A")
    _seed_window(session)
    _seed_implausible_hours(session)
    _open_shift(session, employee, site_a, datetime(2025, 3, 10, 8, 0, tzinfo=UTC))

    with pytest.raises(scan_service.SameSiteTransition):
        scan_service.transition(
            session, employee_id=employee.id, request=_request(site_a), context=_context()
        )


def test_transition_into_an_inactive_site_is_refused_and_rolls_back(session: Session):
    """The opening half runs the check-in guards; a refused open must not leave Site A closed.

    The service raises and never commits; the router rolls back. Here we assert the raise and, after
    rolling back, that Site A is still the single open shift — the transaction was atomic.
    """
    employee = _employee(session)
    site_a = _site(session, number="S-A")
    site_b = _site(session, number="S-B", status=SiteStatus.COMPLETED)
    _seed_window(session)
    _seed_implausible_hours(session)
    _open_shift(session, employee, site_a, datetime(2025, 3, 10, 8, 0, tzinfo=UTC))

    with pytest.raises(scan_service.SiteNotActive):
        scan_service.transition(
            session,
            employee_id=employee.id,
            request=_request(site_b),
            context=_context(),
            now=datetime(2025, 3, 10, 10, 0, tzinfo=UTC),
        )
    session.rollback()

    still_open = scan_service.current_open_shift(session, employee.id)
    assert still_open is not None
    assert still_open.site_id == site_a.id
    assert still_open.check_out_at is None


def test_transition_with_no_employee_raises(session: Session):
    site_b = _site(session, number="S-B")
    with pytest.raises(scan_service.NoEmployeeForCaller):
        scan_service.transition(
            session, employee_id=None, request=_request(site_b), context=_context()
        )


def test_transition_with_a_forged_token_raises_before_closing(session: Session):
    """A bad target token fails before the open shift is touched, so Site A stays open."""
    employee = _employee(session)
    site_a = _site(session, number="S-A")
    _seed_window(session)
    _seed_implausible_hours(session)
    _open_shift(session, employee, site_a, datetime(2025, 3, 10, 8, 0, tzinfo=UTC))

    with pytest.raises(scan_service.InvalidQr):
        scan_service.transition(
            session,
            employee_id=employee.id,
            request=ScanRequest(qr_token="site:forged.signature"),
            context=_context(),
        )
    session.rollback()

    still_open = scan_service.current_open_shift(session, employee.id)
    assert still_open is not None
    assert still_open.site_id == site_a.id


# --------------------------------------------------------------------------- end work and move


def test_end_work_and_move_closes_without_opening_a_new_shift(session: Session):
    """Requirement 11.6: the move action closes the current entry with no departure QR, opening none."""
    from app.models.time_entry import TimeEntrySource

    employee = _employee(session)
    site_a = _site(session, number="S-A")
    _seed_window(session)
    _seed_implausible_hours(session)
    check_in = datetime(2025, 3, 10, 8, 0, tzinfo=UTC)
    _open_shift(session, employee, site_a, check_in)

    at = check_in + timedelta(hours=3)
    outcome = scan_service.end_work_and_move(
        session, employee_id=employee.id, context=_context(), now=at
    )
    session.commit()

    assert outcome.action is ScanAction.CHECK_OUT
    assert outcome.entry.check_out_at == at
    assert outcome.entry.total_minutes == 180
    assert outcome.entry.source is TimeEntrySource.SYSTEM_TRANSITION
    # No new shift was opened.
    assert scan_service.current_open_shift(session, employee.id) is None


def test_end_work_and_move_without_an_open_shift_is_rejected(session: Session):
    """Requirement 11.6: with nothing open there is nothing to end."""
    employee = _employee(session)
    with pytest.raises(scan_service.NoOpenShift):
        scan_service.end_work_and_move(session, employee_id=employee.id, context=_context())


def test_a_three_site_day_totals_correctly(session: Session):
    """Requirement 11.1, 11.2: two transitions across three sites sum to the day's real minutes.

    07:00→11:30 at A, →12:00 at B... rather: A 07:00–09:00 (120), B 09:00–12:00 (180), C 12:00–17:00
    (300) — the day is 600 minutes, split across three sites with no gap and no overlap.
    """
    employee = _employee(session)
    site_a = _site(session, number="S-A")
    site_b = _site(session, number="S-B")
    site_c = _site(session, number="S-C")
    _seed_window(session)
    _seed_implausible_hours(session)

    start = datetime(2025, 3, 10, 7, 0, tzinfo=UTC)
    _open_shift(session, employee, site_a, start)

    scan_service.transition(
        session,
        employee_id=employee.id,
        request=_request(site_b),
        context=_context(),
        now=start + timedelta(hours=2),  # 09:00
    )
    session.commit()
    scan_service.transition(
        session,
        employee_id=employee.id,
        request=_request(site_c),
        context=_context(),
        now=start + timedelta(hours=5),  # 12:00
    )
    session.commit()
    # Close the last site at 17:00.
    scan_service.check_out(
        session, employee_id=employee.id, context=_context(), now=start + timedelta(hours=10)
    )
    session.commit()

    entries = list(session.scalars(select(TimeEntry).order_by(TimeEntry.check_in_at)))
    assert len(entries) == 3
    totals = [e.total_minutes for e in entries]
    assert totals == [120, 180, 300]
    assert sum(totals) == 600
    # No open entry left, and each entry's close is the next one's open — no gap, no overlap.
    assert all(e.check_out_at is not None for e in entries)
    assert entries[0].check_out_at == entries[1].check_in_at
    assert entries[1].check_out_at == entries[2].check_in_at


def test_a_close_overlapping_an_existing_entry_is_rejected_naming_it(session: Session):
    """Requirement 11.7: application-level overlap guard names the conflicting entry.

    On SQLite there is no exclusion constraint, so this exercises the `_overlapping_completed_entry`
    lookup that names the conflict; the database backstop is proven in the integration suite. A
    completed entry sits 10:00–12:00; an open shift from 09:00 is then closed at 11:00, which would
    overlap it.
    """
    from app.models.time_entry import TimeEntrySource as _Src

    employee = _employee(session)
    site_a = _site(session, number="S-A")
    site_b = _site(session, number="S-B")
    _seed_window(session)
    _seed_implausible_hours(session)

    # A pre-existing completed entry 10:00–12:00 at Site B.
    existing = TimeEntry(
        employee_id=employee.id,
        site_id=site_b.id,
        work_date=date(2025, 3, 10),
        check_in_at=datetime(2025, 3, 10, 10, 0, tzinfo=UTC),
        check_out_at=datetime(2025, 3, 10, 12, 0, tzinfo=UTC),
        total_minutes=120,
        source=_Src.QR_SCAN,
        flags=[],
    )
    session.add(existing)
    session.commit()

    # An open shift at Site A from 09:00, closed at 11:00 — overlaps the existing entry.
    open_entry = TimeEntry(
        employee_id=employee.id,
        site_id=site_a.id,
        work_date=date(2025, 3, 10),
        check_in_at=datetime(2025, 3, 10, 9, 0, tzinfo=UTC),
        source=_Src.QR_SCAN,
        flags=[],
    )
    session.add(open_entry)
    session.commit()

    with pytest.raises(scan_service.OverlapRejected) as raised:
        scan_service._check_out(
            session,
            entry=open_entry,
            moment=datetime(2025, 3, 10, 11, 0, tzinfo=UTC),
            context=_context(),
        )
    assert raised.value.conflicting_entry is not None
    assert raised.value.conflicting_entry.id == existing.id
