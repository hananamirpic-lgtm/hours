"""Exports router — request an Excel/PDF export and read it back (Requirement 19).

HTTP only: guard by the report the export names, validate the body, call the export service, map a
refusal onto the error envelope, and shape the response. The service builds, renders, stores, audits
and (when large) defers; it never commits, so `POST /api/exports` commits here — the export row, its
audit record and the stored-file bookkeeping ride one transaction (Requirement 13.2).

Authorization mirrors the report endpoints exactly, because an export is just a report in another
container and must not widen who can see a figure:

* the by-employee export is the one a site manager may reach (`HoursReaderCaller`); its cost is
  included only for a finance caller, the same rule the by-employee report applies (Requirement 2.5);
* the by-site, by-client, profitability and payment-request exports are finance data
  (`FinanceCaller`), administrators and accounting only (Requirement 2.5, 17.7).

The guard is chosen per requested report inside `POST /api/exports` rather than by splitting into two
endpoints, so the client has one place to ask for any export; the finance reports are refused for a
manager with the same `insufficient_role` a direct report request would raise.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import DbSession, HoursReaderCaller, api_error
from app.core import authz
from app.core.storage import ObjectStorage, get_object_storage
from app.models.export import ExportReportType
from app.schemas.export import ExportCreateRequest, ExportResponse
from app.services import export as export_service

router = APIRouter(prefix="/exports", tags=["exports"])

#: Object storage, injected so a test can override it with a fake and no request touches real S3.
Storage = Annotated[ObjectStorage, Depends(get_object_storage)]

#: The finance-only reports: a site manager may not export any of these, just as they may not read
#: them (Requirement 2.5, 17.7). The by-employee report is the sole export a manager may reach.
_FINANCE_ONLY = frozenset(
    {
        ExportReportType.BY_SITE,
        ExportReportType.BY_CLIENT,
        ExportReportType.PROFITABILITY,
        ExportReportType.PAYMENT_REQUEST,
    }
)

_ERROR_STATUS: dict[str, HTTPStatus] = {
    export_service.UnsupportedExportCombination.code: HTTPStatus.BAD_REQUEST,
    export_service.ExportNotFound.code: HTTPStatus.NOT_FOUND,
    # The host cannot paint a PDF (no native stack). Not a client error — a 503, so a client knows to
    # retry against a host that can, rather than "fix your request". Never fires in the deployed
    # container.
    export_service.PdfRenderNotAvailable.code: HTTPStatus.SERVICE_UNAVAILABLE,
}


def _raise_for(error: export_service.ExportError) -> HTTPException:
    status = _ERROR_STATUS.get(getattr(error, "code", ""), HTTPStatus.BAD_REQUEST)
    return api_error(status, getattr(error, "code", "export_error"))


def _response(export, storage: ObjectStorage, *, include_download: bool) -> ExportResponse:
    payload = ExportResponse.model_validate(export)
    if include_download:
        payload.download_url = export_service.download_url(export, storage)
    return payload


@router.post(
    "",
    response_model=ExportResponse,
    status_code=HTTPStatus.CREATED,
    summary="Request an Excel or PDF export of a report or payment request",
    description=(
        "Generate an export of a report or a client payment request in Excel or PDF for a period "
        "(Requirement 19.1, 19.2). The file is stamped with the period, filters, generation time and "
        "requesting user (Requirement 19.4) and the request is recorded in the audit log (Requirement "
        "19.5). Hebrew is rendered right-to-left (Requirement 19.3). A small export is rendered inline "
        "and returned `ready` with a download URL; a large one is returned `pending` and rendered in "
        "the background, with a notification when the file is ready (Requirement 19.6). Authorization "
        "matches the report: the by-employee export is open to site managers (without cost), the "
        "billing exports and the payment request are administrators and accounting only "
        "(Requirement 2.5, 17.7)."
    ),
    responses={
        HTTPStatus.CREATED: {"model": ExportResponse, "description": "The created export"},
        HTTPStatus.BAD_REQUEST: {"description": "An unsupported report/format combination"},
        HTTPStatus.FORBIDDEN: {"description": "The caller may not export this report"},
    },
)
def create_export(
    payload: ExportCreateRequest,
    caller: HoursReaderCaller,
    session: DbSession,
    storage: Storage,
) -> ExportResponse:
    # The guard is the report's guard: a finance report exported by a site manager is refused with the
    # same insufficient_role a direct read would raise, and audited (Requirement 2.5, 2.8).
    finance_roles = authz.FINANCE_ROLES | authz.ROLES_WITH_FULL_ACCESS
    if payload.report_type in _FINANCE_ONLY and caller.role not in finance_roles:
        raise caller.deny(code="insufficient_role", reason="role_not_permitted")

    request = export_service.ExportRequest(
        report_type=payload.report_type,
        format=payload.format,
        year=payload.year,
        month=payload.month,
        language=payload.language,
        employee_id=payload.employee_id,
        site_id=payload.site_id,
        client_id=payload.client_id,
        project=payload.project,
    )

    try:
        export = export_service.create_export(
            session,
            request=request,
            user=caller.user,
            scope=caller.scope,
            may_read_money=caller.may_read_money_fields,
            context=caller.context,
            storage=storage,
        )
        session.commit()
    except export_service.ExportError as error:
        session.rollback()
        raise _raise_for(error) from error

    session.refresh(export)
    return _response(export, storage, include_download=True)


@router.get(
    "/{export_id}",
    response_model=ExportResponse,
    summary="Read an export and its download URL when ready",
    description=(
        "Read one of the caller's own exports (Requirement 19.6). A `ready` export carries a "
        "short-lived signed download URL; a `pending` one is still rendering; a `failed` one carries "
        "the error code. An export requested by another user reads as not found, so one user cannot "
        "reach another's export."
    ),
    responses={
        HTTPStatus.OK: {"model": ExportResponse, "description": "The export"},
        HTTPStatus.NOT_FOUND: {"description": "No such export for this caller"},
    },
)
def read_export(
    export_id: uuid.UUID,
    caller: HoursReaderCaller,
    session: DbSession,
    storage: Storage,
) -> ExportResponse:
    try:
        export = export_service.get_export(
            session, export_id=export_id, requester_user_id=caller.user.id
        )
    except export_service.ExportNotFound as error:
        raise api_error(HTTPStatus.NOT_FOUND, error.code) from error
    return _response(export, storage, include_download=True)
