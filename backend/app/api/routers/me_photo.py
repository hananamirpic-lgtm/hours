"""Self-service profile photo router (Requirement 3.1, employee-facing).

HTTP only: guard, validate the schema, resolve the caller's *own* employee, call the service, map a
refusal onto a status code and the error envelope, and shape the response. The service owns every
rule and never commits, so this module commits after a successful write — the audit row the service
added rides along with it, keeping the change and its audit record in one transaction (Requirement
13.2). This mirrors `app.api.routers.scans` and `app.api.routers.documents`.

The distinction from `documents.py` is authorization and scope. The document flow is
administrator-only and keyed by an `employee_id` in the path; this flow is the employee acting on
their own record. It uses `EmployeeCaller` (which admits the employee role and, per Requirement 2.2,
the administrator) and acts strictly on `caller.user.employee_id` — no employee identifier is ever
accepted from the client, so there is no field a caller could set to touch someone else's photo. A
login with no linked employee has no photo of its own and is refused cleanly, exactly as the scan
path refuses a not-linked caller.

A profile photo is images only (`image/jpeg`, `image/png`); a PDF is refused even though it is a
valid *document* type. The type is constrained when the upload URL is minted and verified again
server-side by magic number when the upload is reported complete (Requirement 4.2), and the 10 MB cap
is enforced at completion. The three endpoints:

* ``POST /api/me/photo/upload-url`` — a short-lived, image- and size-constrained presigned URL.
* ``POST /api/me/photo`` — verify the uploaded bytes, set ``photo_key``, return the key and a fresh
  view URL.
* ``GET /api/me/photo`` — the caller's own photo as a short-lived signed URL, or ``null``.

Storage is a dependency so a test can substitute a fake with the same surface and no request touches
real object storage.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import (
    AuthenticatedContext,
    DbSession,
    EmployeeCaller,
    api_error,
)
from app.core.storage import (
    DOWNLOAD_URL_TTL_SECONDS,
    ObjectStorage,
    StorageError,
    get_object_storage,
)
from app.schemas.photo import (
    PhotoResponse,
    PhotoUploadComplete,
    PhotoUploadInit,
    PhotoUploadTicket,
    PhotoUrlResponse,
)
from app.services import employee as employee_service

router = APIRouter(prefix="/me/photo", tags=["me-photo"])

#: Object storage, injected so a test can override it with a fake and no request touches real S3.
Storage = Annotated[ObjectStorage, Depends(get_object_storage)]


# --------------------------------------------------------------------------- error mapping

#: Which HTTP status each domain error maps onto. A login with no linked employee is a 403 — the
#: caller is authenticated but has no record of its own. A wrong or unsupported type is a 400, an
#: oversize file a 413, matching the document flow's codes. A missing upload is a 404. Anything else
#: from storage is a 400: the client's completion referred to something that does not verify.
_ERROR_STATUS: dict[str, HTTPStatus] = {
    employee_service.NoEmployeeForCaller.code: HTTPStatus.FORBIDDEN,
    employee_service.EmployeeNotFound.code: HTTPStatus.NOT_FOUND,
    "unsupported_file_type": HTTPStatus.BAD_REQUEST,
    "file_too_large": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
    "upload_not_found": HTTPStatus.NOT_FOUND,
    "upload_type_mismatch": HTTPStatus.BAD_REQUEST,
    "storage_error": HTTPStatus.BAD_REQUEST,
}


def _raise_for(code: str) -> HTTPException:
    return api_error(_ERROR_STATUS.get(code, HTTPStatus.BAD_REQUEST), code)


def _own_employee_id(caller: EmployeeCaller) -> uuid.UUID:
    """The caller's own linked employee id, or refuse a login that has none.

    Self-scoping lives here: every endpoint acts on this id and nothing else, so a caller can never
    name another person's record. A login with no linked employee (a manager or accounting account
    admitted by the guard as the administrator would be) has no photo of its own, and is refused with
    the same domain code the scan path uses — audited nowhere here because it is not an authorization
    denial against a resource, just the absence of one.
    """
    employee_id = caller.user.employee_id
    if employee_id is None:
        raise _raise_for(employee_service.NoEmployeeForCaller.code)
    return employee_id


# --------------------------------------------------------------------------- upload


@router.post(
    "/upload-url",
    response_model=PhotoUploadTicket,
    summary="Get a presigned upload URL for the caller's own profile photo",
    description=(
        "Returns a short-lived, image- and size-constrained URL the client PUTs the photo to. Only "
        "image/jpeg and image/png are accepted; a PDF or other type is refused before the URL is "
        "issued. The real type and size are verified server-side when the upload is reported complete "
        "(Requirement 4.2). Always the caller's own employee — no id is accepted from the client."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: {"description": "Unsupported (non-image) file type"},
        HTTPStatus.FORBIDDEN: {"description": "The caller is not linked to an employee"},
    },
)
def create_photo_upload_url(
    payload: PhotoUploadInit,
    caller: EmployeeCaller,
    session: DbSession,
    storage: Storage,
) -> PhotoUploadTicket:
    employee_id = _own_employee_id(caller)
    try:
        presigned = employee_service.begin_photo_upload(
            session, employee_id, mime_type=payload.mime_type, storage=storage
        )
    except employee_service.EmployeeError as error:
        raise _raise_for(error.code) from error
    except StorageError as error:
        raise _raise_for(error.code) from error
    return PhotoUploadTicket(
        file_key=presigned.file_key,
        upload_url=presigned.url,
        required_headers=presigned.required_headers,
        expires_in_seconds=presigned.expires_in_seconds,
        max_bytes=presigned.max_bytes,
    )


@router.post(
    "",
    response_model=PhotoResponse,
    summary="Set the caller's own profile photo after its upload is complete",
    description=(
        "Verifies the uploaded object server-side — that it exists, is within the 10 MB cap, and "
        "really is its declared image type by magic number — before setting the caller's photo. A "
        "verification failure changes nothing (Requirement 4.2). Returns the stored key and a fresh "
        "short-lived view URL so the screen can show the new image at once. Always the caller's own "
        "employee."
    ),
    responses={
        HTTPStatus.BAD_REQUEST: {"description": "The uploaded bytes are not a valid image"},
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE: {"description": "The uploaded file exceeds 10 MB"},
        HTTPStatus.FORBIDDEN: {"description": "The caller is not linked to an employee"},
        HTTPStatus.NOT_FOUND: {"description": "No uploaded object at the key"},
    },
)
def set_photo(
    payload: PhotoUploadComplete,
    caller: EmployeeCaller,
    session: DbSession,
    context: AuthenticatedContext,
    storage: Storage,
) -> PhotoResponse:
    employee_id = _own_employee_id(caller)
    try:
        employee = employee_service.complete_photo_upload(
            session,
            employee_id,
            file_key=payload.file_key,
            mime_type=payload.mime_type,
            storage=storage,
            context=context,
        )
        session.commit()
    except (employee_service.EmployeeError, StorageError) as error:
        session.rollback()
        code = getattr(error, "code", "storage_error")
        raise _raise_for(code) from error

    # The photo was just set, so `photo_key` is present; a fresh view URL lets the screen render it
    # immediately without a second round trip.
    assert employee.photo_key is not None  # noqa: S101 - guaranteed by a successful completion
    url = storage.presign_download(employee.photo_key)
    return PhotoResponse(
        photo_key=employee.photo_key, url=url, expires_in_seconds=DOWNLOAD_URL_TTL_SECONDS
    )


# --------------------------------------------------------------------------- read


@router.get(
    "",
    response_model=PhotoUrlResponse,
    summary="The caller's own profile photo as a short-lived signed URL",
    description=(
        "Returns a signed URL valid for a few minutes, or null when the caller has no photo on file "
        "(or the login is not linked to an employee — which is refused). Always the caller's own "
        "employee; no id is accepted from the client."
    ),
    responses={HTTPStatus.FORBIDDEN: {"description": "The caller is not linked to an employee"}},
)
def get_photo(
    caller: EmployeeCaller,
    session: DbSession,
    storage: Storage,
) -> PhotoUrlResponse:
    employee_id = _own_employee_id(caller)
    try:
        url = employee_service.photo_download_url(session, employee_id, storage=storage)
    except employee_service.EmployeeError as error:
        raise _raise_for(error.code) from error
    return PhotoUrlResponse(url=url, expires_in_seconds=DOWNLOAD_URL_TTL_SECONDS)
