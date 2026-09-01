"""Scan resolution: the state machine behind `POST /api/scans` (Requirement 9, 7.3, 7.4).

One endpoint handles the whole attendance flow, because the decision — check-in, check-out, or
conflict — has to be made server-side with a lock held, or two scans racing each other could each
believe they are the first and both open a shift. The design's "Scan resolution (unified QR)" section
is the specification this file implements:

    1. Verify the token → a site, at the site's current version.
    2. Lock the employee row (SELECT ... FOR UPDATE) — serialises concurrent scans by one person.
    3. If an identical scan by this employee succeeded inside the duplicate window, return that
       result unchanged (Requirement 9.3).
    4. Load the employee's open entry, if any.
    5. Decide:
         no open entry                  → check in at this site,
         open entry at this site        → check out (Task 17 writes the totals; this task returns
                                           the conflict/decision and leaves the close to that task),
         open entry at a different site → 409 open_shift_elsewhere (Requirement 11.4).
    6. Guards before writing: employee active, site active, period not locked, assignment mode.
    7. Write, audit, and return the site, the action and the server time.

The service owns every rule and never commits — the router's unit of work decides the fate of the
change and its audit rows together, the invariant `app.services.audit` rests on. The one-open-entry
database index and the overlap exclusion constraint are the backstop the lock and these checks sit in
front of; on PostgreSQL a race the lock somehow lost still cannot produce two open entries.

**Scope.** Task 16 delivered check-in, the status query, the duplicate window, the four guards, and
the 409 when a shift is open at a different site. Task 17 added the check-out *write*: a scan at the
site of the open shift sets `check_out_at` to the server time and `total_minutes` to the whole
minutes worked, rejecting a check-out that is not after its check-in and a check-out with no open
shift, and flagging a shift longer than the configured implausible-duration threshold for manager
review rather than accepting it silently (Requirement 10.1–10.6).

Task 18 adds the confirmed transition and the overlap backstop (Requirement 11.5–11.8). A scan at a
*different* site still returns the `open_shift_elsewhere` conflict rather than an automatic close —
silently closing a shift at another site on an ambiguous scan would create hours nobody authorised
(Requirement 11.4). When the employee confirms, `transition` closes the open entry at the server
time and opens a new one at the target site in *one* transaction, marking the closed entry
`system_transition` and attributing the close to the system in the audit trail (Requirement 11.5,
11.8). `end_work_and_move` is the closing half of that flow with no target: the "End work and move to
another site" action that closes the current entry without a departure QR (Requirement 11.6). Every
write that would put two completed entries for one employee over the same minute is caught at the
database exclusion constraint and re-raised as a domain error that names the conflicting entry, so a
race the lock did not serialise or a manual write that overlaps is refused rather than paid twice
(Requirement 11.7).

The check-out duration is computed by `app.calculations.hours.whole_minutes`, the same truncation
the daily classification uses, so a day's stored `total_minutes` and the buckets `classify_day`
splits it into agree to the minute.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.calculations.hours import whole_minutes
from app.core.config import get_settings
from app.core.qr_token import InvalidToken, SiteToken, parse
from app.models.employee import Employee
from app.models.period_lock import PeriodLock
from app.models.site import AssignmentMode, EmployeeSite, Site
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.schemas.scan import ScanAction, ScanRequest
from app.services import settings as settings_service
from app.services.audit import AuditContext, record_model_changes, snapshot

#: The anomaly flag a check-in at a site the employee is not assigned to carries (Requirement 7.3).
FLAG_UNASSIGNED_SITE = "unassigned_site"

#: The anomaly flag a check-out whose duration exceeds the implausible-shift threshold carries. A
#: forgotten check-out inflates a day silently; the flag surfaces it for a manager to correct rather
#: than paying or billing the inflated hours (Requirement 10.5, design "Implausible-duration flag").
FLAG_IMPLAUSIBLE_DURATION = "implausible_duration"

#: Default duplicate window when the setting is absent, matching the seeded default (Requirement 9.3).
_DEFAULT_DUPLICATE_WINDOW_SECONDS = 60

#: Default implausible-shift threshold in hours when the setting is absent, matching the seeded
#: default (Requirement 10.5). A shift at least this long is flagged, not refused.
_DEFAULT_IMPLAUSIBLE_SHIFT_HOURS = 16


# --------------------------------------------------------------------------- errors
# Domain errors, not HTTP errors; each carries a machine `code` the router lifts into the error
# envelope, the same shape the other services use.


class ScanError(Exception):
    """Base for every scan failure. `code` is what the front end translates."""

    code = "scan_error"


class InvalidQr(ScanError):
    """The token was unknown, malformed, wrongly signed, or names a version the site has moved past.

    One error for every cause on purpose (Requirement 8.7): a scanner is never told *why* a token was
    refused, only that it was, so a forgery oracle cannot be built from the responses.
    """

    code = "invalid_qr"


class NoEmployeeForCaller(ScanError):
    """The caller's login is not linked to an employee, so it cannot scan.

    Only the employee role carries an `employee_id`; a manager or accounting login has none and has
    no attendance of its own to record.
    """

    code = "no_employee_for_caller"


class EmployeeNotActive(ScanError):
    """The employee's status is not Active, so no new check-in is allowed (Requirement 3.7, 9.4)."""

    code = "employee_not_active"


