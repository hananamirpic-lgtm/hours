"""Time-entry router — the manager hours view read (Requirement 2.3, 2.4, 18.1, 22.3).

HTTP only: guard, read the filters off the query string, narrow the query to the caller's sites, call
the service and shape the response. This is a read, so there is no write to commit and no error to map
onto a status code — the one authorization concern is scope, and it is applied the same way the site
list applies it.

`GET /api/time-entries` lists recorded shifts for the hours view. It is open to the hours readers of
Requirement 2.4 and 2.6 — administrator, site manager and accounting — through `HoursReaderCaller`.
A site manager's list is narrowed to their assigned sites in the query's `WHERE` clause via
`Caller.scope_query` on `TimeEntry.site_id` (Requirement 2.3), exactly as `app.api.routers.sites`
narrows the site list; an administrator or accounting reads across every site. The employee role is
absent: an employee reads their own shifts through the mobile status endpoint, not this console view.

No response is redacted, because a time entry carries no wage or billing field — it records when
someone worked, not what they are paid. Scoping is the whole of the access control here, and it is in
the query, not a post-fetch filter, so a manager's pagination stays stable (Requirement 22.5).

The write endpoints for manual entry and correction — `POST`, `PATCH` and `DELETE /time-entries` —
mount on this same router (Requirement 12). They are open to the attendance writers of Requirement
2.4 and 2.6 — administrator and site manager — through `AttendanceWriterCaller`; the employee role is
absent, which is Requirement 12.6 (employees may not create, edit or delete time entries). A site
manager is further scoped to the sites they run (Requirement 2.3): a create is refused unless the
named site is in their scope, and an edit or delete is refused unless the entry's site is. Every
operation carries a mandatory non-blank reason, and the reused scan primitives apply the same overlap,
plausibility and period-lock rules a scanned entry obeys (Requirement 12.5). The service never
commits, so each write commits here and the audit rows ride along in the same transaction
(Requirement 13.2), the pattern `app.api.routers.scans` and `app.api.routers.employees` follow.
"""

from __future__ import annotations

import uuid
from datetime import date
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import (
    AttendanceWriterCaller,
    AuthenticatedContext,
    DbSession,
    HoursReaderCaller,
    api_error,
)
from app.models.time_entry import TimeEntry, TimeEntryStatus
from app.models.user import UserRole
from app.schemas.period import BulkStatusRequest, BulkStatusResponse
from app.schemas.time_entry import (
    TimeEntryCreate,
    TimeEntryDelete,
    TimeEntryListItem,
    TimeEntryListResponse,
    TimeEntryResponse,
    TimeEntryUpdate,
)
from app.services import period as period_service
from app.services import scan as scan_service
from app.services import time_entry as time_entry_service

router = APIRouter(prefix="/time-entries", tags=["time-entries"])

#: The anomaly flags the hours view filters on (Requirement 22.3). Constrained to the known markers so
#: a typo in the query string is a validation error rather than a filter that silently matches nothing.
_KNOWN_FLAGS = frozenset({"unassigned_site", "implausible_duration"})


