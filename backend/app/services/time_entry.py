"""Reading recorded time entries for the manager hours view (Requirement 2.3, 2.4, 18.1, 22.3).

The hours view lists shifts: an administrator or accounting reads across the business, a site manager
reads only the sites they run. This service owns the query — the filters, the scoping and the stable
order — and the router only translates. Nothing here raises an HTTP error or commits; it is a read,
so there is no transaction to own, but the no-HTTP rule holds so the service stays testable without a
request. This mirrors the read side of `app.services.site`.

Three things carry the weight.

**Scoping is applied in the query, not after the fetch (Requirement 2.3).** The router hands in a
statement already narrowed to the caller's sites through `Caller.scope_query`, exactly as the site
list does. Filtering after the fetch would page over rows the caller may not see, so a manager's page
one could come back empty while their data sat on a later page — the same bug `app.core.authz`
describes and the reason the scope is a `WHERE` clause. An administrator or accounting hands in no
scope and reads every site.

**The employee and site are joined once, so a row carries its labels.** The view shows an employee's
name and a site's name against each shift; loading them with the entry avoids an N+1 walk and lets the
response be built from one query. The join is on the entry's own foreign keys, so it neither widens
nor narrows the set — every entry has exactly one employee and one site.

**The order is total and matches how the view lays out (Requirement 18.1).** Entries come back sorted
by employee, then work date, then check-in time, then id as a tie-break, so the front end can group
the flat page into a chronological per-employee day across sites without re-sorting, and two entries
that share the first three keys never swap places between pages (Requirement 22.5).

Soft-deleted entries are excluded: a deleted entry is retained for audit (Requirement 12.7) but is not
part of the hours a manager reviews.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import Row, Select, cast, func, or_, select
from sqlalchemy import String as SAString
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.calculations.hours import whole_minutes
from app.core.config import get_settings
from app.models.employee import Employee
from app.models.site import Site
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.schemas.time_entry import TimeEntryCreate, TimeEntryUpdate
from app.services import scan as scan_service
from app.services.audit import AuditContext, record_model_changes, snapshot


@dataclass(frozen=True, slots=True)
class TimeEntryFilters:
    """The filters the hours view offers (Requirement 22.3).

    Every field is optional and independent — a request may fix none of them (the whole visible set),
    a date range alone, one employee, one site, a status, or an anomaly-only view. A `None` field is
    "do not filter on this"; the service leaves the corresponding predicate off the query entirely.

    `date_from` and `date_to` bound `work_date` inclusively, so a request for a single day passes the
    same date for both. `flags` is a set of anomaly markers, matched as "carries at least one of
    these", so an anomaly-only view asks for `{"implausible_duration", "unassigned_site"}` and gets
    every flagged entry. `manual_only` narrows to hand-touched entries for a correction review.
    """

    date_from: date | None = None
    date_to: date | None = None
    employee_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    status: TimeEntryStatus | None = None
    flags: frozenset[str] = frozenset()
    manual_only: bool = False


@dataclass(frozen=True, slots=True)
class TimeEntryRow:
    """One joined row: the entry alongside its employee's names and its site's name.

    A plain carrier so the router can build a response item without reaching back through the ORM
    relationships (which the entry model does not declare) or issuing a second query per row.
    """

    entry: TimeEntry
    employee_name: str
    employee_name_en: str
    employee_number: str | None
    site_name: str


@dataclass(frozen=True, slots=True)
class TimeEntryPage:
    """A page of joined rows plus the unfiltered-by-paging total, for list rendering."""

    rows: Sequence[TimeEntryRow]
    total: int


def base_select() -> Select[tuple[TimeEntry]]:
    """The time-entry query, excluding soft-deleted rows.

    Exposed so the router can hand it to `Caller.scope_query(statement, TimeEntry.site_id)` to narrow
    a manager's list to their sites before it reaches `list_time_entries`, the same shape the site
    list uses. Scoping on `site_id` is what Requirement 2.3 turns on: a manager sees the entries at
    the sites they run.
    """
    return select(TimeEntry).where(TimeEntry.deleted_at.is_(None))


def _apply_filters(
    statement: Select[tuple[TimeEntry]],
    filters: TimeEntryFilters,
    *,
    dialect: str,
) -> Select[tuple[TimeEntry]]:
    """Fold each set filter onto the statement; a `None` filter adds no predicate (Requirement 22.3).

    The date range bounds `work_date` inclusively. The flag filter is dialect-aware — see
    `_any_flag_predicate` — because the `flags` column is a real text array on PostgreSQL and a JSON
    string on the SQLite fallback the unit tests build on, and the array containment operator SQLite
    cannot parse. An empty flag set adds no predicate.
    """
    if filters.date_from is not None:
        statement = statement.where(TimeEntry.work_date >= filters.date_from)
    if filters.date_to is not None:
        statement = statement.where(TimeEntry.work_date <= filters.date_to)
    if filters.employee_id is not None:
        statement = statement.where(TimeEntry.employee_id == filters.employee_id)
    if filters.site_id is not None:
        statement = statement.where(TimeEntry.site_id == filters.site_id)
    if filters.status is not None:
        statement = statement.where(TimeEntry.status == filters.status)
    if filters.manual_only:
        statement = statement.where(TimeEntry.is_manual.is_(True))
    if filters.flags:
        statement = statement.where(_any_flag_predicate(filters.flags, dialect=dialect))
    return statement


def _any_flag_predicate(flags: frozenset[str], *, dialect: str) -> ColumnElement[bool]:
    """A predicate true when the entry carries at least one of `flags`, on either engine.

    The `flags` column is a PostgreSQL `text[]` in production and a JSON string on the SQLite unit-test
    engine (`app.models.time_entry` maps it with a `with_variant(JSON())` fallback), and the two need
    different tests: on PostgreSQL the array-overlap operator `&&` is a set intersection, while on
    SQLite the stored value is JSON text such as `["implausible_duration"]`, matched by looking for
    the quoted flag inside it. Both are built as an `OR` over the requested flags, sorted so the
    compiled SQL is deterministic. Because the flag set is a small closed vocabulary of known markers
    with no JSON metacharacters, the SQLite `LIKE` needs no escaping.
    """
    ordered = sorted(flags)
    if dialect == "postgresql":
        return TimeEntry.flags.op("&&")(ordered)
    flags_as_text = cast(TimeEntry.flags, SAString)
    return or_(*(flags_as_text.like(f'%"{flag}"%') for flag in ordered))


def list_time_entries(
    session: Session,
    *,
    filters: TimeEntryFilters | None = None,
    scope_statement: Select[tuple[TimeEntry]] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> TimeEntryPage:
    """A stable-sorted, filtered, scoped page of time entries with their labels (Requirement 18.1).

    `scope_statement` lets the router hand in a query already narrowed to the caller's sites; when it
    is `None` the service reads across every site, the administrator and accounting case. Filters are
    applied on top of the scope, so a manager filtering by employee still only sees that employee's
    shifts at the manager's own sites.

    The order — employee, then work date, then check-in, then id — is total, so the front end lays out
    a chronological per-employee day without re-sorting and pagination is stable (Requirement 22.5).
    The employee and site are joined so each row carries the names the view labels it with; the join
    is on the entry's own foreign keys, so it does not change which entries match.
    """
    filters = filters or TimeEntryFilters()
    scoped = scope_statement if scope_statement is not None else base_select()
    filtered = _apply_filters(scoped, filters, dialect=_dialect_name(session))

    total = session.scalar(select(func.count()).select_from(filtered.subquery())) or 0

    ordered = (
        _labelled_statement(filtered)
        .order_by(
            Employee.full_name,
            TimeEntry.employee_id,
            TimeEntry.work_date,
            TimeEntry.check_in_at,
            TimeEntry.id,
        )
        .limit(limit)
        .offset(offset)
    )

    rows = [_row_of(row) for row in session.execute(ordered)]
    return TimeEntryPage(rows=rows, total=total)


def _labelled_statement(filtered: Select[tuple[TimeEntry]]):
    """Join the matched entries to the employee and site that label each row.

    The filtered statement carries the scope (Requirement 2.3) and the filters (Requirement 22.3);
    its ids drive an `IN` against a fresh select that joins `Employee` and `Site` on the entry's own
    foreign keys, adding the two names without changing which entries match. Driving off the ids
    rather than joining the subquery directly keeps the selected row a mapped `TimeEntry` the response
    model can validate, and every entry has exactly one employee and one site so the join is total.
    Returns a statement selecting the ORM `TimeEntry` plus the three label columns.
    """
    entry_ids = filtered.with_only_columns(TimeEntry.id).scalar_subquery()
    return (
        select(TimeEntry, Employee.full_name, Employee.full_name_en, Employee.employee_number, Site.name)
        .join(Employee, Employee.id == TimeEntry.employee_id)
        .join(Site, Site.id == TimeEntry.site_id)
        .where(TimeEntry.id.in_(entry_ids))
    )


def _dialect_name(session: Session) -> str:
    """The name of the session's database dialect (`postgresql`, `sqlite`, ...).

    Read from the bound engine so the flag filter can pick the containment test the engine
    understands. Defaults to `postgresql`, the production engine, when no bind can be resolved.
    """
    bind = session.get_bind()
    return bind.dialect.name if bind is not None else "postgresql"


def _row_of(row: Row) -> TimeEntryRow:
    entry, employee_name, employee_name_en, employee_number, site_name = row
    return TimeEntryRow(
        entry=entry,
        employee_name=employee_name,
        employee_name_en=employee_name_en,
        employee_number=employee_number,
        site_name=site_name,
    )


# =============================================================================================
# Manual entry and correction (Requirement 12)
# =============================================================================================
#
# The read side above lists shifts. This side writes them by hand: a manager or administrator creates
# a time entry when a scan failed (Requirement 12.1), corrects the times of an existing one
# (Requirement 12.2), or soft-deletes one, always retaining the row for audit (Requirement 12.7).
# Every operation carries a mandatory non-blank reason, enforced at the schema (Requirement 12.3).
#
# The invariants a manual write must obey are *exactly* those a scanned write obeys (Requirement
# 12.5), so this module does not re-derive them — it reuses the scan service's primitives, which are
# the single authority for the whole system:
#
#   * `scan.is_period_locked`            — the month must be open (Requirement 15.5).
#   * `scan._flush_guarding_overlap`     — no two completed entries for one employee may overlap; the
#                                          refusal names the conflicting entry (Requirement 11.7). It
#                                          layers an application check over the database exclusion
#                                          constraint, so the same refusal holds on the SQLite unit
#                                          engine and against a race on PostgreSQL.
#   * `scan._implausible_shift_minutes`  — a shift at least this long is flagged, not refused
#     + `scan.FLAG_IMPLAUSIBLE_DURATION`   (Requirement 10.5, 12.5).
#   * `scan._local_work_date`            — a manual entry with no explicit work date is attributed to
#                                          the local date of its check-in, as a scan is (Req 10.4).
#   * `hours.whole_minutes`              — the stored total is the same truncation a check-out uses,
#                                          so a manual entry and a scanned one classify identically.
#
# Reusing these rather than reimplementing is the point of Requirement 12.5: a rule that lived in two
# places would eventually disagree between them, and the disagreement would be a shift paid twice or a
# locked month written into. Marking every hand-touched entry manual (Requirement 12.4) is this
# module's own concern, since the scan path never does it.
#
# Like the read side and the scan service, nothing here commits: the router owns the unit of work, so
# a manual write and its audit rows land together or not at all (Requirement 13.2). Role and scope are
# the router's concern — managers and administrators only, employees forbidden (Requirement 12.6), a
# site manager scoped to their sites (Requirement 2.3).


class ManualEntryError(Exception):
    """Base for a refused manual entry, correction or deletion.

    A domain error, not an HTTP error: `code` is the machine string the router lifts into the error
    envelope and the front end translates, the same convention the scan and employee services use.
    """

    code = "manual_entry_error"


class TimeEntryNotFound(ManualEntryError):
    """No live time entry with that id — it never existed or was already soft-deleted."""

    code = "time_entry_not_found"


class EmployeeNotFound(ManualEntryError):
    """A manual entry named an employee that does not exist."""

    code = "employee_not_found"


class SiteNotFound(ManualEntryError):
    """A manual entry named a site that does not exist."""

    code = "site_not_found"


class CheckOutNotAfterCheckIn(ManualEntryError):
    """The resulting check-out is not strictly after the check-in (Requirement 10.3, 12.5).

    Matches the database `check_out_after_check_in` constraint, refused in the service first with a
    translatable code. Applies to a creation (both times given) and to an edit that would leave the
    two out of order.
    """

    code = "check_out_not_after_check_in"


class NoTimeFieldToUpdate(ManualEntryError):
    """A correction supplied neither a check-in nor a check-out time (Requirement 12.2).

    An edit that changes no time is not a correction; the reason alone cannot be the whole of an edit,
    since a manual entry's times are what a correction exists to fix. Refused so the caller sends a
    time to change.
    """

    code = "no_time_field_to_update"


def get_live_entry(session: Session, entry_id: uuid.UUID) -> TimeEntry:
    """Load a non-deleted time entry by id, or raise `TimeEntryNotFound`.

    Soft-deleted rows are excluded: a deleted entry is retained for audit but is no longer a live
    record to correct or delete again (Requirement 12.7). The caller (the router) checks scope against
    the returned entry's `site_id` before acting on it (Requirement 2.3).
    """
    entry = session.scalars(
        select(TimeEntry).where(TimeEntry.id == entry_id, TimeEntry.deleted_at.is_(None))
    ).one_or_none()
    if entry is None:
        raise TimeEntryNotFound(f"time entry {entry_id} does not exist or is deleted")
    return entry


def _aware_utc(moment: datetime) -> datetime:
    """A datetime normalised to aware UTC, so comparisons and durations are between aware instants.

    A client sends an ISO 8601 time; on the SQLite unit engine a reloaded column can come back naive.
    Both are coerced to aware UTC here, matching how the scan service normalises before it compares a
    check-out to its check-in or computes a duration.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _guard_period_open(
    session: Session, work_date: date, *, admin_override: bool = False
) -> bool:
    """Refuse a write into a locked month (Requirement 12.5, 15.5), reusing the scan authority.

    Delegates to `scan.assert_period_open`, the single guard that also knows the one exception: an
    administrator performing an explicit override may write into a locked month (Requirement 15.5).
    Returns whether an override was actually used — true only when the month was locked *and* the
    override was authorised — so the caller can record the override in the audit log (Requirement
    15.6). A locked month with no override raises `PeriodLocked`; an open month never overrides.
    """
    return scan_service.assert_period_open(session, work_date, admin_override=admin_override)