class SiteNotActive(ScanError):
    """The site's status is not Active, so no new check-in is allowed there (Requirement 6.5, 9.4)."""

    code = "site_not_active"


class PeriodLocked(ScanError):
    """The month the scan falls in is locked, so no entry may be written (Requirement 9.5, 15.5)."""

    code = "period_locked"


class UnassignedSiteRejected(ScanError):
    """A strict-mode site refused a check-in by an employee not assigned to it (Requirement 7.4)."""

    code = "unassigned_site_rejected"


class OpenShiftElsewhere(ScanError):
    """The employee has an open shift at a *different* site (Requirement 11.4).

    Carries the other site so the router can name it and offer the transition-or-cancel choice. This
    is deliberately not an automatic transition: silently closing a shift at another site on an
    ambiguous scan would create hours nobody authorised, so the employee confirms and Task 18's
    `POST /api/scans/transition` does the close-and-open in one transaction.
    """

    code = "open_shift_elsewhere"

    def __init__(self, open_entry: TimeEntry, other_site: Site) -> None:
        super().__init__(f"open shift at site {other_site.id}")
        self.open_entry = open_entry
        self.other_site = other_site


class NoOpenShift(ScanError):
    """A check-out was attempted with no open shift to close (Requirement 10.6).

    Raised by the explicit check-out button path (`POST /api/scans/checkout`), where there is no QR to
    resolve to a site and the only thing to close is the caller's current open entry — so if none is
    open the request cannot be satisfied and the caller is told plainly that no shift is open. The
    unified scan path never reaches this: a scan with no open entry is a *check-in*, not a failed
    check-out.
    """

    code = "no_open_shift"


class CheckOutNotAfterCheckIn(ScanError):
    """The check-out instant is not strictly after the check-in (Requirement 10.3).

    The server clock is monotonic in practice, so this guards a pinned time in a test and the pathological
    case of a clock stepping backwards, rather than anything a normal scan produces. It matches the
    database `check_out_after_check_in` check, so the write is refused in the service before it would be
    refused by the constraint, with a machine code the front end can translate.
    """

    code = "check_out_not_after_check_in"


class SameSiteTransition(ScanError):
    """A transition was confirmed to the site the open shift is already at (Requirement 11.5).

    Transition means *move* — close one site and open another. Confirming a transition to the same
    site is a scan at that site, which is a check-out, not a transition; the two would collide (the
    close and the re-open would share the same instant on the same site), so it is refused and the
    caller is told to scan normally instead.
    """

    code = "same_site_transition"


class OverlapRejected(ScanError):
    """A write would put two completed entries for this employee over the same minute (Requirement 11.7).

    The database exclusion constraint is the authority — two completed entries for one employee may
    never overlap, because being paid at two sites for the same minute is the one thing the whole
    invariant exists to prevent. This wraps the `IntegrityError` the constraint raises so the caller
    gets a translatable code and, where it could be identified, the conflicting entry named, rather
    than a raw database error. It also catches the one-open-entry index: a race that opened a second
    shift the row lock did not serialise surfaces here as the same domain refusal.
    """

    code = "overlap_rejected"

    def __init__(self, message: str, *, conflicting_entry: TimeEntry | None = None) -> None:
        super().__init__(message)
        self.conflicting_entry = conflicting_entry


#: Fragments of the constraint names the database raises, matched against the `IntegrityError` so an
#: overlap or a second open entry is turned into `OverlapRejected` while an unrelated integrity error
#: (a bad foreign key, say) is left to propagate. The names reproduce the migration's, which the
#: integration tests assert against the live schema.
_OVERLAP_CONSTRAINTS = ("no_overlapping_entries", "one_open_entry_per_employee")


# --------------------------------------------------------------------------- result


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    """What a resolved scan produced, for the router to serialise.

    `entry` is the time entry the scan created or matched. `action` says what happened, and `at` is
    the server-side timestamp the action recorded — the check-in time for a check-in and a duplicate
    no-op, the check-out time for a check-out.
    """

    entry: TimeEntry
    action: ScanAction
    at: datetime


# --------------------------------------------------------------------------- token resolution


def resolve_site(session: Session, token: str) -> Site:
    """Resolve a presented token to its site, verifying signature and current version.

    The token is parsed and its signature checked first (a forged or malformed token never reaches a
    row), then the named site is loaded and the token's version confirmed against the site's current
    `qr_token_version` — a code printed before the last regeneration names a version the site has
    moved past and is refused (Requirement 8.6). Every failure is the one `InvalidQr`, so the scanner
    cannot distinguish a forgery from a rotated code from a typo (Requirement 8.7).
    """
    try:
        parsed: SiteToken = parse(token)
    except InvalidToken as error:
        raise InvalidQr(str(error)) from error

    site = session.get(Site, parsed.site_id)
    if site is None:
        raise InvalidQr("token names a site that does not exist")
    if parsed.version != site.qr_token_version:
        raise InvalidQr("token version is not current")
    return site


