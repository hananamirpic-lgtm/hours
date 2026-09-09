"""Document router (Requirement 4).

HTTP only: guard, validate, call the service, map a refusal onto a status code and the error
envelope. The service owns every rule and never commits, so this module commits after a successful
write — the audit rows the service added ride along with it (Requirement 13.2).

The upload is two calls, because the file does not pass through the API (Requirements 4.3, 20.3):

1. `POST /employees/{id}/documents/upload-url` returns a short-lived, type- and size-constrained
   presigned URL. The client PUTs the file straight to private storage.
2. `POST /employees/{id}/documents` reports the upload finished; the server verifies the stored
   bytes server-side (Requirement 4.2) and records the row, or refuses with the reason.

Authorization is checked on every request (Requirement 4.3). Managing documents — issuing an upload
URL, recording a document, listing, deleting — is administrator-only, because a document is a
passport or a work permit and those are the most sensitive records the system holds. Download is
open to an administrator or to the employee reading their own document, and refused to everyone else;
minting the signed URL is the grant, so the check runs before the URL is ever created.

Storage is a dependency so a test can substitute a fake with the same surface and no request touches
real object storage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response

from app.api.deps import (
    AuthenticatedContext,
    CurrentCaller,
    DbSession,
    OperationsCaller,
    api_error,
)
from app.core import authz
from app.core.storage import (
    DOWNLOAD_URL_TTL_SECONDS,
    ObjectStorage,
    StorageError,
    get_object_storage,
)
from app.schemas.document import (
    DocumentDownloadResponse,
    DocumentListResponse,
    DocumentResponse,
    DocumentUploadComplete,
    DocumentUploadInit,
    DocumentUploadTicket,
)
from app.services import document as document_service
from app.services import employee as employee_service

router = APIRouter(tags=["documents"])

#: Object storage, injected so a test can override it with a fake and no request touches real S3.
Storage = Annotated[ObjectStorage, Depends(get_object_storage)]


# --------------------------------------------------------------------------- error mapping

#: Which HTTP status each domain error maps onto. A wrong type or an oversize file is a bad request —
#: a different body would succeed. A missing upload or document is a not-found. A type mismatch found
#: server-side is a bad request: the client uploaded something other than what it declared.
_ERROR_STATUS: dict[str, HTTPStatus] = {
    document_service.DocumentNotFound.code: HTTPStatus.NOT_FOUND,
    "unsupported_file_type": HTTPStatus.BAD_REQUEST,
    "file_too_large": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
    "upload_not_found": HTTPStatus.NOT_FOUND,
    "upload_type_mismatch": HTTPStatus.BAD_REQUEST,
    "storage_error": HTTPStatus.BAD_REQUEST,
}


def _raise_for(error: Exception, code: str) -> HTTPException:
    return api_error(_ERROR_STATUS.get(code, HTTPStatus.BAD_REQUEST), code)


def _document_response(document, *, on_date: date) -> DocumentResponse:  # noqa: ANN001
    response = DocumentResponse.model_validate(document)
    response.is_expired = document.is_expired_on(on_date)
    return response


def _today() -> date:
    return datetime.now(UTC).date()


# --------------------------------------------------------------------------- reads


@router.get(
    "/employees/{employee_id}/documents",
    response_model=DocumentListResponse,
    summary="List an employee's documents",
    description=(
        "Every non-deleted document held against the employee, plus a flag for whether any has "
        "expired (Requirement 4.5). Administrator only: documents are the most sensitive records "
        "the system holds."
    ),
)
def list_documents(
    employee_id: uuid.UUID,
    caller: OperationsCaller,
    session: DbSession,
) -> DocumentListResponse:
    # Confirm the employee exists so a bad id is a 404 rather than an empty list.
    try:
        employee_service.get_employee(session, employee_id)
    except employee_service.EmployeeError as error:
        raise _raise_for(error, error.code) from error

    today = _today()
    documents = document_service.list_documents(session, employee_id)
    items = [_document_response(document, on_date=today) for document in documents]
    return DocumentListResponse(
        items=items,
        total=len(items),
        has_expired_document=any(item.is_expired for item in items),
    )


# --------------------------------------------------------------------------- upload


@router.post(
    "/employees/{employee_id}/documents/upload-url",
    response_model=DocumentUploadTicket,
    summary="Get a presigned upload URL for a new document",
    description=(
        "Returns a short-lived, type- and size-constrained URL the client PUTs the file to. The "
        "type is checked against PDF/JPEG/PNG before the URL is issued; the real type and size are "
        "verified server-side when the upload is reported complete (Requirement 4.2)."
    ),
    responses={HTTPStatus.BAD_REQUEST: {"description": "Unsupported file type or size"}},
)
def create_upload_url(
    employee_id: uuid.UUID,
    payload: DocumentUploadInit,
    caller: OperationsCaller,
    session: DbSession,
    storage: Storage,
) -> DocumentUploadTicket:
    try:
        employee_service.get_employee(session, employee_id)
        presigned = document_service.begin_upload(session, employee_id, payload, storage=storage)
    except employee_service.EmployeeError as error:
        raise _raise_for(error, error.code) from error
    except StorageError as error:
        raise _raise_for(error, error.code) from error
    return DocumentUploadTicket(
        file_key=presigned.file_key,
        upload_url=presigned.url,
        required_headers=presigned.required_headers,
        expires_in_seconds=presigned.expires_in_seconds,
        max_bytes=presigned.max_bytes,
    )


@router.post(
    "/employees/{employee_id}/documents",
    status_code=HTTPStatus.CREATED,
    response_model=DocumentResponse,
    summary="Record a document after its upload is complete",
    description=(
        "Verifies the uploaded object server-side — that it exists, is within the 10 MB cap, and "
        "really is its declared type — before recording the row. A verification failure records "
        "nothing (Requirement 4.2)."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: {"description": "The uploaded bytes are the wrong type"},
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE: {"description": "The uploaded file exceeds 10 MB"},
        HTTPStatus.NOT_FOUND: {"description": "No employee, or no uploaded object at the key"},
    },
)
def complete_upload(
    employee_id: uuid.UUID,
    payload: DocumentUploadComplete,
    caller: OperationsCaller,
    session: DbSession,
    context: AuthenticatedContext,
    storage: Storage,
) -> DocumentResponse:
    try:
        employee_service.get_employee(session, employee_id)
        document = document_service.complete_upload(
            session,
            employee_id,
            payload,
            storage=storage,
            context=context,
            uploaded_by_user_id=caller.user.id,
        )
        session.commit()
    except (employee_service.EmployeeError, StorageError) as error:
        session.rollback()
        code = getattr(error, "code", "storage_error")
        raise _raise_for(error, code) from error
    session.refresh(document)
    return _document_response(document, on_date=_today())


# --------------------------------------------------------------------------- download


@router.get(
    "/documents/{document_id}/download-url",
    response_model=DocumentDownloadResponse,
    summary="Get a short-lived download URL for a document",
    description=(
        "Returns a signed URL valid for a few minutes (Requirement 4.3). Available to an "
        "administrator or to the employee whose document it is; refused to everyone else. The "
        "authorization check runs before the URL is minted, because minting it is the grant."
    ),
    responses={
        HTTPStatus.FORBIDDEN: {"description": "The caller may not read this document"},
        HTTPStatus.NOT_FOUND: {"description": "No document with that id"},
    },
)
def create_download_url(
    document_id: uuid.UUID,
    caller: CurrentCaller,
    session: DbSession,
    storage: Storage,
) -> DocumentDownloadResponse:
    try:
        document = document_service.get_document(session, document_id)
    except document_service.DocumentError as error:
        raise _raise_for(error, error.code) from error

    _authorize_document_read(caller, document.employee_id)

    url = document_service.build_download_url(session, document, storage=storage)
    # A denial in the authorization step audited into the session; a successful read has nothing to
    # record here, so nothing is committed on this path.
    return DocumentDownloadResponse(url=url, expires_in_seconds=DOWNLOAD_URL_TTL_SECONDS)


def _authorize_document_read(caller: CurrentCaller, employee_id: uuid.UUID) -> None:
    """Permit an administrator, or the employee reading their own document; refuse the rest.

    Administrators have full access (Requirement 2.2). An employee may act only on their own record
    (Requirement 2.7), so an employee-role caller is allowed exactly when the document is theirs;
    `require_own_employee_record` audits and raises otherwise. Any other role — a site manager,
    accounting — is refused, because a passport or work permit is not theirs to read; the refusal is
    audited (Requirement 2.8).
    """
    if caller.role in authz.ROLES_WITH_FULL_ACCESS:
        return
    if caller.role in authz.OWN_RECORD_ONLY_ROLES:
        caller.require_own_employee_record(employee_id)
        return
    raise caller.deny(
        code="document_forbidden",
        reason="document_not_permitted_for_role",
        detail=f"employee={employee_id}",
    )


# --------------------------------------------------------------------------- delete


@router.delete(
    "/documents/{document_id}",
    status_code=HTTPStatus.NO_CONTENT,
    summary="Delete a document",
    description=(
        "Soft-deletes the document and removes its bytes from private storage. The row is kept for "
        "the audit trail. Administrator only."
    ),
)
def delete_document(
    document_id: uuid.UUID,
    caller: OperationsCaller,
    session: DbSession,
    context: AuthenticatedContext,
    storage: Storage,
) -> Response:
    try:
        document = document_service.get_document(session, document_id)
        document_service.delete_document(session, document, context=context, storage=storage)
        session.commit()
    except document_service.DocumentError as error:
        session.rollback()
        raise _raise_for(error, error.code) from error
    return Response(status_code=HTTPStatus.NO_CONTENT)
