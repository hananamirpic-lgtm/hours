"""Employee router (Requirement 3).

HTTP only: guard, validate, call the service, map a refusal onto a status code and the error
envelope, redact the response for the caller. The service owns every rule and never commits, so this
module commits after a successful write — the audit rows the service added ride along with it, which
is what keeps a change and its audit record in one transaction (Requirement 13.2).

Redaction is applied at every return through `caller.redact`, so a site manager never receives wage
fields (Requirement 2.5) whether they read one card or a list. It is safe to call unconditionally: a
finance role gets the payload back unchanged.

**Why these handlers return a plain dict rather than a response model.** `redact` strips keys from a
serialised payload — a mapping or a list of them — and returns a new structure with the wage keys
gone. If a handler then declared `response_model=EmployeeResponse`, FastAPI would re-validate that
structure against the model and put every stripped optional field back as `null` (or `rates` back as
`[]`), quietly undoing the redaction. So the wage-bearing endpoints serialise to a JSON-able dict,
redact that, and return it as-is; the schema is documented through `responses` instead. This mirrors
how the authorization matrix's probe endpoints return their redacted payloads.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import (
    AdminCaller,
    AuthenticatedContext,
    DbSession,
    PersonnelReaderCaller,
    api_error,
)
from app.models.employee import Employee, EmployeeStatus
from app.schemas.employee import (
    EmployeeCreate,
    EmployeeListItem,
    EmployeeListResponse,
    EmployeeRatesUpdate,
    EmployeeResponse,
    EmployeeStatusUpdate,
    EmployeeUpdate,
)
from app.schemas.site import EmployeeSitesResponse, EmployeeSitesUpdate
from app.services import document as document_service
from app.services import employee as employee_service
from app.services import site as site_service

router = APIRouter(prefix="/employees", tags=["employees"])


# --------------------------------------------------------------------------- error mapping

#: Which HTTP status each service error maps onto. A duplicate passport is a conflict — the request is
#: well-formed but collides with existing state. An invalid status transition is a conflict for the
#: same reason: the resource is not in a state that accepts the change. An overlapping or malformed
#: rate is a bad request — the body itself is wrong and a different body would work.
_ERROR_STATUS: dict[str, HTTPStatus] = {
    employee_service.EmployeeNotFound.code: HTTPStatus.NOT_FOUND,
    employee_service.DuplicatePassport.code: HTTPStatus.CONFLICT,
    employee_service.InvalidStatusTransition.code: HTTPStatus.CONFLICT,
    employee_service.OverlappingRates.code: HTTPStatus.BAD_REQUEST,
    employee_service.InvalidRatePeriod.code: HTTPStatus.BAD_REQUEST,
}


def _raise_for(error: employee_service.EmployeeError) -> HTTPException:
    """Turn a service error into the matching HTTP failure, with params where they help the client.

    A duplicate passport names the conflicting employee, which Requirement 3.6 asks for; the front end
    renders "already used by <name>" from the id rather than from server prose.
    """
    status = _ERROR_STATUS.get(error.code, HTTPStatus.BAD_REQUEST)
    if isinstance(error, employee_service.DuplicatePassport):
        return _conflict_with_params(
            error.code,
            {
                "employee_id": str(error.conflicting.id),
                "full_name": error.conflicting.full_name,
            },
        )
    if isinstance(error, employee_service.InvalidStatusTransition):
        return _conflict_with_params(
            error.code,
            {"current": error.current.value, "requested": error.requested.value},
        )
    return api_error(status, error.code)


def _conflict_with_params(code: str, params: dict[str, str]) -> HTTPException:
    """A 409 carrying the error envelope with `params`, so the client can name what collided."""
    return HTTPException(
        status_code=HTTPStatus.CONFLICT,
        detail={"error": {"code": code, "params": params}},
    )


# --------------------------------------------------------------------------- serialisation


def _card_dict(
    employee: Employee, caller: PersonnelReaderCaller | AdminCaller, session: DbSession
) -> dict[str, Any]:
    """The employee card as a JSON-able dict, with today's rate flattened on and wage fields redacted.

    The current rate's figures are copied onto the top-level wage fields for convenience; both they
    and the full `rates` list are named to match `WAGE_FIELDS`, so `caller.redact` removes them for a
    site manager. When no rate is in force today the wage fields are absent, which a finance reader
    sees as "no rate set" rather than an error. Returned as a dict so the redaction is not undone by a
    response model re-adding the stripped keys — see the module note.

    The expired-document flag (Requirement 4.5) is computed here against today, so every reader —
    including a site manager, since it is not a wage field — sees whether the employee holds an
    expired permit without the card loading the documents themselves.
    """
    response = EmployeeResponse.model_validate(employee)
    today = datetime.now(UTC).date()
    current = employee_service.resolve_rate(employee, today)
    if current is not None:
        response.hourly_wage = current.hourly_wage
        response.overtime_rate = current.overtime_rate
        response.shabbat_holiday_rate = current.shabbat_holiday_rate
        response.travel_allowance_daily = current.travel_allowance_daily
    response.has_expired_document = document_service.has_expired_document(
        session, employee.id, on_date=today
    )
    return caller.redact(response.model_dump(mode="json"))


# --------------------------------------------------------------------------- reads


@router.get(
    "",
    response_model=EmployeeListResponse,
    summary="List employees",
    description=(
        "A stable-sorted page of employees, optionally filtered by status. Site managers see only "
        "employees, and no wage fields; administrators and accounting see everyone. The list carries "
        "no sensitive personal fields — the card is where the detail lives."
    ),
)
def list_employees(
    caller: PersonnelReaderCaller,
    session: DbSession,
    status: EmployeeStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EmployeeListResponse:
    page = employee_service.list_employees(session, status=status, limit=limit, offset=offset)
    items = [EmployeeListItem.model_validate(item) for item in page.items]
    # The list carries no wage or sensitive fields, so redaction is a no-op here; kept for symmetry
    # and so a future column added to the list item is covered by the same rule as the card.
    return EmployeeListResponse(items=items, total=page.total, limit=limit, offset=offset)


@router.get(
    "/{employee_id}",
    summary="Read one employee",
    responses={
        HTTPStatus.OK: {"model": EmployeeResponse, "description": "The employee card"},
        HTTPStatus.NOT_FOUND: {"description": "No employee with that id"},
    },
)
def read_employee(
    employee_id: uuid.UUID,
    caller: PersonnelReaderCaller,
    session: DbSession,
) -> Any:
    try:
        employee = employee_service.get_employee(session, employee_id)
    except employee_service.EmployeeError as error:
        raise _raise_for(error) from error
    return _card_dict(employee, caller, session)


# --------------------------------------------------------------------------- writes
# Create, update, status and rates are admin-only. A site manager reads employees at their sites
# (Requirement 2.4) but does not edit the personnel record; accounting reads but does not write.


@router.post(
    "",
    status_code=HTTPStatus.CREATED,
    summary="Create an employee",
    responses={
        HTTPStatus.CREATED: {"model": EmployeeResponse, "description": "The created employee"},
        HTTPStatus.CONFLICT: {"description": "The passport is already in use"},
    },
)
def create_employee(
    payload: EmployeeCreate,
    caller: AdminCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> Any:
    try:
        employee = employee_service.create_employee(session, payload, context=context)
        session.commit()
    except employee_service.EmployeeError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(employee)
    return _card_dict(employee, caller, session)


@router.patch(
    "/{employee_id}",
    summary="Update an employee",
    responses={
        HTTPStatus.OK: {"model": EmployeeResponse, "description": "The updated employee"},
        HTTPStatus.NOT_FOUND: {"description": "No employee with that id"},
        HTTPStatus.CONFLICT: {"description": "The passport is already in use"},
    },
)
def update_employee(
    employee_id: uuid.UUID,
    payload: EmployeeUpdate,
    caller: AdminCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> Any:
    try:
        employee = employee_service.update_employee(session, employee_id, payload, context=context)
        session.commit()
    except employee_service.EmployeeError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(employee)
    return _card_dict(employee, caller, session)


@router.patch(
    "/{employee_id}/status",
    summary="Change an employee's status",
    description=(
        "Moves an employee between Active, On Leave and Inactive, or to Terminated. There is no "
        "hard delete: a terminated employee's record and history remain. Leaving Terminated is a "
        "rehire decision, not a status edit, and is rejected."
    ),
    responses={
        HTTPStatus.OK: {"model": EmployeeResponse, "description": "The employee at its new status"},
        HTTPStatus.NOT_FOUND: {"description": "No employee with that id"},
        HTTPStatus.CONFLICT: {"description": "The lifecycle does not allow that transition"},
    },
)
def change_status(
    employee_id: uuid.UUID,
    payload: EmployeeStatusUpdate,
    caller: AdminCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> Any:
    try:
        employee = employee_service.change_status(
            session, employee_id, payload.status, context=context, reason=payload.reason
        )
        session.commit()
    except employee_service.EmployeeError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(employee)
    return _card_dict(employee, caller, session)


# --------------------------------------------------------------------------- rates


@router.get(
    "/{employee_id}/rates",
    summary="Read an employee's rate history",
    description=(
        "The full effective-dated rate history, plus today's rate flattened onto the card. Wage "
        "fields, so a site manager receives neither the history nor the current rate."
    ),
    responses={
        HTTPStatus.OK: {"model": EmployeeResponse, "description": "The employee with its rates"},
        HTTPStatus.NOT_FOUND: {"description": "No employee with that id"},
    },
)
def read_rates(
    employee_id: uuid.UUID,
    caller: PersonnelReaderCaller,
    session: DbSession,
) -> Any:
    try:
        employee = employee_service.get_employee(session, employee_id)
    except employee_service.EmployeeError as error:
        raise _raise_for(error) from error
    return _card_dict(employee, caller, session)


@router.put(
    "/{employee_id}/rates",
    summary="Replace an employee's rate history",
    description=(
        "Replaces the whole rate history with the submitted, non-overlapping chain. The submission "
        "is validated before anything is written, so a rejected body leaves the existing history "
        "untouched. Admin only, since rates are wage data."
    ),
    responses={
        HTTPStatus.OK: {"model": EmployeeResponse, "description": "The employee with its new rates"},
        HTTPStatus.NOT_FOUND: {"description": "No employee with that id"},
        HTTPStatus.BAD_REQUEST: {"description": "The submitted rates overlap or are malformed"},
    },
)
def replace_rates(
    employee_id: uuid.UUID,
    payload: EmployeeRatesUpdate,
    caller: AdminCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> Any:
    try:
        employee = employee_service.replace_rate_history(
            session, employee_id, payload.rates, context=context
        )
        session.commit()
    except employee_service.EmployeeError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(employee)
    return _card_dict(employee, caller, session)


# --------------------------------------------------------------------------- site assignment
# The many-to-many between employees and sites (Requirement 7.1), maintained here from the employee
# side and in the site router from the site side — both write the same `employee_sites` table. An
# assignment is an expectation only; it never restricts where an employee may record time (7.2).
# Admin only, matching the site-side endpoint, since assignment is an administrative decision.


def _site_error(error: site_service.SiteError) -> HTTPException:
    """Map a site-service error onto the matching HTTP failure for the assignment endpoints."""
    status = {
        site_service.SiteNotFound.code: HTTPStatus.NOT_FOUND,
        site_service.EmployeeNotFound.code: HTTPStatus.NOT_FOUND,
    }.get(error.code, HTTPStatus.BAD_REQUEST)
    return api_error(status, error.code)


@router.get(
    "/{employee_id}/sites",
    response_model=EmployeeSitesResponse,
    summary="Read the sites an employee is assigned to",
    description="The employee's assigned sites (Requirement 7.1). Admin only.",
    responses={HTTPStatus.NOT_FOUND: {"description": "No employee with that id"}},
)
def read_employee_sites(
    employee_id: uuid.UUID,
    caller: AdminCaller,
    session: DbSession,
) -> EmployeeSitesResponse:
    try:
        employee = employee_service.get_employee(session, employee_id)
    except employee_service.EmployeeError as error:
        raise _raise_for(error) from error
    site_ids = site_service.assigned_site_ids(session, employee.id)
    return EmployeeSitesResponse(employee_id=employee.id, site_ids=site_ids)


@router.put(
    "/{employee_id}/sites",
    response_model=EmployeeSitesResponse,
    summary="Set the sites an employee is assigned to",
    description=(
        "Replaces the set of sites the employee is expected at (Requirement 7.1). The assignment "
        "does not restrict where the employee may record time (Requirement 7.2). Admin only."
    ),
    responses={HTTPStatus.NOT_FOUND: {"description": "No employee, or a site id that does not exist"}},
)
def set_employee_sites(
    employee_id: uuid.UUID,
    payload: EmployeeSitesUpdate,
    caller: AdminCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> EmployeeSitesResponse:
    try:
        site_ids = site_service.replace_employee_sites(
            session, employee_id, payload.site_ids, context=context
        )
        session.commit()
    except site_service.SiteError as error:
        session.rollback()
        raise _site_error(error) from error
    return EmployeeSitesResponse(employee_id=employee_id, site_ids=site_ids)
