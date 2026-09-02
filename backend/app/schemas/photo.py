"""Self-service profile photo schemas (Requirement 3.1, employee-facing).

The employee mobile app lets a signed-in employee replace their *own* profile photo. Like the
document upload it is a two-step, file-never-through-the-API flow (Requirements 4.3, 20.3), so the
shapes mirror `DocumentUploadInit`/`DocumentUploadTicket`/`DocumentUploadComplete` — but constrained
to images. A profile photo is a face, not a passport scan: only `image/jpeg` and `image/png` are
accepted, never a PDF. The type is checked at the boundary here and again server-side after the
upload, because a declaration is not a verification.

Locale-neutral like every other schema: no message text, only values the front end formats. Every
model forbids unknown fields, and no request model carries an employee identifier — the endpoints
resolve the caller's own employee server-side, so there is no field a client could set to act on
someone else.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.storage import IMAGE_MIME_TYPES


class PhotoUploadInit(BaseModel):
    """Ask for a presigned upload URL for the caller's own profile photo.

    The declared MIME type is checked against the image set here so an unsupported one (a PDF, a GIF)
    is refused before a URL is minted; the real type is verified server-side after the upload, since
    a `Content-Type` header is a claim the bytes have to back up (Requirement 4.2).
    """

    model_config = ConfigDict(extra="forbid")

    mime_type: str = Field(min_length=1, max_length=255)

    def is_supported_type(self) -> bool:
        return self.mime_type.strip().lower() in IMAGE_MIME_TYPES


class PhotoUploadTicket(BaseModel):
    """The presigned URL and the key to report back once the upload is done.

    Same shape as the document ticket: the client PUTs the file to `upload_url` with
    `required_headers`, then calls the completion endpoint with `file_key`.
    """

    file_key: str
    upload_url: str
    required_headers: dict[str, str]
    expires_in_seconds: int
    max_bytes: int


class PhotoUploadComplete(BaseModel):
    """Tell the API the upload finished, so it can verify the bytes and set the photo.

    Carries only the key the ticket named and the declared type; the real size and type come from
    storage, so this body is a claim the verification step confirms or rejects. No employee id — the
    photo set is always the caller's own.
    """

    model_config = ConfigDict(extra="forbid")

    file_key: str = Field(min_length=1, max_length=1024)
    mime_type: str = Field(min_length=1, max_length=255)


class PhotoResponse(BaseModel):
    """The result of setting a photo: the stored key, and a fresh short-lived view URL.

    The URL lets the mobile screen show the new image immediately without a second round trip; it is
    short-lived by design, so it is not a durable link.
    """

    photo_key: str
    url: str
    expires_in_seconds: int


class PhotoUrlResponse(BaseModel):
    """The caller's own photo as a short-lived signed URL, or its absence.

    `url` is `null` when the employee has no photo on file (or the login is not linked to an
    employee), which the mobile screen renders as a placeholder.
    """

    url: str | None
    expires_in_seconds: int
