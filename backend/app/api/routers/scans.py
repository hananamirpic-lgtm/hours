"""Scan router (Requirement 9, 7.3, 7.4, 23.1).

HTTP only: guard, validate the schema, resolve the caller's employee, call the scan service, map a
refusal onto a status code and the error envelope, and shape the response. The service owns every
rule and never commits, so this module commits after a successful write — the audit rows the service
added ride along with it, keeping a change and its audit record in one transaction (Requirement
13.2). This mirrors `app.api.routers.sites`.

Two endpoints:

* `POST /api/scans` — the unified scan. The employee presents a QR token; the server decides check-in
  versus check-out versus conflict from their state and writes the result with a server-side
  timestamp. There is no location field in the request model and `extra="forbid"` rejects one that is
  sent (Requirement 9.6).
* `POST /api/scans/checkout` — close the current open shift explicitly, the button path (Req 10.6).
* `POST /api/scans/transition` — the confirmed move: close the current shift and open one at the
  target site in one transaction (Requirement 11.5, 11.8).
* `POST /api/scans/end-and-move` — "End work and move to another site": close the current shift with
  no departure QR (Requirement 11.6).
* `GET /api/scans/status` — the caller's current open shift, or its absence, for the employee home
  screen (Requirement 23.1).
* `GET /api/scans/history` — the caller's own recent completed work by day, newest first, for the
  employee home screen's recent-work section (Requirement 23.3).

Both are the employee's own record. The guard admits the employee role (and, per Requirement 2.2, the
administrator); the caller's linked `employee_id` is what the scan acts on, so a login with none is
told it has no attendance to record rather than allowed to scan as nobody.
"""

from __future__ import annotations

from datetime import date
from http import HTTPStatus

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import (
    AuthenticatedContext,
    DbSession,
    EmployeeCaller,
    api_error,
)
from app.core.rate_limit import rate_limit_scan
from app.schemas.scan import (
    AssignedSite,
    MySitesResponse,
    OpenShift,
    ScanRequest,
    ScanResult,
    ScanStatusResponse,
    SelfCheckInRequest,
    WorkHistoryDay,
    WorkHistoryResponse,
)
from app.services import scan as scan_service

# The scan endpoints carry a per-client rate limit (Requirement 20.4) in addition to the per-employee
# duplicate-submission window in the service: a stolen or shared QR replayed in a loop is stopped by
# the ceiling here before it reaches the state machine. Applied at the router so every scan write, and
# the status read, is covered without each endpoint having to remember it.
router = APIRouter(prefix="/scans", tags=["scans"], dependencies=[Depends(rate_limit_scan)])


# --------------------------------------------------------------------------- error mapping

#: Which HTTP status each scan-service error maps onto. An invalid token is a bad request — the body
#: is wrong. The guard refusals (employee, site, period, strict assignment) are 409 conflicts: the
#: request is well-formed but collides with the current state of the world. A login with no employee
#: is a 403 — the caller is authenticated but has no attendance of its own.
_ERROR_STATUS: dict[str, HTTPStatus] = {
    scan_service.InvalidQr.code: HTTPStatus.BAD_REQUEST,
    scan_service.NoEmployeeForCaller.code: HTTPStatus.FORBIDDEN,
    scan_service.EmployeeNotActive.code: HTTPStatus.CONFLICT,
    scan_service.SiteNotActive.code: HTTPStatus.CONFLICT,
    scan_service.PeriodLocked.code: HTTPStatus.CONFLICT,
    scan_service.UnassignedSiteRejected.code: HTTPStatus.CONFLICT,
    scan_service.SelfCheckInSiteNotAssigned.code: HTTPStatus.CONFLICT,
    scan_service.OpenShiftElsewhere.code: HTTPStatus.CONFLICT,
    scan_service.NoOpenShift.code: HTTPStatus.CONFLICT,
    scan_service.CheckOutNotAfterCheckIn.code: HTTPStatus.CONFLICT,
    scan_service.SameSiteTransition.code: HTTPStatus.CONFLICT,
    scan_service.OverlapRejected.code: HTTPStatus.CONFLICT,
}