# --------------------------------------------------------------------------- employee resolution


def resolve_employee_locked(session: Session, employee_id: uuid.UUID) -> Employee:
    """Load the employee row and take a row lock, serialising this employee's concurrent scans.

    `SELECT ... FOR UPDATE` is the second step of the design's flow: it makes two scans by the same
    person queue rather than interleave, so the "load open entry, then decide" sequence below sees a
    stable state. On SQLite (the unit-test engine) `with_for_update` is a no-op — SQLite serialises
    writes at the database level anyway — so the lock is exercised for real only against PostgreSQL,
    which is where the concurrency integration test proves it.
    """
    statement = select(Employee).where(Employee.id == employee_id).with_for_update()
    employee = session.scalars(statement).one_or_none()
    if employee is None:
        raise NoEmployeeForCaller("caller's employee record does not exist")
    return employee


# --------------------------------------------------------------------------- open-entry lookup


def open_entry_for(session: Session, employee_id: uuid.UUID) -> TimeEntry | None:
    """The employee's current open entry (check-in with no check-out), or `None`.

    At most one can exist — the partial unique index guarantees it — so `one_or_none` is honest
    rather than optimistic. Soft-deleted rows are excluded, matching the index's `WHERE` clause.
    """
    statement = (
        select(TimeEntry)
        .where(
            TimeEntry.employee_id == employee_id,
            TimeEntry.check_out_at.is_(None),
            TimeEntry.deleted_at.is_(None),
        )
        .order_by(TimeEntry.check_in_at.desc())
    )
    return session.scalars(statement).first()


def _overlapping_completed_entry(
    session: Session,
    *,
    employee_id: uuid.UUID,
    check_in_at: datetime,
    check_out_at: datetime,
    exclude_id: uuid.UUID | None = None,
) -> TimeEntry | None:
    """A completed entry for this employee whose time range overlaps `[check_in_at, check_out_at)`.

    Read only to *name* the conflict in the error the exclusion constraint has already decided
    (Requirement 11.7): the database is the authority on whether an overlap exists, and this query
    just recovers which existing entry the rejected write collided with, so the message can point at
    it. Ranges are half-open — two entries that merely touch at one instant (a transition's close and
    the next open) do not overlap — matching the `tstzrange(...) && ...` the constraint uses. On
    SQLite (the unit-test engine) there is no exclusion constraint, so this same query is what an
    application-level guard uses to refuse an overlap the database would not catch there.
    """
    statement = select(TimeEntry).where(
        TimeEntry.employee_id == employee_id,
        TimeEntry.deleted_at.is_(None),
        TimeEntry.check_out_at.is_not(None),
        TimeEntry.check_in_at < check_out_at,
        TimeEntry.check_out_at > check_in_at,
    )
    if exclude_id is not None:
        statement = statement.where(TimeEntry.id != exclude_id)
    return session.scalars(statement).first()


# --------------------------------------------------------------------------- time and period helpers


def _local_work_date(moment: datetime, timezone_name: str) -> date:
    """The local date a moment falls on, for `work_date` (Requirement 10.4).

    Storage is UTC; which calendar day a check-in belongs to is decided in the business timezone, so a
    shift that starts at 23:30 local is attributed to that day even though its UTC instant is the next
    date. A shift crossing midnight keeps the work date of its check-in.
    """
    return moment.astimezone(ZoneInfo(timezone_name)).date()


def is_period_locked(session: Session, work_date: date) -> bool:
    """Whether the calendar month containing `work_date` is locked (Requirement 9.5, 15.5).

    Reads the `period_locks` row for the month and asks it: a row locked and not since unlocked means
    the month is closed to writes. No row means the month was never locked, so a scan is allowed.
    """
    lock = session.scalars(
        select(PeriodLock).where(
            PeriodLock.year == work_date.year, PeriodLock.month == work_date.month
        )
    ).one_or_none()
    return lock is not None and lock.is_locked


def assert_period_open(
    session: Session,
    work_date: date,
    *,
    admin_override: bool = False,
) -> bool:
    """Refuse a write into a locked month, unless an administrator explicitly overrides (Req 15.5).

    A locked month rejects creation, edit and deletion of entries for every role — the guard the scan
    path and the manual-entry path both sit behind (Requirement 9.5, 12.5, 15.5). The one exception is
    an administrator performing an explicit override (Requirement 15.5): when `admin_override` is set
    and the month is locked, the write is *allowed* and this returns True so the caller knows an
    override was actually exercised (an override on an open month changes nothing and returns False),
    which is the signal the caller uses to record the override in the audit log (Requirement 15.6).

    Returns whether an override was used. Raises `PeriodLocked` when the month is locked and no
    override was authorised. Centralising the decision here means the admin-override path and the plain
    guard cannot drift apart, and there is one place that knows a locked month is not absolutely
    sealed — it is sealed against everyone but a deliberate administrator act.
    """
    if not is_period_locked(session, work_date):
        return False
    if admin_override:
        return True
    raise PeriodLocked(f"{work_date:%Y-%m} is locked")


