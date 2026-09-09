"""Client router (Requirement 5).

HTTP only: guard, validate, call the service, map a refusal onto a status code and the error
envelope. The service owns every rule and never commits, so this module commits after a successful
write — the audit rows the service added ride along with it, which is what keeps a change and its
audit record in one transaction (Requirement 13.2). This mirrors `app.api.routers.employees`.

Authorization: every client endpoint is open to the operational administrators — the administrator
and the operations administrator — since managing clients is operational work. A client is a billing
entity, but its record carries no wage, payroll or billing *figure* (payment terms are net-days plus
a note, not an amount; billing rates live on the site rate history), so there is nothing to redact
and no reason to withhold the record from the operations administrator. A site manager still has no
client endpoint (Requirement 2.5), and accounting reaches clients through the finance reports.

The delete endpoint applies Requirement 5.4: a client whose sites carry time entries cannot be
deleted, and the 409 offers archival as the recorded alternative. Archival itself is `POST
/{id}/archive`, a distinct action rather than a field edit, so the rule is reached the same way
whether the caller tried to delete first or archived directly.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response

from app.api.deps import (
    AuthenticatedContext,
    ClientReaderCaller,
    DbSession,
    OperationsCaller,
    api_error,
)
from app.schemas.client import (
    ClientCreate,
    ClientListItem,
    ClientListResponse,
    ClientResponse,
    ClientSiteItem,
    ClientSitesResponse,
    ClientUpdate,
)
from app.services import client as client_service

router = APIRouter(prefix="/clients", tags=["clients"])


# --------------------------------------------------------------------------- error mapping

#: Which HTTP status each service error maps onto. A duplicate company number is a conflict — the
#: request is well-formed but collides with existing state. A client that still has time entries is a
#: conflict too: the resource is not in a state that accepts a delete, and the response says what to
#: do instead.
_ERROR_STATUS: dict[str, HTTPStatus] = {
    client_service.ClientNotFound.code: HTTPStatus.NOT_FOUND,
    client_service.DuplicateCompanyNumber.code: HTTPStatus.CONFLICT,
    client_service.ClientHasTimeEntries.code: HTTPStatus.CONFLICT,
}


def _raise_for(error: client_service.ClientError) -> HTTPException:
    """Turn a service error into the matching HTTP failure, with params and actions where they help."""
    status = _ERROR_STATUS.get(error.code, HTTPStatus.BAD_REQUEST)
    if isinstance(error, client_service.DuplicateCompanyNumber):
        return HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail={
                "error": {
                    "code": error.code,
                    "params": {
                        "client_id": str(error.conflicting.id),
                        "name": error.conflicting.name,
                    },
                }
            },
        )
    if isinstance(error, client_service.ClientHasTimeEntries):
        # The recorded alternative to deletion is archival, surfaced as an action the front end can
        # offer directly (Requirement 5.4).
        return HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail={"error": {"code": error.code, "actions": ["archive"]}},
        )
    return api_error(status, error.code)


# --------------------------------------------------------------------------- reads


@router.get(
    "",
    response_model=ClientListResponse,
    summary="List clients",
    description=(
        "A stable-sorted page of clients. Archived clients are excluded unless "
        "`include_archived=true`. Administrators and accounting only."
    ),
)
def list_clients(
    caller: ClientReaderCaller,  # noqa: ARG001 - the type is the guard; a client card carries no money
    session: DbSession,
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ClientListResponse:
    page = client_service.list_clients(
        session, include_archived=include_archived, limit=limit, offset=offset
    )
    items = [ClientListItem.model_validate(item) for item in page.items]
    return ClientListResponse(items=items, total=page.total, limit=limit, offset=offset)


@router.get(
    "/{client_id}",
    response_model=ClientResponse,
    summary="Read one client",
    responses={HTTPStatus.NOT_FOUND: {"description": "No client with that id"}},
)
def read_client(
    client_id: uuid.UUID,
    caller: ClientReaderCaller,  # noqa: ARG001 - the type is the guard; a client card carries no money
    session: DbSession,
) -> ClientResponse:
    try:
        client = client_service.get_client(session, client_id)
    except client_service.ClientError as error:
        raise _raise_for(error) from error
    return ClientResponse.model_validate(client)


@router.get(
    "/{client_id}/sites",
    response_model=ClientSitesResponse,
    summary="List a client's sites",
    description="The sites a client owns (Requirement 5.3). Administrators and accounting only.",
    responses={HTTPStatus.NOT_FOUND: {"description": "No client with that id"}},
)
def read_client_sites(
    client_id: uuid.UUID,
    caller: ClientReaderCaller,  # noqa: ARG001 - the type is the guard; a client card carries no money
    session: DbSession,
) -> ClientSitesResponse:
    try:
        sites = client_service.list_sites(session, client_id)
    except client_service.ClientError as error:
        raise _raise_for(error) from error
    items = [ClientSiteItem.model_validate(site) for site in sites]
    return ClientSitesResponse(client_id=client_id, items=items, total=len(items))


# --------------------------------------------------------------------------- writes
# Create, update, delete and archive are open to the operational administrators.


@router.post(
    "",
    status_code=HTTPStatus.CREATED,
    response_model=ClientResponse,
    summary="Create a client",
    responses={HTTPStatus.CONFLICT: {"description": "The company number is already in use"}},
)
def create_client(
    payload: ClientCreate,
    caller: OperationsCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> ClientResponse:
    try:
        client = client_service.create_client(session, payload, context=context)
        session.commit()
    except client_service.ClientError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(client)
    return ClientResponse.model_validate(client)


@router.patch(
    "/{client_id}",
    response_model=ClientResponse,
    summary="Update a client",
    responses={
        HTTPStatus.NOT_FOUND: {"description": "No client with that id"},
        HTTPStatus.CONFLICT: {"description": "The company number is already in use"},
    },
)
def update_client(
    client_id: uuid.UUID,
    payload: ClientUpdate,
    caller: OperationsCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> ClientResponse:
    try:
        client = client_service.update_client(session, client_id, payload, context=context)
        session.commit()
    except client_service.ClientError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(client)
    return ClientResponse.model_validate(client)


@router.delete(
    "/{client_id}",
    status_code=HTTPStatus.NO_CONTENT,
    summary="Delete a client",
    description=(
        "Deletes a client only when none of its sites carry a time entry. When they do, the request "
        "is refused with a conflict that offers archival instead (Requirement 5.4)."
    ),
    responses={
        HTTPStatus.NO_CONTENT: {"description": "The client was deleted"},
        HTTPStatus.NOT_FOUND: {"description": "No client with that id"},
        HTTPStatus.CONFLICT: {"description": "The client has time entries; archive it instead"},
    },
)
def delete_client(
    client_id: uuid.UUID,
    caller: OperationsCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> Response:
    try:
        client_service.delete_client(session, client_id, context=context)
        session.commit()
    except client_service.ClientError as error:
        session.rollback()
        raise _raise_for(error) from error
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.post(
    "/{client_id}/archive",
    response_model=ClientResponse,
    summary="Archive a client",
    description=(
        "Archives a client instead of deleting it, keeping its sites and time entries. This is the "
        "recorded alternative to a delete that was refused (Requirement 5.4). Idempotent."
    ),
    responses={HTTPStatus.NOT_FOUND: {"description": "No client with that id"}},
)
def archive_client(
    client_id: uuid.UUID,
    caller: OperationsCaller,
    session: DbSession,
    context: AuthenticatedContext,
) -> ClientResponse:
    try:
        client = client_service.archive_client(session, client_id, context=context)
        session.commit()
    except client_service.ClientError as error:
        session.rollback()
        raise _raise_for(error) from error
    session.refresh(client)
    return ClientResponse.model_validate(client)