def _raise_for(error: scan_service.ScanError) -> HTTPException:
    """Turn a scan-service error into the matching HTTP failure, with params where they help.

    The open-shift conflict is the one that carries a body worth reading: it names the other site and
    offers the two actions the employee may take (Requirement 11.4), matching the envelope shape in
    the design.
    """
    if isinstance(error, scan_service.OpenShiftElsewhere):
        return HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail={
                "error": {
                    "code": error.code,
                    "params": {
                        "site_id": str(error.other_site.id),
                        "site_name": error.other_site.name,
                        "since": error.open_entry.check_in_at.isoformat(),
                    },
                    "actions": ["transition", "cancel"],
                }
            },
        )
    if isinstance(error, scan_service.OverlapRejected):
        # Name the conflicting entry where the service could recover it (Requirement 11.7), so the
        # front end can point at the shift the rejected write collided with rather than showing a
        # bare code. `params` is omitted when the conflict could not be identified.
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
    status = _ERROR_STATUS.get(error.code, HTTPStatus.BAD_REQUEST)
    return api_error(status, error.code)


# --------------------------------------------------------------------------- scan


@router.post(
    "",
    response_model=ScanResult,
    summary="Record a scan",
    description=(
        "Resolve a presented site QR token to a check-in, a check-out decision, or a conflict. The "
        "recorded time is the server's; the request carries no timestamp and no location field, and "
        "an unknown field such as latitude is rejected (Requirement 9.2, 9.6). An identical repeat "
        "within the duplicate window returns the existing entry rather than a second one "
        "(Requirement 9.3). An open shift at a different site returns 409 open_shift_elsewhere "
        "naming that site (Requirement 11.4)."
    ),
    responses={
        HTTPStatus.OK: {"model": ScanResult, "description": "The scan outcome"},
        HTTPStatus.BAD_REQUEST: {"description": "The token is invalid, unknown or revoked"},
        HTTPStatus.FORBIDDEN: {"description": "The caller is not linked to an employee"},
        HTTPStatus.CONFLICT: {
            "description": (
                "A guard refused the scan: employee or site inactive, period locked, strict-mode "
                "site, or an open shift at another site"
            )
        },
    },
)
def record_scan(
    payload: ScanRequest,
    caller: EmployeeCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> ScanResult:
    try:
        outcome = scan_service.resolve_scan(
            session,
            employee_id=caller.user.employee_id,
            request=payload,
            context=context,
        )
        session.commit()
    except scan_service.ScanError as error:
        session.rollback()
        raise _raise_for(error) from error

    return _result_of(outcome)


def _result_of(outcome: scan_service.ScanOutcome) -> ScanResult:
    """Shape a resolved scan or check-out into the response model.

    Shared by the unified scan and the explicit check-out so both report the outcome the same way:
    which entry, at which site, what happened, the server time of the action, whether a shift is still
    open, and any anomaly flag the entry carries (an unassigned-site check-in, or an implausible
    duration on close).
    """
    entry = outcome.entry
    return ScanResult(
        time_entry_id=entry.id,
        site_id=entry.site_id,
        action=outcome.action,
        at=outcome.at,
        work_date=entry.work_date,
        is_open=entry.check_out_at is None,
        flags=list(entry.flags),
    )


# --------------------------------------------------------------------------- explicit check-out


@router.post(
    "/checkout",
    response_model=ScanResult,
    summary="Check out of the current open shift",
    description=(
        "Close the caller's current open shift explicitly, without a QR — the button path for ending "
        "a shift (Requirement 10.6). The check-out time is the server's; worked duration is stored as "
        "whole minutes (Requirement 10.1, 10.2). A shift longer than the implausible-duration "
        "threshold is flagged for manager review rather than accepted silently (Requirement 10.5). "
        "With no open shift the request is refused with no_open_shift (Requirement 10.6)."
    ),
    responses={
        HTTPStatus.OK: {"model": ScanResult, "description": "The closed entry"},
        HTTPStatus.FORBIDDEN: {"description": "The caller is not linked to an employee"},
        HTTPStatus.CONFLICT: {"description": "No open shift, or the check-out is not after check-in"},
    },
)
def check_out(
    caller: EmployeeCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> ScanResult:
    try:
        outcome = scan_service.check_out(
            session,
            employee_id=caller.user.employee_id,
            context=context,
        )
        session.commit()
    except scan_service.ScanError as error:
        session.rollback()
        raise _raise_for(error) from error

    return _result_of(outcome)


# --------------------------------------------------------------------------- self check-in (no QR)


@router.post(
    "/self-check-in",
    response_model=ScanResult,
    summary="Open a shift without a QR, by picking a site",
    description=(
        "The fallback when there is no QR to scan — the camera is broken, or no code is reachable — "
        "so the employee opens a shift by picking one of their assigned sites instead. Because "
        "nothing proves the employee is at the site, the entry is recorded as manual, flagged "
        "self_reported, and left Draft: it must be approved by a manager and never auto-approves on "
        "check-out. The same guards a scan check-in runs apply — active employee, active site, open "
        "period — with one stricter rule: the employee may only self-check-in to a site they are "
        "assigned to (the assignment is the presence signal a QR would otherwise be), so an "
        "unassigned site is refused. An open shift at another site returns 409 open_shift_elsewhere "
        "naming it, so the app can offer the transition."
    ),
    responses={
        HTTPStatus.OK: {"model": ScanResult, "description": "The opened self-reported shift"},
        HTTPStatus.FORBIDDEN: {"description": "The caller is not linked to an employee"},
        HTTPStatus.CONFLICT: {
            "description": (
                "A guard refused: employee or site inactive, period locked, the site is not one the "
                "employee is assigned to, or a shift is already open at another site"
            )
        },
    },
)
def self_check_in(
    payload: SelfCheckInRequest,
    caller: EmployeeCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> ScanResult:
    try:
        outcome = scan_service.self_check_in(
            session,
            employee_id=caller.user.employee_id,
            site_id=payload.site_id,
            context=context,
        )
        session.commit()
    except scan_service.ScanError as error:
        session.rollback()
        raise _raise_for(error) from error

    return _result_of(outcome)


# --------------------------------------------------------------------------- my assigned sites


@router.get(
    "/my-sites",
    response_model=MySitesResponse,
    summary="The caller's own assigned sites, for the self-check-in picker",
    description=(
        "Returns the active sites the caller's linked employee is assigned to — each an id and a "
        "name only — so the mobile app can offer a site picker for a no-QR self check-in "
        "(Requirement 7.1). Always self-scoped: the caller's own assignments only, resolved from "
        "their linked employee, never another person's, and no employee identifier is accepted from "
        "the client. A caller with no linked employee or no assignments gets an empty list."
    ),
)
def my_sites(
    caller: EmployeeCaller,
    session: DbSession,
) -> MySitesResponse:
    sites = scan_service.my_assigned_sites(session, caller.user.employee_id)
    return MySitesResponse(
        sites=[AssignedSite(id=site.id, name=site.name) for site in sites]
    )


# --------------------------------------------------------------------------- transition


@router.post(
    "/transition",
    response_model=ScanResult,
    summary="Move to another site, closing the current shift",
    description=(
        "The confirmed answer to an open_shift_elsewhere conflict (Requirement 11.5). In one "
        "transaction it closes the current open shift at the server time — marked as a system "
        "transition and audited with the transition as its reason (Requirement 11.8) — and opens a "
        "new shift at the site the presented token resolves to, so the day has no gap and no overlap "
        "at the seam. The new shift runs the ordinary check-in guards, so a transition into an "
        "inactive or strict-mode site is refused as a plain check-in there would be. With no shift "
        "open it behaves as a plain check-in at the target."
    ),
    responses={
        HTTPStatus.OK: {"model": ScanResult, "description": "The newly opened shift at the target"},
        HTTPStatus.BAD_REQUEST: {"description": "The target token is invalid, unknown or revoked"},
        HTTPStatus.FORBIDDEN: {"description": "The caller is not linked to an employee"},
        HTTPStatus.CONFLICT: {
            "description": (
                "A guard refused the new shift, the target is the site already open, or a write "
                "would overlap an existing entry"
            )
        },
    },
)
def transition(
    payload: ScanRequest,
    caller: EmployeeCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> ScanResult:
    try:
        outcome = scan_service.transition(
            session,
            employee_id=caller.user.employee_id,
            request=payload,
            context=context,
        )
        session.commit()
    except scan_service.ScanError as error:
        session.rollback()
        raise _raise_for(error) from error

    return _result_of(outcome)


# --------------------------------------------------------------------------- end work and move


@router.post(
    "/end-and-move",
    response_model=ScanResult,
    summary="End the current shift to move on, without a departure QR",
    description=(
        "The 'End work and move to another site' action (Requirement 11.6): closes the caller's "
        "current open shift at the server time without requiring the departure QR, marking it a "
        "system transition and audited as such (Requirement 11.8). No new shift is opened — the "
        "employee opens the next by scanning in where they move to. With no open shift the request "
        "is refused with no_open_shift."
    ),
    responses={
        HTTPStatus.OK: {"model": ScanResult, "description": "The closed entry"},
        HTTPStatus.FORBIDDEN: {"description": "The caller is not linked to an employee"},
        HTTPStatus.CONFLICT: {"description": "No open shift to end"},
    },
)
def end_and_move(
    caller: EmployeeCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> ScanResult:
    try:
        outcome = scan_service.end_work_and_move(
            session,
            employee_id=caller.user.employee_id,
            context=context,
        )
        session.commit()
    except scan_service.ScanError as error:
        session.rollback()
        raise _raise_for(error) from error

    return _result_of(outcome)


# --------------------------------------------------------------------------- status


@router.get(
    "/status",
    response_model=ScanStatusResponse,
    summary="The caller's current open shift",
    description=(
        "Returns the employee's open shift — site and check-in time — or its absence, so the "
        "employee home screen can show check-out when a shift is open and scan-to-check-in when not "
        "(Requirement 23.1, 23.2)."
    ),
)
def scan_status(
    caller: EmployeeCaller,
    session: DbSession,
) -> ScanStatusResponse:
    entry = scan_service.current_open_shift(session, caller.user.employee_id)
    if entry is None:
        return ScanStatusResponse(open_shift=None)
    return ScanStatusResponse(
        open_shift=OpenShift(
            time_entry_id=entry.id,
            site_id=entry.site_id,
            check_in_at=entry.check_in_at,
            work_date=entry.work_date,
            flags=list(entry.flags),
        )
    )


# --------------------------------------------------------------------------- history


@router.get(
    "/history",
    response_model=WorkHistoryResponse,
    summary="The caller's own recent work by day",
    description=(
        "Returns the employee's own completed days — each a work date and the whole minutes worked "
        "that day, summed across every site (Requirement 11.2) — newest first. Only completed shifts "
        "count; an open shift does not appear. Read-only and always self-scoped: the caller's own "
        "record only, resolved from their linked employee, never another person's — no employee "
        "identifier is accepted from the client.\n\n"
        "With no query parameters it returns the most recent days (capped), for the employee home "
        "screen's recent-work section (Requirement 23.3). Given the optional `date_from` and/or "
        "`date_to` (each an ISO `YYYY-MM-DD` date), it filters `work_date` to that **inclusive** "
        "range and returns every matching day with no cap, for the my-hours screen. An inverted "
        "range (`date_from` after `date_to`) returns no days rather than an error."
    ),
)
def work_history(
    caller: EmployeeCaller,
    session: DbSession,
    date_from: date | None = None,
    date_to: date | None = None,
) -> WorkHistoryResponse:
    days = scan_service.recent_work_history(
        session,
        caller.user.employee_id,
        date_from=date_from,
        date_to=date_to,
    )
    return WorkHistoryResponse(
        days=[
            WorkHistoryDay(work_date=day.work_date, total_minutes=day.total_minutes)
            for day in days
        ]
    )