def _duplicate_window_seconds(session: Session) -> int:
    """The configured duplicate-submission window, defaulting to 60s (Requirement 9.3)."""
    return settings_service.get_int_or(
        session, "duplicate_scan_window_seconds", _DEFAULT_DUPLICATE_WINDOW_SECONDS
    )


def _implausible_shift_minutes(session: Session) -> int:
    """The implausible-shift threshold in whole minutes (Requirement 10.5).

    Read from the `implausible_shift_hours` setting (seeded default 16 hours) and converted to
    minutes, because durations everywhere else in the engine are whole minutes. A shift whose
    `total_minutes` reaches this is flagged, not refused — the employee may genuinely have worked a
    long shift, so the decision is left to a manager rather than made by rejecting the check-out.
    """
    hours = settings_service.get_int_or(
        session, "implausible_shift_hours", _DEFAULT_IMPLAUSIBLE_SHIFT_HOURS
    )
    return hours * 60


# --------------------------------------------------------------------------- assignment


def _is_assigned(session: Session, employee_id: uuid.UUID, site_id: uuid.UUID) -> bool:
    """Whether the employee is assigned to the site (Requirement 7.1).

    Assignment is expectation only; it never restricts where time may be recorded (Requirement 7.2).
    It is read here for one reason: to decide whether an *open*-mode check-in at an unassigned site is
    flagged (Requirement 7.3), or a *strict*-mode one is refused (Requirement 7.4).
    """
    row = session.get(EmployeeSite, {"employee_id": employee_id, "site_id": site_id})
    return row is not None


# --------------------------------------------------------------------------- the scan


def resolve_scan(
    session: Session,
    *,
    employee_id: uuid.UUID | None,
    request: ScanRequest,
    context: AuditContext,
    now: datetime | None = None,
) -> ScanOutcome:
    """Resolve one scan into a check-in, a duplicate no-op, or a raised conflict.

    The steps follow the design's flow exactly. `now` is injectable so a test can pin the server time
    and the duplicate window without sleeping; production passes `None` and the current UTC instant is
    used. `employee_id` is the caller's linked employee (the router resolves it from the token); a
    login with none cannot scan.
    """
    if employee_id is None:
        raise NoEmployeeForCaller("caller is not linked to an employee")

    moment = (now or datetime.now(UTC)).astimezone(UTC)

    # Step 1: token → site, at the current version. A forged or rotated code fails here, before any
    # state is read, and never creates an entry (Requirement 8.7, 9.1).
    site = resolve_site(session, request.qr_token)

    # Step 2: lock the employee row so concurrent scans by this person serialise.
    employee = resolve_employee_locked(session, employee_id)

    # Step 3: the duplicate window (Requirement 9.3). An identical scan — same employee, same site,
    # created inside the window — returns the existing entry as a no-op, ahead of the open-entry
    # decision below. It has to come first: a double-tap on a check-in leaves the first entry *open*,
    # so a window check that ran only on the no-open-entry branch would never see the repeat and the
    # second tap would be read as a check-out. This absorbs a double-tap or a network retry.
    window = _duplicate_window_seconds(session)
    recent = _recent_identical_check_in(
        session, employee_id=employee.id, site_id=site.id, moment=moment, window_seconds=window
    )
    if recent is not None:
        return ScanOutcome(entry=recent, action=ScanAction.DUPLICATE_IGNORED, at=recent.check_in_at)

    # Step 4: load the open entry and decide.
    open_entry = open_entry_for(session, employee.id)

    if open_entry is None:
        return _check_in(
            session,
            employee=employee,
            site=site,
            moment=moment,
            context=context,
        )

    if open_entry.site_id == site.id:
        # A scan at the site of the open shift closes it (Requirement 10.1). The write sets the
        # check-out time and the total, and flags an implausible duration for review.
        return _check_out(session, entry=open_entry, moment=moment, context=context)

    # An open shift at a different site is the conflict of Requirement 11.4. Name the other site so
    # the employee can choose to transition or cancel; the transition itself is Task 18.
    other_site = session.get(Site, open_entry.site_id)
    raise OpenShiftElsewhere(open_entry, other_site if other_site is not None else site)


