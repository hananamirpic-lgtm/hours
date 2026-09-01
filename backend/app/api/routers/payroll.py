"""Payroll router — calculate and read monthly payroll (Requirement 16).

HTTP only: guard, validate the path and body, call the payroll service, map a refusal onto a status
code and the error envelope, and shape the response. The service owns every rule and never commits, so
`POST /api/payroll/calculate` commits here — the record it upserts and the allocations it rebuilds
ride along in one transaction (Requirement 13.2), the pattern the period and time-entry routers follow.

Payroll is wage data, so every endpoint here is behind `FinanceCaller`, which admits administrators
and accounting only (Requirement 2.5, 2.6). A site manager never receives wage fields (Requirement
2.5) and so has no business on any of these endpoints; the employee role is likewise absent. Nothing
is redacted below because the caller is already a finance role — the guard, not a per-field strip, is
what keeps wages from the wrong reader here.

* `POST /api/payroll/calculate` — compute an employee's month from its Approved or Locked entries
  (Requirement 15.8), replacing any existing draft rather than duplicating it (Requirement 16.10).
* `GET /api/payroll` — a page of records for a period (Requirement 16.6).
* `GET /api/payroll/{employee_id}/{year}/{month}` — one record with its per-site allocation
  (Requirement 16.8).
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query

from app.api.deps import (
    DbSession,
    FinanceCaller,
    api_error,
)
from app.schemas.payroll import (
    PayrollCalculateRequest,
    PayrollListResponse,
    PayrollRecordResponse,
)
from app.services import payroll as payroll_service

router = APIRouter(prefix="/payroll", tags=["payroll"])

#: Bounds for the year and month in a path or a filter. A value outside these ranges is a validation
#: error, matching the `payroll_records` check constraints so a bad value never reaches a row.
_YEAR = Annotated[int, Path(ge=2000, le=2200)]
_MONTH = Annotated[int, Path(ge=1, le=12)]

#: Which HTTP status each payroll error maps onto. A missing employee, missing record, or a gap in the
#: rate history is a not-found or a bad request — the request is well-formed but the data it names is
#: absent or incomplete.
_ERROR_STATUS: dict[str, HTTPStatus] = {
    payroll_service.EmployeeNotFound.code: HTTPStatus.NOT_FOUND,
    payroll_service.PayrollRecordNotFound.code: HTTPStatus.NOT_FOUND,
    payroll_service.MissingRate.code: HTTPStatus.BAD_REQUEST,
}


def _raise_for(error: payroll_service.PayrollError) -> HTTPException:
    status = _ERROR_STATUS.get(getattr(error, "code", ""), HTTPStatus.BAD_REQUEST)
    return api_error(status, getattr(error, "code", "payroll_error"))


# --------------------------------------------------------------------------- calculate


@router.post(
    "/calculate",
    response_model=PayrollRecordResponse,
    summary="Calculate an employee's monthly payroll",
    description=(
        "Compute one employee's pay for a month from its Approved or Locked entries (Requirement "
        "15.8), classifying each day into regular, overtime, Shabbat and holiday buckets, pricing "
        "each at the rate in force on the work date (Requirement 16.9), and allocating the cost per "
        "site so the allocations sum to the total exactly (Requirement 16.8). Administrators and "
        "accounting only. Recalculating replaces the existing draft rather than duplicating it "
        "(Requirement 16.10); a record for a locked month is final."
    ),
    responses={
        HTTPStatus.OK: {"model": PayrollRecordResponse, "description": "The calculated record"},
        HTTPStatus.NOT_FOUND: {"description": "No such employee"},
        HTTPStatus.BAD_REQUEST: {"description": "A payable day has no rate in force"},
    },
)
def calculate_payroll(
    payload: PayrollCalculateRequest,
    caller: FinanceCaller,  # noqa: ARG001 — the type is the guard; the caller is unused past it
    session: DbSession,
) -> PayrollRecordResponse:
    inputs = payroll_service.PayrollInputs(
        travel=payload.travel, bonuses=payload.bonuses, deductions=payload.deductions
    )
    try:
        record = payroll_service.calculate_payroll(
            session,
            employee_id=payload.employee_id,
            year=payload.year,
            month=payload.month,
            inputs=inputs,
        )
        session.commit()
    except payroll_service.PayrollError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(record)
    return PayrollRecordResponse.model_validate(record)


# --------------------------------------------------------------------------- list


@router.get(
    "",
    response_model=PayrollListResponse,
    summary="List payroll records for a period",
    description=(
        "A stable-sorted page of payroll records, filtered by year, month or employee (Requirement "
        "16.6). Administrators and accounting only. Each record carries its per-site allocation."
    ),
)
def list_payroll(
    caller: FinanceCaller,  # noqa: ARG001 — the type is the guard
    session: DbSession,
    year: Annotated[int | None, Query(ge=2000, le=2200)] = None,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
    employee_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PayrollListResponse:
    page = payroll_service.list_records(
        session,
        year=year,
        month=month,
        employee_id=employee_id,
        limit=limit,
        offset=offset,
    )
    return PayrollListResponse(
        items=[PayrollRecordResponse.model_validate(record) for record in page.items],
        total=page.total,
        limit=limit,
        offset=offset,
    )


# --------------------------------------------------------------------------- read one


@router.get(
    "/{employee_id}/{year}/{month}",
    response_model=PayrollRecordResponse,
    summary="Read one employee's monthly payroll record",
    description=(
        "One employee's payroll record for a month, with its per-site cost allocation (Requirement "
        "16.6, 16.8). Administrators and accounting only. A month with no record is a not-found."
    ),
    responses={
        HTTPStatus.OK: {"model": PayrollRecordResponse, "description": "The record"},
        HTTPStatus.NOT_FOUND: {"description": "No record for that employee and month"},
    },
)
def get_payroll_record(
    employee_id: uuid.UUID,
    year: _YEAR,
    month: _MONTH,
    caller: FinanceCaller,  # noqa: ARG001 — the type is the guard
    session: DbSession,
) -> PayrollRecordResponse:
    try:
        record = payroll_service.get_record(
            session, employee_id=employee_id, year=year, month=month
        )
    except payroll_service.PayrollError as error:
        raise _raise_for(error) from error
    return PayrollRecordResponse.model_validate(record)