@router.get(
    "",
    response_model=TimeEntryListResponse,
    summary="List time entries for the hours view",
    description=(
        "A stable-sorted, filtered page of recorded shifts for the manager hours view (Requirement "
        "18.1). Filter by date range, employee, site, status, an anomaly flag, or manual-only "
        "(Requirement 22.3). Site managers see only the entries at their assigned sites; "
        "administrators and accounting see every site (Requirement 2.3, 2.4). Entries come back "
        "ordered by employee, work date and check-in time, so the view lays out a chronological "
        "per-employee day across sites with per-site and daily totals; each carries its employee and "
        "site names and its manual and anomaly markers."
    ),
)
def list_time_entries(
    caller: HoursReaderCaller,
    session: DbSession,
    date_from: date | None = None,
    date_to: date | None = None,
    employee_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    status: TimeEntryStatus | None = None,
    flag: Annotated[
        list[str] | None,
        Query(description="Anomaly flag(s) to filter on; repeat for several (matched as any-of)."),
    ] = None,
    manual_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TimeEntryListResponse:
    filters = time_entry_service.TimeEntryFilters(
        date_from=date_from,
        date_to=date_to,
        employee_id=employee_id,
        site_id=site_id,
        status=status,
        flags=_clean_flags(flag),
        manual_only=manual_only,
    )
    scoped = caller.scope_query(time_entry_service.base_select(), TimeEntry.site_id)
    page = time_entry_service.list_time_entries(
        session, filters=filters, scope_statement=scoped, limit=limit, offset=offset
    )
    items = [
        TimeEntryListItem(
            id=row.entry.id,
            employee_id=row.entry.employee_id,
            employee_name=row.employee_name,
            employee_name_en=row.employee_name_en,
            site_id=row.entry.site_id,
            site_name=row.site_name,
            work_date=row.entry.work_date,
            check_in_at=row.entry.check_in_at,
            check_out_at=row.entry.check_out_at,
            total_minutes=row.entry.total_minutes,
            source=row.entry.source,
            is_manual=row.entry.is_manual,
            status=row.entry.status,
            flags=list(row.entry.flags),
        )
        for row in page.rows
    ]
    return TimeEntryListResponse(items=items, total=page.total, limit=limit, offset=offset)


def _clean_flags(flags: list[str] | None) -> frozenset[str]:
    """Keep only the anomaly markers the view knows, dropping blanks and unknowns.

    An unknown flag would match no entry and only mislead a reader into thinking they had filtered on
    something real, so it is dropped rather than passed through. An empty result means "do not filter
    on flags", which is the no-filter default (Requirement 22.3).
    """
    if not flags:
        return frozenset()
    return frozenset(value for value in flags if value in _KNOWN_FLAGS)


# --------------------------------------------------------------------------- write: error mapping
# Manual entry, correction and deletion (Requirement 12). The service raises two families of domain
# error: its own `ManualEntryError` (not-found, out-of-order times, empty correction) and the reused
# scan primitives' `ScanError` (a locked period, an overlap). Both carry a machine `code`; this maps
# each onto a status and, where it helps, names the conflict in the envelope.

#: Which HTTP status each write error maps onto. A missing entry, employee or site is a not-found. An
#: out-of-order or empty correction is a bad request — the body itself is wrong. A locked period and
#: an overlap are conflicts: the request is well-formed but collides with the state of the world.
_WRITE_ERROR_STATUS: dict[str, HTTPStatus] = {
    time_entry_service.TimeEntryNotFound.code: HTTPStatus.NOT_FOUND,
    time_entry_service.EmployeeNotFound.code: HTTPStatus.NOT_FOUND,
    time_entry_service.SiteNotFound.code: HTTPStatus.NOT_FOUND,
    time_entry_service.CheckOutNotAfterCheckIn.code: HTTPStatus.BAD_REQUEST,
    time_entry_service.NoTimeFieldToUpdate.code: HTTPStatus.BAD_REQUEST,
    scan_service.PeriodLocked.code: HTTPStatus.CONFLICT,
    scan_service.OverlapRejected.code: HTTPStatus.CONFLICT,
}

#: The machine code returned when a non-administrator sets `override` on a write into a locked month.
#: Only an administrator may override a lock (Requirement 15.5); anyone else is refused before the
#: write is attempted, so a manager cannot smuggle an entry into a frozen month by asking to.
CODE_OVERRIDE_REQUIRES_ADMIN = "override_requires_admin"


def _override_allowed(caller: object, requested: bool) -> bool:
    """Whether this caller may exercise a locked-period override (Requirement 15.5).

    Only an administrator may override a lock. A non-administrator that asked for one is refused with
    a 403 rather than silently ignored, so the caller learns the write was rejected on the override
    rather than believing it succeeded. Returns the override flag to pass to the service — false for
    everyone who did not ask, true only for an administrator who did.
    """
    if not requested:
        return False
    role = getattr(getattr(caller, "user", None), "role", None)
    if role is not UserRole.ADMIN:
        raise api_error(HTTPStatus.FORBIDDEN, CODE_OVERRIDE_REQUIRES_ADMIN)
    return True


def _record_override(
    session,
    entry: TimeEntry,
    *,
    admin_override: bool,
    action: str,
    reason: str,
    context,
) -> None:
    """Record an administrator's write into a locked month, when one actually happened (Req 15.6).

    An override was exercised only when the administrator asked for one *and* the entry's month is in
    fact locked — a write into an open month with `override` set changed nothing about the guard, so
    nothing is recorded. When it was exercised, the override is recorded against the month's
    `period_locks` row under the write's reason, so the month's audit history shows every override,
    what was done and why (Requirement 15.6). Read after the write, when the entry's `work_date` is
    settled; the month stays locked through the write, so the lock check still holds.
    """
    if not admin_override:
        return
    if not scan_service.is_period_locked(session, entry.work_date):
        return
    period_service.record_override(
        session,
        year=entry.work_date.year,
        month=entry.work_date.month,
        action=action,
        reason=reason,
        context=context,
    )


def _raise_for_write(error: Exception) -> HTTPException:
    """Turn a manual-write domain error into the matching HTTP failure, naming the conflict where set.

    An overlap names the conflicting entry so the console can point at the shift the rejected write
    collided with (Requirement 11.7, 12.5), the same envelope shape `app.api.routers.scans` returns;
    `params` is omitted when the conflict could not be identified. Everything else maps by code.
    """
    if isinstance(error, scan_service.OverlapRejected):
        params: dict[str, str] = {}
        conflict = error.conflicting_entry
        if conflict is not None:
            params = {
                "time_entry_id": str(conflict.id),
                "site_id": str(conflict.site_id),
                "check_in_at": conflict.check_in_at.isoformat(),
            }
            if conflict.check_out_at is not None:
                params["check_out_at"] = conflict.check_out_at.isoformat()
        return HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail={"error": {"code": error.code, "params": params}},
        )
    code = getattr(error, "code", "manual_entry_error")
    status = _WRITE_ERROR_STATUS.get(code, HTTPStatus.BAD_REQUEST)
    return api_error(status, code)