def _check_in(
    session: Session,
    *,
    employee: Employee,
    site: Site,
    moment: datetime,
    context: AuditContext,
    source: TimeEntrySource = TimeEntrySource.QR_SCAN,
    audit_reason: str = "scan_check_in",
) -> ScanOutcome:
    """Create a new check-in entry after the guards pass (Requirement 9.1).

    The duplicate window is already handled by the caller (`resolve_scan`, step 3), so by the time
    control reaches here the scan is a genuine new check-in. The guards run before anything is
    written, so a refusal leaves no partial state.

    `source` and `audit_reason` are parameters because the *opening half of a transition* reuses this
    exact path: it opens a new entry at the target site under the same active-employee, active-site,
    period-lock and assignment guards a plain check-in obeys, differing only in that it is recorded as
    `qr_scan` (the employee did scan the target's QR to transition) with the transition's audit
    reason. Keeping one write means a transition's new entry cannot drift from a scanned one.
    """
    # Step 6: the guards, before anything is written.
    if not employee.can_check_in:
        raise EmployeeNotActive(f"employee status is {employee.status}")
    if not site.can_check_in:
        raise SiteNotActive(f"site status is {site.status}")

    work_date = _local_work_date(moment, get_settings().app_timezone)
    if is_period_locked(session, work_date):
        raise PeriodLocked(f"{work_date:%Y-%m} is locked")

    assigned = _is_assigned(session, employee.id, site.id)
    flags: list[str] = []
    if not assigned:
        if site.assignment_mode is AssignmentMode.STRICT:
            raise UnassignedSiteRejected(f"employee not assigned to strict-mode site {site.id}")
        # Open mode: allow the check-in and flag it for manager review (Requirement 7.3).
        flags.append(FLAG_UNASSIGNED_SITE)

    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=work_date,
        check_in_at=moment,
        source=source,
        is_manual=False,
        status=TimeEntryStatus.DRAFT,
        flags=flags,
    )
    session.add(entry)
    # Flush so the entry has an id the audit rows can reference; the caller commits. A flush here is
    # also where the one-open-entry index fires if a race opened a second shift the row lock missed,
    # so the overlap guard wraps it.
    _flush_guarding_overlap(session, employee_id=employee.id, new_entry=entry)

    empty = dict.fromkeys(snapshot(entry), None)
    record_model_changes(session, entry, empty, context=context, reason=audit_reason)

    return ScanOutcome(entry=entry, action=ScanAction.CHECK_IN, at=entry.check_in_at)


def _flush_guarding_overlap(
    session: Session,
    *,
    employee_id: uuid.UUID,
    new_entry: TimeEntry,
) -> None:
    """Flush the pending write, refusing an overlap with a domain error that names the conflict.

    Two layers guard the invariant (Requirement 11.7). First, an *application* check: when the entry
    being written is completed, look for an existing completed entry it overlaps and refuse before the
    flush, naming it. This runs on every engine, including the SQLite unit-test one that has no
    exclusion constraint, so the same refusal a manager sees is exercised in unit tests. Second, the
    *database* backstop: the flush itself, whose GiST exclusion constraint and partial unique index
    reject an overlapping completed entry and a second open entry even against a race two application
    checks could each pass — which application code cannot win. An `IntegrityError` from one of those
    constraints is re-raised as the same domain error naming the conflict; an integrity error from any
    other constraint is left to propagate, because swallowing it would hide a real bug.

    The application check is a guard, not the authority: the database has the final say, and the seam
    of a transition (a close touching the next open at one instant) is a half-open touch that both
    layers accept.
    """
    if new_entry.check_out_at is not None:
        check_in_at = new_entry.check_in_at
        if check_in_at is not None and check_in_at.tzinfo is None:
            check_in_at = check_in_at.replace(tzinfo=UTC)
        check_out_at = new_entry.check_out_at
        if check_out_at.tzinfo is None:
            check_out_at = check_out_at.replace(tzinfo=UTC)
        # `no_autoflush`: the pending write is not yet valid to flush — that is exactly what this
        # check exists to decide — so the lookup query must not autoflush it into the exclusion
        # constraint and raise a raw `IntegrityError` before the guard can raise its domain error.
        with session.no_autoflush:
            conflict = _overlapping_completed_entry(
                session,
                employee_id=employee_id,
                check_in_at=check_in_at,
                check_out_at=check_out_at,
                exclude_id=new_entry.id,
            )
        if conflict is not None:
            raise OverlapRejected(
                f"entry overlaps existing entry {conflict.id}", conflicting_entry=conflict
            )

    try:
        session.flush()
    except IntegrityError as error:
        if not _is_overlap_constraint(error):
            raise
        session.rollback()
        conflict = _find_conflict_for(session, employee_id=employee_id, new_entry=new_entry)
        raise OverlapRejected(str(error.orig), conflicting_entry=conflict) from error


def _is_overlap_constraint(error: IntegrityError) -> bool:
    """Whether an `IntegrityError` came from the overlap or one-open-entry constraint."""
    text = str(getattr(error, "orig", error))
    return any(name in text for name in _OVERLAP_CONSTRAINTS)


def _find_conflict_for(
    session: Session,
    *,
    employee_id: uuid.UUID,
    new_entry: TimeEntry,
) -> TimeEntry | None:
    """The existing entry the rejected write collided with, for the error to name (Requirement 11.7).

    After a rolled-back flush the pending write is gone, so this looks up the surviving entry that
    conflicts: the overlapping completed entry when the new write is itself completed, otherwise the
    employee's current open entry (the one-open-entry index rejected a second open shift). `None` when
    it cannot be recovered — the error still carries a code and message, just not a named entry.
    """
    check_in_at = new_entry.check_in_at
    if check_in_at is not None and check_in_at.tzinfo is None:
        check_in_at = check_in_at.replace(tzinfo=UTC)
    check_out_at = new_entry.check_out_at
    if check_out_at is not None:
        if check_out_at.tzinfo is None:
            check_out_at = check_out_at.replace(tzinfo=UTC)
        return _overlapping_completed_entry(
            session,
            employee_id=employee_id,
            check_in_at=check_in_at,
            check_out_at=check_out_at,
            exclude_id=new_entry.id,
        )
    return open_entry_for(session, employee_id)