def _apply_plausibility_flag(session: Session, entry: TimeEntry, total_minutes: int | None) -> None:
    """Add or leave the implausible-duration flag to match the entry's current total (Req 10.5, 12.5).

    A manual entry is subject to the same plausibility rule as a scanned one: a shift at least as long
    as the configured threshold is flagged for manager review, not refused. The threshold and the flag
    name are the scan service's, so a manual entry and a scanned one are flagged on the same boundary.
    A shift that is no longer implausible after a correction has the flag removed, so a fixed entry
    stops carrying a marker that no longer applies.
    """
    threshold = scan_service._implausible_shift_minutes(session)
    has_flag = scan_service.FLAG_IMPLAUSIBLE_DURATION in entry.flags
    should_flag = total_minutes is not None and total_minutes >= threshold
    if should_flag and not has_flag:
        entry.flags = [*entry.flags, scan_service.FLAG_IMPLAUSIBLE_DURATION]
    elif has_flag and not should_flag:
        entry.flags = [f for f in entry.flags if f != scan_service.FLAG_IMPLAUSIBLE_DURATION]


def create_manual_entry(
    session: Session,
    payload: TimeEntryCreate,
    *,
    context: AuditContext,
    admin_override: bool = False,
) -> TimeEntry:
    """Create a manually recorded, completed shift (Requirement 12.1, 12.4, 12.5).

    The steps mirror a scanned write, in the order that leaves no partial state on a refusal:

    1. Resolve the employee and site the entry names; a missing either is a domain not-found.
    2. Normalise both times to aware UTC and refuse a check-out that is not after the check-in
       (Requirement 10.3), the same check the database and the scan path enforce.
    3. Attribute the entry to a work date — the caller's if given, otherwise the local date of the
       check-in (Requirement 10.4) — and refuse if that month is locked (Requirement 15.5).
    4. Build the entry marked manual: `source=manual`, `is_manual=True`, `manual_reason` set, so it is
       badged wherever it appears (Requirement 12.4), and attribute its creation to the actor.
    5. Compute `total_minutes` from the two instants with the shared truncation, and flag an
       implausible duration for review (Requirement 10.5).
    6. Flush through the overlap guard, which refuses an entry overlapping an existing completed one
       and names the conflict (Requirement 11.7), then audit every field in the caller's transaction.
    """
    employee = session.get(Employee, payload.employee_id)
    if employee is None:
        raise EmployeeNotFound(f"employee {payload.employee_id} does not exist")
    site = session.get(Site, payload.site_id)
    if site is None:
        raise SiteNotFound(f"site {payload.site_id} does not exist")

    check_in_at = _aware_utc(payload.check_in_at)
    check_out_at = _aware_utc(payload.check_out_at)
    if check_out_at <= check_in_at:
        raise CheckOutNotAfterCheckIn(
            f"check-out {check_out_at.isoformat()} is not after check-in {check_in_at.isoformat()}"
        )

    work_date = payload.work_date or scan_service._local_work_date(
        check_in_at, get_settings().app_timezone
    )
    _guard_period_open(session, work_date, admin_override=admin_override)

    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=work_date,
        check_in_at=check_in_at,
        check_out_at=check_out_at,
        total_minutes=whole_minutes(check_in_at, check_out_at),
        source=TimeEntrySource.MANUAL,
        is_manual=True,
        manual_reason=payload.reason,
        status=TimeEntryStatus.DRAFT,
        flags=[],
        created_by_user_id=context.actor_user_id,
    )
    _apply_plausibility_flag(session, entry, entry.total_minutes)

    session.add(entry)
    # Flush through the shared overlap guard so a manual entry cannot straddle an existing completed
    # shift; the refusal names the conflict (Requirement 11.7). The guard flushes, so the entry has an
    # id the audit rows reference.
    scan_service._flush_guarding_overlap(session, employee_id=employee.id, new_entry=entry)

    empty = dict.fromkeys(snapshot(entry), None)
    record_model_changes(session, entry, empty, context=context, reason=payload.reason)
    return entry


