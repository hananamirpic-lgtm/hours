"""Period router — locking and unlocking a calendar month (Requirement 15.4, 15.6, 15.7).

HTTP only: guard, validate the path and body, call the period service, map a refusal onto a status
code and the error envelope, and shape the response. The service owns every rule and never commits,
so this module commits after a successful lock or unlock — the status changes it made and the audit
rows ride along in one transaction (Requirement 13.2), the pattern the scan and time-entry routers
follow.

Locking a month is an administrator action (Requirement 15.4), so every endpoint here is behind
`OperationsCaller`. Accounting reads approved hours and reports but does not freeze the ledger; a site
manager approves their own sites' entries through the bulk-status endpoint but does not lock the
whole month. `GET /api/periods` is admin-only too: the period screen it feeds is the administrator's.

* `POST /api/periods/{year}/{month}/lock` — freeze the month's Approved entries. When the month still
  holds unapproved entries and the call did not force, it returns 200 with a *warning* body listing
  them rather than an error, because the administrator's next move is to act on that list, not to
  retry a failed request (Requirement 15.7).
* `POST /api/periods/{year}/{month}/unlock` — reopen a locked month with a mandatory reason (15.6).
* `GET /api/periods` — the months the workflow has touched and their state.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path

from app.api.deps import (
    AuthenticatedContext,
    DbSession,
    OperationsCaller,
    api_error,
)
from app.models.period_lock import PeriodLock
from app.schemas.period import (
    PeriodListResponse,
    PeriodLockRequest,
    PeriodLockResult,
    PeriodState,
    PeriodUnlockRequest,
    UnapprovedEntry,
)
from app.services import period as period_service

router = APIRouter(prefix="/periods", tags=["periods"])

#: Bounds for the month in the path. A year outside this range or a month outside 1–12 is a
#: validation error, matching the `period_locks` check constraints so a bad path never reaches a row.
_YEAR = Annotated[int, Path(ge=2000, le=2200)]
_MONTH = Annotated[int, Path(ge=1, le=12)]

#: Which HTTP status each period error maps onto. Locking a locked month or unlocking an open one is a
#: 409 conflict: the request is well-formed but collides with the month's current state.
_ERROR_STATUS: dict[str, HTTPStatus] = {
    period_service.PeriodAlreadyLocked.code: HTTPStatus.CONFLICT,
    period_service.PeriodNotLocked.code: HTTPStatus.CONFLICT,
}


def _raise_for(error: period_service.PeriodError) -> HTTPException:
    status = _ERROR_STATUS.get(getattr(error, "code", ""), HTTPStatus.BAD_REQUEST)
    return api_error(status, getattr(error, "code", "period_error"))


def _state_of(period: PeriodLock) -> PeriodState:
    """Shape a `period_locks` row into the state the front end reads."""
    return PeriodState(
        year=period.year,
        month=period.month,
        is_locked=period.is_locked,
        locked_at=period.locked_at,
        locked_by_user_id=period.locked_by_user_id,
        unlocked_at=period.unlocked_at,
        unlocked_by_user_id=period.unlocked_by_user_id,
        unlock_reason=period.unlock_reason,
    )


# --------------------------------------------------------------------------- lock


@router.post(
    "/{year}/{month}/lock",
    response_model=PeriodLockResult,
    summary="Lock a calendar month",
    description=(
        "Freeze the month's Approved entries, setting them to Locked (Requirement 15.4). "
        "Administrators only. When the month still holds entries that are not Approved, the first "
        "call returns a warning listing them and locks nothing (Requirement 15.7); repeat with "
        "`force` true to lock the Approved entries and leave the rest. A month that is already locked "
        "is refused."
    ),
    responses={
        HTTPStatus.OK: {"model": PeriodLockResult, "description": "Locked, or a warning to act on"},
        HTTPStatus.CONFLICT: {"description": "The month is already locked"},
    },
)
def lock_period(
    year: _YEAR,
    month: _MONTH,
    payload: PeriodLockRequest,
    caller: OperationsCaller,  # noqa: ARG001 — the type is the guard; the caller is unused past it
    session: DbSession,
    context: AuthenticatedContext,
) -> PeriodLockResult:
    try:
        outcome = period_service.lock_period(
            session, year=year, month=month, force=payload.force, context=context
        )
        session.commit()
    except period_service.PeriodError as error:
        session.rollback()
        raise _raise_for(error) from error

    return PeriodLockResult(
        locked=outcome.locked,
        year=year,
        month=month,
        locked_count=outcome.locked_count,
        unapproved=[UnapprovedEntry.model_validate(entry) for entry in outcome.unapproved],
        state=_state_of(outcome.period) if outcome.period is not None else None,
    )


# --------------------------------------------------------------------------- unlock


@router.post(
    "/{year}/{month}/unlock",
    response_model=PeriodState,
    summary="Unlock a calendar month",
    description=(
        "Reopen a locked month so entries in it may be written again (Requirement 15.6). "
        "Administrators only. A mandatory reason is recorded in the audit log. Locked entries are "
        "left Locked — an unlock reopens the month, it does not walk every entry back down the "
        "ladder. A month that is not currently locked is refused."
    ),
    responses={
        HTTPStatus.OK: {"model": PeriodState, "description": "The reopened month's state"},
        HTTPStatus.CONFLICT: {"description": "The month is not locked"},
    },
)
def unlock_period(
    year: _YEAR,
    month: _MONTH,
    payload: PeriodUnlockRequest,
    caller: OperationsCaller,  # noqa: ARG001 — the type is the guard
    session: DbSession,
    context: AuthenticatedContext,
) -> PeriodState:
    try:
        period = period_service.unlock_period(
            session, year=year, month=month, reason=payload.reason, context=context
        )
        session.commit()
    except period_service.PeriodError as error:
        session.rollback()
        raise _raise_for(error) from error
    return _state_of(period)


# --------------------------------------------------------------------------- list


@router.get(
    "",
    response_model=PeriodListResponse,
    summary="List the months the workflow has touched",
    description=(
        "The calendar months that have been locked or unlocked, most recent first, with each month's "
        "current state (Requirement 15.4). Administrators only; feeds the admin period screen."
    ),
)
def list_periods(
    caller: OperationsCaller,  # noqa: ARG001 — the type is the guard
    session: DbSession,
) -> PeriodListResponse:
    periods = period_service.list_periods(session)
    return PeriodListResponse(items=[_state_of(period) for period in periods])