def _check_out(
    session: Session,
    *,
    entry: TimeEntry,
    moment: datetime,
    context: AuditContext,
) -> ScanOutcome:
    """Close an open entry: write the check-out time and the total, flagging an implausible duration.

    The single write for a check-out, shared by the unified scan (a scan at the site of the open
    shift) and the explicit button (`POST /api/scans/checkout`). The steps:

    * Reject a check-out that is not strictly after its check-in (Requirement 10.3), matching the
      database `check_out_after_check_in` constraint but failing with a translatable code first.
    * Set `check_out_at` to the server time and `total_minutes` to the whole minutes between the two
      UTC instants (Requirement 10.1, 10.2), truncated exactly as the daily classification truncates.
    * Add the `implausible_duration` flag when the total reaches the configured threshold, so a
      forgotten check-out is surfaced for review rather than accepted silently (Requirement 10.5).
    * Attribute the close to the caller (`closed_by_user_id`) and audit every changed field in the
      caller's transaction (Requirement 13.2).

    The period-lock guard is not re-checked here: the month is fixed by the check-in's `work_date`,
    which was open when the shift began, and a check-out completes a shift already recorded rather
    than writing into a new period. `work_date` is unchanged — a shift crossing midnight keeps the
    date of its check-in (Requirement 10.4).
    """
    # A reloaded entry can carry a naive `check_in_at` on SQLite (the unit-test engine stores no
    # zone); PostgreSQL preserves the zone. Normalise to aware UTC so the comparison and the duration
    # are between two aware instants regardless of engine.
    check_in_at = entry.check_in_at
    if check_in_at.tzinfo is None:
        check_in_at = check_in_at.replace(tzinfo=UTC)

    if moment <= check_in_at:
        raise CheckOutNotAfterCheckIn(
            f"check-out {moment.isoformat()} is not after check-in {check_in_at.isoformat()}"
        )

    before = snapshot(entry)

    # Read the threshold before mutating the entry: it queries the settings table, and a query
    # autoflushes the pending write. Fetching it first keeps the in-memory close from being flushed
    # into the exclusion constraint before `_flush_guarding_overlap` can raise its domain error.
    threshold = _implausible_shift_minutes(session)

    total = whole_minutes(check_in_at, moment)
    entry.check_out_at = moment
    entry.total_minutes = total
    if context.actor_user_id is not None:
        entry.closed_by_user_id = context.actor_user_id

    if total >= threshold and FLAG_IMPLAUSIBLE_DURATION not in entry.flags:
        # Reassign rather than append: the flags column is a mutable list, and reassigning a new list
        # is what the ORM reliably sees as a change to persist on both PostgreSQL and the SQLite
        # test engine.
        entry.flags = [*entry.flags, FLAG_IMPLAUSIBLE_DURATION]

    # Completing an entry gives it a range the exclusion constraint checks against every other
    # completed entry for this employee; a close that would overlap one is refused naming it
    # (Requirement 11.7). This is the normal check-out's backstop, and the seam of a transition (a
    # close touching the next open at one instant) is a half-open touch, not an overlap, so it passes.
    _flush_guarding_overlap(session, employee_id=entry.employee_id, new_entry=entry)
    record_model_changes(session, entry, before, context=context, reason="scan_check_out")

    return ScanOutcome(entry=entry, action=ScanAction.CHECK_OUT, at=moment)


def check_out(
    session: Session,
    *,
    employee_id: uuid.UUID | None,
    context: AuditContext,
    now: datetime | None = None,
) -> ScanOutcome:
    """Close the caller's current open shift explicitly, for `POST /api/scans/checkout`.

    The button path of Requirement 10.6: no QR is presented, so there is no site to resolve and no
    check-in decision to make — the only thing to do is close whatever the employee currently has
    open. The employee row is locked (serialising a button tap racing a scan), the open entry is
    loaded, and if none is open the request is refused with `no_open_shift` and a plain statement that
    there is nothing to close. When one is open, the same `_check_out` write the scan path uses closes
    it, so the totals and the implausible-duration flag are computed identically whichever path the
    employee took.
    """
    if employee_id is None:
        raise NoEmployeeForCaller("caller is not linked to an employee")

    moment = (now or datetime.now(UTC)).astimezone(UTC)

    employee = resolve_employee_locked(session, employee_id)
    open_entry = open_entry_for(session, employee.id)
    if open_entry is None:
        raise NoOpenShift("no open shift to check out of")

    return _check_out(session, entry=open_entry, moment=moment, context=context)


# --------------------------------------------------------------------------- transition and move


