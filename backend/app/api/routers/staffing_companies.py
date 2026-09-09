"""Staffing-company router (Requirement 1, 3).

HTTP only: guard, validate, call the service, map a refusal onto a status code and the error envelope.
The service owns every rule and never commits, so this module commits after a successful write — the
audit rows the service added ride along, keeping a change and its audit record in one transaction.

Every endpoint, read and write, is admin-only: the Staffing Company tab is an administrative surface
(Requirement 1.1, 1.2). The one refusal with structure is the delete guard: when active employees are
still linked, the 409 carries them in `params` so the front end can name exactly who must be
deactivated first (Requirement 3.2, 3.3).
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import AuthenticatedContext, DbSession, OperationsCaller, api_error
from app.schemas.staffing_company import (
    StaffingCompanyCreate,
    StaffingCompanyListItem,
    StaffingCompanyListResponse,
    StaffingCompanyResponse,
    StaffingCompanyUpdate,
)
from app.services import staffing_company as staffing_company_service

router = APIRouter(prefix="/staffing-companies", tags=["staffing-companies"])


# --------------------------------------------------------------------------- error mapping

#: Which HTTP status each service error maps onto. A not-found is a 404; a delete blocked by active
#: employees is a 409 — the request is well-formed but collides with existing state.
_ERROR_STATUS: dict[str, HTTPStatus] = {
    staffing_company_service.StaffingCompanyNotFound.code: HTTPStatus.NOT_FOUND,
    staffing_company_service.StaffingCompanyHasActiveEmployees.code: HTTPStatus.CONFLICT,
}


def _raise_for(error: staffing_company_service.StaffingCompanyError) -> HTTPException:
    """Turn a service error into the matching HTTP failure, naming the blockers where it helps.

    The delete guard's 409 carries the active linked employees so the front end can render "still
    linked to: <names>" from the ids/names rather than from server prose (Requirement 3.2, 3.3).
    """
    if isinstance(error, staffing_company_service.StaffingCompanyHasActiveEmployees):
        return HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail={
                "error": {
                    "code": error.code,
                    "params": {
                        "employees": [
                            {"id": str(e.id), "full_name": e.full_name} for e in error.employees
                        ],
                    },
                }
            },
        )
    status = _ERROR_STATUS.get(error.code, HTTPStatus.BAD_REQUEST)
    return api_error(status, error.code)


# --------------------------------------------------------------------------- reads


@router.get(
    "",
    summary="List staffing companies",
    description="A stable-sorted page of staffing companies. Admin or operations admin.",
)
def list_staffing_companies(
    caller: OperationsCaller,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    page = staffing_company_service.list_staffing_companies(session, limit=limit, offset=offset)
    items = [StaffingCompanyListItem.model_validate(item) for item in page.items]
    response = StaffingCompanyListResponse(
        items=items, total=page.total, limit=limit, offset=offset
    )
    # Redact money (the flat hourly_rate) for a non-finance caller such as the operations admin. The
    # response is returned as a redacted dict rather than the model, so a response_model cannot re-add
    # the stripped key as null — the same pattern the sites router uses (Requirement 3 of the role).
    return caller.redact(response.model_dump(mode="json"))


@router.get(
    "/{company_id}",
    summary="Read one staffing company",
    responses={HTTPStatus.NOT_FOUND: {"description": "No staffing company with that id"}},
)
def read_staffing_company(
    company_id: uuid.UUID,
    caller: OperationsCaller,
    session: DbSession,
) -> Any:
    try:
        company = staffing_company_service.get_staffing_company(session, company_id)
    except staffing_company_service.StaffingCompanyError as error:
        raise _raise_for(error) from error
    return caller.redact(StaffingCompanyResponse.model_validate(company).model_dump(mode="json"))


# --------------------------------------------------------------------------- writes


@router.post(
    "",
    status_code=HTTPStatus.CREATED,
    summary="Create a staffing company",
)
def create_staffing_company(
    payload: StaffingCompanyCreate,
    caller: OperationsCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> Any:
    try:
        company = staffing_company_service.create_staffing_company(session, payload, context=context)
        session.commit()
    except staffing_company_service.StaffingCompanyError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(company)
    return caller.redact(StaffingCompanyResponse.model_validate(company).model_dump(mode="json"))


@router.patch(
    "/{company_id}",
    summary="Update a staffing company",
    responses={HTTPStatus.NOT_FOUND: {"description": "No staffing company with that id"}},
)
def update_staffing_company(
    company_id: uuid.UUID,
    payload: StaffingCompanyUpdate,
    caller: OperationsCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> Any:
    try:
        company = staffing_company_service.update_staffing_company(
            session, company_id, payload, context=context
        )
        session.commit()
    except staffing_company_service.StaffingCompanyError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(company)
    return caller.redact(StaffingCompanyResponse.model_validate(company).model_dump(mode="json"))


@router.delete(
    "/{company_id}",
    status_code=HTTPStatus.NO_CONTENT,
    response_model=None,
    summary="Delete a staffing company",
    description=(
        "Deletes a staffing company. Refused with 409 while any active (non-terminated) employee is "
        "linked to it; the error names those employees so they can be deactivated first. Admin only."
    ),
    responses={
        HTTPStatus.NOT_FOUND: {"description": "No staffing company with that id"},
        HTTPStatus.CONFLICT: {"description": "Active employees are still linked to this company"},
    },
)
def delete_staffing_company(
    company_id: uuid.UUID,
    caller: OperationsCaller,  # noqa: ARG001 - the type is the admin-only guard
    session: DbSession,
    context: AuthenticatedContext,
) -> None:
    try:
        staffing_company_service.delete_staffing_company(session, company_id, context=context)
        session.commit()
    except staffing_company_service.StaffingCompanyError as error:
        session.rollback()
        raise _raise_for(error) from error