def correct_manual_entry(
    session: Session,
    entry_id: uuid.UUID,
    payload: TimeEntryUpdate,
    *,
    context: AuditContext,
    admin_override: bool = False,
) -> TimeEntry:
    """Correct the check-in or check-out time of an existing entry (Requirement 12.2, 12.4, 12.5).

    Either time may be sent; at least one must be (Requirement 12.2). Editing marks the entry manual
    wherever it appears even if it was scanned (Requirement 12.4), because a hand correction is what
    the marking discloses; the `source` is left as it was (a corrected scan is still a scan by origin)
    but `is_manual` becomes true and the reason is recorded. The resulting times must stay ordered
    (Requirement 10.3), the entry's month must be open (Requirement 15.5), the total is recomputed,
    the implausible-duration flag is refreshed (Requirement 10.5), and the overlap guard refuses a
    correction that would straddle another completed entry, naming it (Requirement 11.7). Every
    changed field is audited under the reason in the caller's transaction (Requirement 13.2).
    """
    if payload.check_in_at is None and payload.check_out_at is None:
        raise NoTimeFieldToUpdate("a correction must change the check-in or the check-out time")

    entry = get_live_entry(session, entry_id)

    # The month the entry currently belongs to must be open before it may be corrected (Requirement
    # 15.5), unless an administrator overrides. A correction cannot move an entry into or out of a
    # locked month unnoticed: its work date is fixed by its check-in, refreshed below if it changes.
    _guard_period_open(session, entry.work_date, admin_override=admin_override)

    before = snapshot(entry)

    new_check_in = _aware_utc(payload.check_in_at) if payload.check_in_at is not None else _aware_utc(
        entry.check_in_at
    )
    new_check_out = entry.check_out_at
    if payload.check_out_at is not None:
        new_check_out = _aware_utc(payload.check_out_at)
    elif new_check_out is not None:
        new_check_out = _aware_utc(new_check_out)

    if new_check_out is not None and new_check_out <= new_check_in:
        raise CheckOutNotAfterCheckIn(
            f"check-out {new_check_out.isoformat()} is not after check-in {new_check_in.isoformat()}"
        )

    entry.check_in_at = new_check_in
    entry.check_out_at = new_check_out
    entry.work_date = scan_service._local_work_date(new_check_in, get_settings().app_timezone)
    entry.total_minutes = (
        whole_minutes(new_check_in, new_check_out) if new_check_out is not None else None
    )
    entry.is_manual = True
    if entry.manual_reason is None:
        entry.manual_reason = payload.reason
    _apply_plausibility_flag(session, entry, entry.total_minutes)

    # If the corrected month is locked, refuse — a correction must not land in a closed period even if
    # the original one was open (Requirement 15.5) — unless an administrator overrides.
    _guard_period_open(session, entry.work_date, admin_override=admin_override)

    scan_service._flush_guarding_overlap(session, employee_id=entry.employee_id, new_entry=entry)
    record_model_changes(session, entry, before, context=context, reason=payload.reason)
    return entry


def soft_delete_entry(
    session: Session,
    entry_id: uuid.UUID,
    *,
    reason: str,
    context: AuditContext,
    admin_override: bool = False,
) -> TimeEntry:
    """Soft-delete an entry, retaining the row for audit (Requirement 12.7).

    Never a hard delete: `deleted_at` and `delete_reason` are set and the row stays, so the audit
    trail and any historical report keep a valid record of the shift that was removed. The entry's
    month must be open (Requirement 15.5) — a deletion is a write, and a locked month rejects writes
    for every role but an admin override. The change is audited under the reason in the caller's
    transaction (Requirement 13.2). A blank reason never reaches here; the schema rejects it
    (Requirement 12.3).
    """
    entry = get_live_entry(session, entry_id)
    _guard_period_open(session, entry.work_date, admin_override=admin_override)

    before = snapshot(entry)
    entry.deleted_at = datetime.now(UTC)
    entry.delete_reason = reason
    session.flush()
    record_model_changes(session, entry, before, context=context, reason=reason)
    return entry