def transition(
    session: Session,
    *,
    employee_id: uuid.UUID | None,
    request: ScanRequest,
    context: AuditContext,
    now: datetime | None = None,
) -> ScanOutcome:
    """Close the current open shift and open one at the target site, in one transaction (Req 11.5).

    The confirmed answer to the `open_shift_elsewhere` conflict. The employee has an open shift at
    Site A, scanned Site B, was told about the collision, and chose to move; this does what a plain
    scan deliberately would not, because now it is authorised. The two writes share the caller's
    transaction — the router commits once — so there is never a moment where both shifts are open or
    both are closed: the day has no gap and no overlap at the seam (Requirement 11.5).

    Order matters. The close is written *first*, at the server time, and the open uses that same
    instant as its check-in, so the two entries meet at one point rather than overlapping. Because
    `tstzrange` is half-open the touching instant is not an overlap, and the one-open-entry index is
    satisfied the moment the first entry gains a `check_out_at` before the second is added.

    The closing entry is recorded as system-closed: its `source` becomes `system_transition` and the
    close is attributed in the audit trail with the transition as its reason, so the audit view can
    explain why an entry the employee never scanned out of was closed (Requirement 11.8). The opening
    entry runs the ordinary check-in guards — active employee, active site, period not locked,
    assignment mode — so a transition into an inactive or strict-mode site is refused exactly as a
    plain check-in there would be.
    """
    if employee_id is None:
        raise NoEmployeeForCaller("caller is not linked to an employee")

    moment = (now or datetime.now(UTC)).astimezone(UTC)

    # Resolve the target site first: a forged or rotated token fails before anything is closed
    # (Requirement 8.7), so a bad token can never leave the employee checked out of Site A with
    # nowhere open.
    target_site = resolve_site(session, request.qr_token)

    employee = resolve_employee_locked(session, employee_id)
    open_entry = open_entry_for(session, employee.id)
    if open_entry is None:
        # Nothing to transition from. Treat it as a plain scan would: a check-in at the target,
        # rather than inventing a close of a shift that is not open.
        return _check_in(
            session, employee=employee, site=target_site, moment=moment, context=context
        )

    if open_entry.site_id == target_site.id:
        raise SameSiteTransition(
            f"already open at site {target_site.id}; scan to check out rather than transition"
        )

    # Close Site A first, marked as a system transition (Requirement 11.8), then open Site B at the
    # same instant. The close writes its own audit rows; the guards on the open run before it is
    # added, so a refused open leaves the whole transaction to be rolled back by the router with
    # nothing committed.
    _close_for_transition(session, entry=open_entry, moment=moment, context=context)
    return _check_in(
        session,
        employee=employee,
        site=target_site,
        moment=moment,
        context=context,
        source=TimeEntrySource.QR_SCAN,
        audit_reason="scan_transition_open",
    )


def end_work_and_move(
    session: Session,
    *,
    employee_id: uuid.UUID | None,
    context: AuditContext,
    now: datetime | None = None,
) -> ScanOutcome:
    """Close the current open shift with no departure QR — "End work and move to another site" (Req 11.6).

    The closing half of a transition on its own: the employee is leaving Site A and has no QR to scan
    (they have already left, or the code is unreachable), so this closes the open entry at the server
    time without requiring the departure scan. It differs from the explicit check-out button only in
    intent and in the mark it leaves — the entry is recorded as `system_transition` with the move as
    its audit reason, so the trail shows it was ended to move on rather than scanned out — which is
    why it is here rather than folded into `check_out`. No new shift is opened; the employee opens the
    next one by scanning in at the site they move to, which is a plain check-in.
    """
    if employee_id is None:
        raise NoEmployeeForCaller("caller is not linked to an employee")

    moment = (now or datetime.now(UTC)).astimezone(UTC)

    employee = resolve_employee_locked(session, employee_id)
    open_entry = open_entry_for(session, employee.id)
    if open_entry is None:
        raise NoOpenShift("no open shift to end")

    return _close_for_transition(session, entry=open_entry, moment=moment, context=context)


def _close_for_transition(
    session: Session,
    *,
    entry: TimeEntry,
    moment: datetime,
    context: AuditContext,
) -> ScanOutcome:
    """Close an open entry as a system transition: check-out time, total, `system_transition` source.

    The shared close for both `transition` and `end_work_and_move` (Requirement 11.6, 11.8). It reuses
    `_check_out` for the write everything downstream depends on — the check-out time, the whole-minute
    total, the implausible-duration flag, the `closed_by_user_id` attribution and the audit rows — so
    a system close and a scanned one produce identical totals. On top of that it sets `source` to
    `system_transition` and re-labels the audit reason to `scan_transition_close`, so the entry is
    visibly system-closed with the transition as its stated reason (Requirement 11.8). The
    `closed_by_user_id` is left as `_check_out` set it (the confirming employee): the *reason* records
    that the system closed it on a transition, while the actor is still the person who confirmed the
    move, which is the honest account of who did what.
    """
    before = snapshot(entry)
    outcome = _check_out(session, entry=entry, moment=moment, context=context)
    entry.source = TimeEntrySource.SYSTEM_TRANSITION
    session.flush()
    # A second audit pass over the same before-snapshot records the source change (and re-states the
    # close) under the transition reason, so the trail explains the system close (Requirement 11.8).
    record_model_changes(session, entry, before, context=context, reason="scan_transition_close")
    return outcome