# --------------------------------------------------------------------------- create


@router.post(
    "",
    status_code=HTTPStatus.CREATED,
    response_model=TimeEntryResponse,
    summary="Create a time entry by hand",
    description=(
        "Record a completed shift manually when a scan failed (Requirement 12.1). Managers and "
        "administrators only; employees may not (Requirement 12.6), and a site manager may only "
        "record at a site in their scope (Requirement 2.3). A non-blank reason is mandatory "
        "(Requirement 12.3). The entry is marked manual and badged wherever it appears (Requirement "
        "12.4), and is subject to the same overlap, plausibility and period-lock rules as a scanned "
        "entry (Requirement 12.5): an entry overlapping an existing one is rejected naming the "
        "conflict, and a write into a locked month is refused."
    ),
    responses={
        HTTPStatus.CREATED: {"model": TimeEntryResponse, "description": "The created entry"},
        HTTPStatus.NOT_FOUND: {"description": "No such employee or site"},
        HTTPStatus.BAD_REQUEST: {"description": "The check-out is not after the check-in"},
        HTTPStatus.CONFLICT: {"description": "The entry overlaps another, or the month is locked"},
    },
)
def create_time_entry(
    payload: TimeEntryCreate,
    caller: AttendanceWriterCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> TimeEntryResponse:
    # Scope first (Requirement 2.3): a site manager may only record at a site they run. The check
    # audits and raises a 403 before any write is attempted.
    caller.require_site(payload.site_id)
    # Only an administrator may write into a locked month (Requirement 15.5); a non-admin asking is
    # refused here before the write.
    admin_override = _override_allowed(caller, payload.override)
    try:
        entry = time_entry_service.create_manual_entry(
            session, payload, context=context, admin_override=admin_override
        )
        _record_override(
            session, entry, admin_override=admin_override,
            action="manual_create", reason=payload.reason, context=context,
        )
        session.commit()
    except (time_entry_service.ManualEntryError, scan_service.ScanError) as error:
        session.rollback()
        raise _raise_for_write(error) from error
    session.refresh(entry)
    return TimeEntryResponse.model_validate(entry)


# --------------------------------------------------------------------------- correct


@router.patch(
    "/{entry_id}",
    response_model=TimeEntryResponse,
    summary="Correct a time entry's times",
    description=(
        "Edit the check-in or check-out time of an existing entry (Requirement 12.2). Managers and "
        "administrators only (Requirement 12.6); a site manager may only correct an entry at a site "
        "in their scope (Requirement 2.3). A non-blank reason is mandatory (Requirement 12.3). The "
        "edit marks the entry manual wherever it appears even if it was scanned (Requirement 12.4), "
        "recomputes the total, refreshes the implausible-duration flag, and is subject to the same "
        "overlap and period-lock rules as a scanned entry (Requirement 12.5)."
    ),
    responses={
        HTTPStatus.OK: {"model": TimeEntryResponse, "description": "The corrected entry"},
        HTTPStatus.NOT_FOUND: {"description": "No live entry with that id"},
        HTTPStatus.BAD_REQUEST: {"description": "No time field to change, or times out of order"},
        HTTPStatus.CONFLICT: {"description": "The correction overlaps another, or the month is locked"},
    },
)
def correct_time_entry(
    entry_id: uuid.UUID,
    payload: TimeEntryUpdate,
    caller: AttendanceWriterCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> TimeEntryResponse:
    # Load the entry to check its site is in scope before correcting it (Requirement 2.3). A
    # not-found is mapped after the scope check so a manager cannot probe entries outside their sites.
    try:
        existing = time_entry_service.get_live_entry(session, entry_id)
    except time_entry_service.ManualEntryError as error:
        raise _raise_for_write(error) from error
    caller.require_site(existing.site_id)
    admin_override = _override_allowed(caller, payload.override)

    try:
        entry = time_entry_service.correct_manual_entry(
            session, entry_id, payload, context=context, admin_override=admin_override
        )
        _record_override(
            session, entry, admin_override=admin_override,
            action="manual_correct", reason=payload.reason, context=context,
        )
        session.commit()
    except (time_entry_service.ManualEntryError, scan_service.ScanError) as error:
        session.rollback()
        raise _raise_for_write(error) from error
    session.refresh(entry)
    return TimeEntryResponse.model_validate(entry)


# --------------------------------------------------------------------------- delete (soft)


@router.delete(
    "/{entry_id}",
    response_model=TimeEntryResponse,
    summary="Soft-delete a time entry",
    description=(
        "Remove an entry with a mandatory reason, retaining the row for audit (Requirement 12.7). "
        "Never a hard delete: `deleted_at` and `delete_reason` are set and the record stays. Managers "
        "and administrators only (Requirement 12.6); a site manager may only delete an entry at a "
        "site in their scope (Requirement 2.3). A write into a locked month is refused (Requirement "
        "15.5). A blank reason is rejected (Requirement 12.3)."
    ),
    responses={
        HTTPStatus.OK: {"model": TimeEntryResponse, "description": "The soft-deleted entry"},
        HTTPStatus.NOT_FOUND: {"description": "No live entry with that id"},
        HTTPStatus.CONFLICT: {"description": "The month is locked"},
    },
)
def delete_time_entry(
    entry_id: uuid.UUID,
    payload: TimeEntryDelete,
    caller: AttendanceWriterCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> TimeEntryResponse:
    try:
        existing = time_entry_service.get_live_entry(session, entry_id)
    except time_entry_service.ManualEntryError as error:
        raise _raise_for_write(error) from error
    caller.require_site(existing.site_id)
    admin_override = _override_allowed(caller, payload.override)

    try:
        entry = time_entry_service.soft_delete_entry(
            session, entry_id, reason=payload.reason, context=context, admin_override=admin_override
        )
        _record_override(
            session, entry, admin_override=admin_override,
            action="manual_delete", reason=payload.reason, context=context,
        )
        session.commit()
    except (time_entry_service.ManualEntryError, scan_service.ScanError) as error:
        session.rollback()
        raise _raise_for_write(error) from error
    session.refresh(entry)
    return TimeEntryResponse.model_validate(entry)


# --------------------------------------------------------------------------- bulk status change
# Advancing a set of entries along the approval ladder (Requirement 15.2, 15.3). Managers and
# administrators write status; a manager is scoped to their sites and may only step forward, an
# administrator may also reverse one rung with a reason. The service is the authority for what a
# transition may do — see `app.services.period`.

#: Which HTTP status each status-change error maps onto. An illegal transition or a reversal by a
#: non-admin is a 409 conflict: the request is well-formed but collides with the ladder's rules. A
#: reversal missing its reason is a 400 — the body itself is incomplete.
_STATUS_ERROR_STATUS: dict[str, HTTPStatus] = {
    period_service.InvalidTransition.code: HTTPStatus.CONFLICT,
    period_service.ReversalRequiresAdmin.code: HTTPStatus.CONFLICT,
    period_service.ReversalRequiresReason.code: HTTPStatus.BAD_REQUEST,
}


def _raise_for_status(error: period_service.PeriodError) -> HTTPException:
    """Turn a status-change domain error into the matching HTTP failure, naming the statuses.

    An illegal transition names the from/to statuses in the envelope so the front end can explain
    which move was refused, matching the envelope shape the other write endpoints return.
    """
    if isinstance(error, period_service.InvalidTransition):
        return HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail={
                "error": {
                    "code": error.code,
                    "params": {
                        "from_status": error.from_status.value,
                        "to_status": error.to_status.value,
                    },
                }
            },
        )
    status = _STATUS_ERROR_STATUS.get(getattr(error, "code", ""), HTTPStatus.BAD_REQUEST)
    return api_error(status, getattr(error, "code", "period_error"))


@router.post(
    "/bulk-status",
    response_model=BulkStatusResponse,
    summary="Advance a set of entries along the approval ladder",
    description=(
        "Move the given entries to a target status along Draft → Review → Approved → Locked "
        "(Requirement 15.2). Managers and administrators only; a site manager may move only their "
        "sites' entries and only forward (Requirement 15.3), scoped to their sites in the query "
        "(Requirement 2.3). An administrator may also reverse one rung, which requires a reason "
        "recorded in the audit log (Requirement 15.6). An illegal transition — a jump of more than "
        "one rung, or a reversal by a non-admin — is refused and nothing moves. Entries already at "
        "the target are skipped, so a repeat is idempotent."
    ),
    responses={
        HTTPStatus.OK: {"model": BulkStatusResponse, "description": "The entries that moved"},
        HTTPStatus.BAD_REQUEST: {"description": "A reversal was requested without a reason"},
        HTTPStatus.CONFLICT: {"description": "An illegal transition, or a reversal by a non-admin"},
    },
)
def bulk_change_status(
    payload: BulkStatusRequest,
    caller: AttendanceWriterCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> BulkStatusResponse:
    is_admin = caller.user.role is UserRole.ADMIN
    scoped = caller.scope_query(time_entry_service.base_select(), TimeEntry.site_id)
    try:
        outcome = period_service.bulk_change_status(
            session,
            entry_ids=payload.entry_ids,
            target_status=payload.target_status,
            scope_statement=scoped,
            is_admin=is_admin,
            reason=payload.reason,
            context=context,
        )
        session.commit()
    except period_service.PeriodError as error:
        session.rollback()
        raise _raise_for_status(error) from error
    return BulkStatusResponse(
        updated_count=len(outcome.updated_ids),
        updated_ids=outcome.updated_ids,
        target_status=outcome.target_status,
    )
