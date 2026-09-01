"""Site router (Requirement 6, 7, with 2 and 17.7 for the guards).

HTTP only: guard, validate, call the service, map a refusal onto a status code and the error
envelope, redact the response for the caller. The service owns every rule and never commits, so this
module commits after a successful write — the audit rows the service added ride along with it, which
is what keeps a change and its audit record in one transaction (Requirement 13.2). This mirrors
`app.api.routers.employees` and `app.api.routers.clients`.

Two authorization shapes meet here.

**Reads are scoped, not closed.** A site manager may read the sites they run, so the list is narrowed
to their `user_sites` scope in the `WHERE` clause and a single-site read checks the site is in scope.
Administrators and accounting see every site.

**Billing rates are redacted, not withheld.** Requirement 2.5 and 17.7 keep the billing rate from a
site manager, but the rest of the site card is theirs to see. So every response is passed through
`caller.redact`, which strips the `site_rates` history and the flattened billing fields for a manager
and returns the payload unchanged for a finance role. As in the employee router, the wage-bearing
handlers return a plain dict rather than a response model, because declaring `response_model` would
re-validate the redacted payload and put the stripped keys back as `null` — see that module's note.

Writes — create, update, rates and assignment — are administrator-only. There are no location fields
anywhere in the request models, and `extra="forbid"` on each means a request carrying a `latitude` or
`longitude` is rejected as an unknown field (Requirement 6.8).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.api.deps import (
    AdminCaller,
    AuthenticatedContext,
    Caller,
    DbSession,
    HoursReaderCaller,
    api_error,
)
from app.core.qr_token import QrAction
from app.models.site import QrMode, Site, SiteStatus
from app.schemas.site import (
    SiteCreate,
    SiteEmployeesUpdate,
    SiteListItem,
    SiteListResponse,
    SiteRateResponse,
    SiteRatesUpdate,
    SiteResponse,
    SiteUpdate,
)
from app.services import qr as qr_service
from app.services import site as site_service

router = APIRouter(prefix="/sites", tags=["sites"])


# --------------------------------------------------------------------------- error mapping

#: Which HTTP status each service error maps onto. A duplicate site number is a conflict — the request
#: is well-formed but collides with existing state. An overlapping or malformed rate is a bad request
#: — the body itself is wrong and a different body would work. An unknown employee named in an
#: assignment is a bad request for the same reason.
_ERROR_STATUS: dict[str, HTTPStatus] = {
    site_service.SiteNotFound.code: HTTPStatus.NOT_FOUND,
    site_service.EmployeeNotFound.code: HTTPStatus.NOT_FOUND,
    site_service.DuplicateSiteNumber.code: HTTPStatus.CONFLICT,
    site_service.OverlappingRates.code: HTTPStatus.BAD_REQUEST,
    site_service.InvalidRatePeriod.code: HTTPStatus.BAD_REQUEST,
}


def _raise_for(error: site_service.SiteError) -> HTTPException:
    """Turn a service error into the matching HTTP failure, with params where they help the client."""
    status = _ERROR_STATUS.get(error.code, HTTPStatus.BAD_REQUEST)
    if isinstance(error, site_service.DuplicateSiteNumber):
        return HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail={
                "error": {
                    "code": error.code,
                    "params": {
                        "site_id": str(error.conflicting.id),
                        "site_number": error.conflicting.site_number,
                    },
                }
            },
        )
    return api_error(status, error.code)


# --------------------------------------------------------------------------- serialisation


def _card_dict(site: Site, caller: Caller, session: DbSession) -> dict[str, Any]:
    """The site card as a JSON-able dict, with today's billing rate flattened on and billing redacted.

    The current rate's figures are copied onto the top-level billing fields for convenience; both they
    and the full `site_rates` list are named to match `BILLING_FIELDS`, so `caller.redact` removes
    them for a site manager (Requirement 2.5, 17.7). When no rate is in force today the billing fields
    are absent, which a finance reader sees as "no rate set". The assigned employees (Requirement 7.1)
    are attached to every reader, because the assignment is not a billing figure. Returned as a dict so
    the redaction is not undone by a response model re-adding the stripped keys — see the module note.
    """
    response = SiteResponse.model_validate(site)
    # The billing history is served under `site_rates` (to match `BILLING_FIELDS`) while the model
    # attribute is `rates`, so it is copied across explicitly rather than by `from_attributes`.
    response.site_rates = [SiteRateResponse.model_validate(rate) for rate in site.rates]
    response.employee_ids = site_service.assigned_employee_ids(session, site.id)
    today = datetime.now(UTC).date()
    current = site_service.resolve_rate(site, today)
    if current is not None:
        response.billing_rate = current.billing_rate
        response.overtime_billing_rate = current.overtime_billing_rate
    return caller.redact(response.model_dump(mode="json"))


# --------------------------------------------------------------------------- reads


@router.get(
    "",
    response_model=SiteListResponse,
    summary="List sites",
    description=(
        "A stable-sorted page of sites, optionally filtered by status. Site managers see only their "
        "assigned sites; administrators and accounting see every site. The list carries no billing "
        "fields — the card is where the rate lives, and even there it is redacted for a manager."
    ),
)
def list_sites(
    caller: HoursReaderCaller,
    session: DbSession,
    status: SiteStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SiteListResponse:
    scoped = caller.scope_query(site_service.base_select(), Site.id)
    page = site_service.list_sites(
        session, status=status, scope_statement=scoped, limit=limit, offset=offset
    )
    items = [SiteListItem.model_validate(item) for item in page.items]
    return SiteListResponse(items=items, total=page.total, limit=limit, offset=offset)


@router.get(
    "/{site_id}",
    summary="Read one site",
    responses={
        HTTPStatus.OK: {"model": SiteResponse, "description": "The site card"},
        HTTPStatus.NOT_FOUND: {"description": "No site with that id"},
        HTTPStatus.FORBIDDEN: {"description": "The site is outside the caller's scope"},
    },
)
def read_site(
    site_id: uuid.UUID,
    caller: HoursReaderCaller,
    session: DbSession,
) -> Any:
    caller.require_site(site_id)
    try:
        site = site_service.get_site(session, site_id)
    except site_service.SiteError as error:
        raise _raise_for(error) from error
    return _card_dict(site, caller, session)


# --------------------------------------------------------------------------- writes
# Create, update, rates and assignment are administrator-only.


@router.post(
    "",
    status_code=HTTPStatus.CREATED,
    summary="Create a site",
    responses={
        HTTPStatus.CREATED: {"model": SiteResponse, "description": "The created site"},
        HTTPStatus.CONFLICT: {"description": "The site number is already in use"},
    },
)
def create_site(
    payload: SiteCreate,
    caller: AdminCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> Any:
    try:
        site = site_service.create_site(session, payload, context=context)
        session.commit()
    except site_service.SiteError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(site)
    return _card_dict(site, caller, session)


# --------------------------------------------------------------------------- QR download and rotation


def _select_artifact(
    artifacts: list[qr_service.QrArtifact], action: QrAction | None
) -> qr_service.QrArtifact | None:
    """Pick the artifact matching the requested action, or the sole one for a unified site.

    A unified site has one artifact with `action=None` and ignores the query. A separate site has
    two; the caller names which with `?action=check_in|check_out`, and omitting it on a separate site
    is a bad request rather than a silent guess.
    """
    if len(artifacts) == 1 and artifacts[0].action is None:
        return artifacts[0]
    return next((art for art in artifacts if art.action == action), None)


@router.get(
    "/{site_id}/qr",
    summary="Download a site's printable QR",
    description=(
        "Returns the site's QR as a printable file (Requirement 8.5). `format` chooses PNG (default) "
        "or PDF; both carry the site name and number as readable text. A unified site has one code; "
        "a separate site has two, selected with `action=check_in|check_out`. The token encoded is the "
        "site's current version, so a file downloaded after a regeneration replaces the printed one."
    ),
    responses={
        HTTPStatus.OK: {"content": {"image/png": {}, "application/pdf": {}}},
        HTTPStatus.NOT_FOUND: {"description": "No site with that id"},
        HTTPStatus.FORBIDDEN: {"description": "The site is outside the caller's scope"},
        HTTPStatus.BAD_REQUEST: {"description": "A separate-mode site needs an action"},
    },
)
def download_qr(
    site_id: uuid.UUID,
    caller: HoursReaderCaller,
    session: DbSession,
    format: Literal["png", "pdf"] = "png",
    action: QrAction | None = None,
) -> Response:
    caller.require_site(site_id)
    try:
        site = site_service.get_site(session, site_id)
    except site_service.SiteError as error:
        raise _raise_for(error) from error

    rendered = qr_service.render_site_qr(site)
    if site.qr_mode is QrMode.SEPARATE and action is None:
        raise api_error(HTTPStatus.BAD_REQUEST, "qr_action_required")

    artifacts = rendered.pdf if format == "pdf" else rendered.png
    artifact = _select_artifact(artifacts, action)
    if artifact is None:
        raise api_error(HTTPStatus.BAD_REQUEST, "qr_action_required")

    suffix = f"-{artifact.action.value}" if artifact.action is not None else ""
    filename = f"site-{site.site_number}{suffix}.{format}"
    return Response(
        content=artifact.content,
        media_type=artifact.media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/{site_id}/qr/regenerate",
    summary="Regenerate a site's QR token",
    description=(
        "Bumps the site's QR version and mints a fresh token, after which every previously printed "
        "code is rejected on scan (Requirement 8.6, 8.7). Admin only, since it invalidates codes in "
        "the field. Returns the site card."
    ),
    responses={
        HTTPStatus.OK: {"model": SiteResponse, "description": "The site with its rotated QR"},
        HTTPStatus.NOT_FOUND: {"description": "No site with that id"},
    },
)
def regenerate_qr(
    site_id: uuid.UUID,
    caller: AdminCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> Any:
    try:
        site = site_service.regenerate_qr(session, site_id, context=context)
        session.commit()
    except site_service.SiteError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(site)
    return _card_dict(site, caller, session)


@router.patch(
    "/{site_id}",
    summary="Update a site",
    responses={
        HTTPStatus.OK: {"model": SiteResponse, "description": "The updated site"},
        HTTPStatus.NOT_FOUND: {"description": "No site with that id"},
        HTTPStatus.CONFLICT: {"description": "The site number is already in use"},
    },
)
def update_site(
    site_id: uuid.UUID,
    payload: SiteUpdate,
    caller: AdminCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> Any:
    try:
        site = site_service.update_site(session, site_id, payload, context=context)
        session.commit()
    except site_service.SiteError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(site)
    return _card_dict(site, caller, session)


# --------------------------------------------------------------------------- rates


@router.get(
    "/{site_id}/rates",
    summary="Read a site's billing-rate history",
    description=(
        "The full effective-dated billing history, plus today's rate flattened onto the card. "
        "Billing fields, so a site manager receives neither the history nor the current rate "
        "(Requirement 2.5, 17.7)."
    ),
    responses={
        HTTPStatus.OK: {"model": SiteResponse, "description": "The site with its rates"},
        HTTPStatus.NOT_FOUND: {"description": "No site with that id"},
        HTTPStatus.FORBIDDEN: {"description": "The site is outside the caller's scope"},
    },
)
def read_rates(
    site_id: uuid.UUID,
    caller: HoursReaderCaller,
    session: DbSession,
) -> Any:
    caller.require_site(site_id)
    try:
        site = site_service.get_site(session, site_id)
    except site_service.SiteError as error:
        raise _raise_for(error) from error
    return _card_dict(site, caller, session)


@router.put(
    "/{site_id}/rates",
    summary="Replace a site's billing-rate history",
    description=(
        "Replaces the whole billing history with the submitted, non-overlapping chain. The "
        "submission is validated before anything is written, so a rejected body leaves the existing "
        "history untouched — which is what keeps an already-billed period unchanged (Requirement "
        "6.6). Admin only, since billing rates are finance data."
    ),
    responses={
        HTTPStatus.OK: {"model": SiteResponse, "description": "The site with its new rates"},
        HTTPStatus.NOT_FOUND: {"description": "No site with that id"},
        HTTPStatus.BAD_REQUEST: {"description": "The submitted rates overlap or are malformed"},
    },
)
def replace_rates(
    site_id: uuid.UUID,
    payload: SiteRatesUpdate,
    caller: AdminCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> Any:
    try:
        site = site_service.replace_rate_history(session, site_id, payload.rates, context=context)
        session.commit()
    except site_service.SiteError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(site)
    return _card_dict(site, caller, session)


# --------------------------------------------------------------------------- assignment


@router.put(
    "/{site_id}/employees",
    summary="Set the employees assigned to a site",
    description=(
        "Replaces the set of employees expected at this site (Requirement 7.1). The assignment is an "
        "expectation only and never restricts where an employee may record time (Requirement 7.2). "
        "Admin only."
    ),
    responses={
        HTTPStatus.OK: {"model": SiteResponse, "description": "The site with its assignment"},
        HTTPStatus.NOT_FOUND: {"description": "No site, or an employee id that does not exist"},
    },
)
def set_site_employees(
    site_id: uuid.UUID,
    payload: SiteEmployeesUpdate,
    caller: AdminCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> Any:
    try:
        site_service.replace_site_employees(
            session, site_id, payload.employee_ids, context=context
        )
        site = site_service.get_site(session, site_id)
        session.commit()
    except site_service.SiteError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(site)
    return _card_dict(site, caller, session)