def _recent_identical_check_in(
    session: Session,
    *,
    employee_id: uuid.UUID,
    site_id: uuid.UUID,
    moment: datetime,
    window_seconds: int,
) -> TimeEntry | None:
    """A check-in by this employee at this site created inside the duplicate window (Requirement 9.3).

    "Identical" is same employee, same site, within the window of the current instant — which is what
    a double-tap or a retry of the same scan looks like. The most recent qualifying entry is returned
    so a repeat resolves to the entry the first scan created. The window is measured against
    `check_in_at`, the server time the entry recorded, not the wall clock at read time.
    """
    cutoff = moment - timedelta(seconds=window_seconds)
    statement = (
        select(TimeEntry)
        .where(
            TimeEntry.employee_id == employee_id,
            TimeEntry.site_id == site_id,
            TimeEntry.source == TimeEntrySource.QR_SCAN,
            TimeEntry.deleted_at.is_(None),
            TimeEntry.check_in_at >= cutoff,
            TimeEntry.check_in_at <= moment,
        )
        .order_by(TimeEntry.check_in_at.desc())
    )
    return session.scalars(statement).first()


# --------------------------------------------------------------------------- status


def current_open_shift(session: Session, employee_id: uuid.UUID | None) -> TimeEntry | None:
    """The caller's current open shift, for `GET /api/scans/status` (Requirement 23.1).

    `None` when the caller is not linked to an employee or has no shift open — in both cases the
    employee home screen shows scan-to-check-in rather than check-out. A login with no employee is a
    quiet `None` rather than an error, because the status query is a read the front end polls and a
    manager glancing at it should not see a failure.
    """
    if employee_id is None:
        return None
    return open_entry_for(session, employee_id)


# --------------------------------------------------------------------------- recent work history


@dataclass(frozen=True, slots=True)
class WorkHistoryDay:
    """One day of the employee's own recent work: a local work date and the minutes worked that day.

    `total_minutes` is the sum of `total_minutes` across every *completed* entry on `work_date`, so a
    day the employee split between two sites shows the combined total, not one site's share
    (Requirement 11.2). Only completed entries contribute; an open shift with no `total_minutes` yet
    adds nothing.
    """

    work_date: date
    total_minutes: int


def recent_work_history(
    session: Session,
    employee_id: uuid.UUID | None,
    *,
    limit_days: int = 14,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[WorkHistoryDay]:
    """The caller's own completed work by day, newest first, for `GET /api/scans/history` (Req 23.3).

    Each element is a `work_date` and the whole minutes worked that day, summed across *every* site
    the employee worked that day (Requirement 11.2 — the day total is the sum of the per-site
    entries). Only completed entries contribute: `check_out_at` and `total_minutes` are both set on a
    close, so summing `total_minutes` over rows with a non-null `check_out_at` counts a finished shift
    once and ignores an open one, which has no total yet. Soft-deleted rows are excluded, the same
    `deleted_at IS NULL` filter every other entry query applies (`open_entry_for`,
    `_recent_identical_check_in`). Grouped by `work_date` and ordered newest first, and a day whose
    completed minutes sum to zero is not shown — the simplest correct behaviour is to include only
    days with some completed minutes.

    **Two shapes, one function.** With no range (`date_from` and `date_to` both `None`) the result is
    the recent-work view the employee home screen reads: capped at the most recent `limit_days` days
    (default 14), where the count is of *days that have completed work*, so a day with only an open
    shift does not occupy one of the slots. This is the existing contract and is left unchanged.

    When a range is supplied, `work_date` is filtered between `date_from` and `date_to` *inclusive*
    and **every** matching day is returned — the 14-day cap does not apply, because the my-hours
    screen asks for exactly the window the employee picked, not a recent slice of it. Either bound may
    be given on its own: `date_from` alone is an open-ended "from this date on", `date_to` alone a
    "up to and including this date". An **inverted range** (`date_from > date_to`) cannot match any
    day, so it returns an empty list rather than erroring — the sane, predictable answer for a picker
    whose two dates got crossed, and the front end shows "no hours in this range".

    A caller with no linked employee gets an empty list, matching `current_open_shift`: the employee
    home screen a manager might glance at shows "no recent work" rather than an error, and the router
    keeps the not-linked case a quiet empty read exactly as the status endpoint does.
    """
    if employee_id is None:
        return []

    # An inverted range matches no day. Answer empty rather than running a query that would return
    # nothing anyway (or, with only the lower bound applied, the wrong thing) — see the docstring.
    if date_from is not None and date_to is not None and date_from > date_to:
        return []

    ranged = date_from is not None or date_to is not None

    total = func.sum(TimeEntry.total_minutes)
    statement = select(TimeEntry.work_date, total.label("total_minutes")).where(
        TimeEntry.employee_id == employee_id,
        TimeEntry.deleted_at.is_(None),
        TimeEntry.check_out_at.is_not(None),
    )
    if date_from is not None:
        statement = statement.where(TimeEntry.work_date >= date_from)
    if date_to is not None:
        statement = statement.where(TimeEntry.work_date <= date_to)

    statement = (
        statement.group_by(TimeEntry.work_date)
        .having(total > 0)
        .order_by(TimeEntry.work_date.desc())
    )
    # The cap is only the unfiltered recent-work slice; a range returns all of its days.
    if not ranged:
        statement = statement.limit(limit_days)

    return [
        WorkHistoryDay(work_date=row.work_date, total_minutes=int(row.total_minutes))
        for row in session.execute(statement)
    ]
