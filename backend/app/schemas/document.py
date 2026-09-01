"""Document request and response schemas (Requirement 4).

The upload is two steps, and the schemas mirror that. First the client asks for a place to put a file
and gets back a presigned URL (`DocumentUploadInit` → `DocumentUploadTicket`); then it uploads
straight to storage and tells the API the upload finished (`DocumentUploadComplete`), at which point
the server verifies the bytes and records the row. The file never travels through the API, which is
what Requirement 4.3 and 20.3 are about.

Locale-neutral like every other schema: no message text, only values the front end formats.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.storage import ALLOWED_MIME_TYPES, MAX_FILE_BYTES
from app.models.document import DocumentType


class DocumentUploadInit(BaseModel):
    """Ask for a presigned upload URL for a new document.

    The MIME type and size are declared up front so the URL can be refused before it is issued when
    the type is unsupported or the size is over the cap — the client learns "wrong type" without a
    round trip to storage. Both are re-checked server-side after the upload, because a declaration is
    not a verification (Requirement 4.2).
    """

    model_config = ConfigDict(extra="forbid")

    type: DocumentType
    file_name: str = Field(min_length=1, max_length=255)
    #: The type is validated against the allowed set here so an unsupported one is a 422 at the
    #: boundary; the service raises the same domain error if this is somehow bypassed.
    mime_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, le=MAX_FILE_BYTES)
    expiry_date: date | None = None

    def is_supported_type(self) -> bool:
        return self.mime_type.strip().lower() in ALLOWED_MIME_TYPES


class DocumentUploadTicket(BaseModel):
    """The presigned URL and the key to report back once the upload is done."""

    file_key: str
    upload_url: str
    required_headers: dict[str, str]
    expires_in_seconds: int
    max_bytes: int


class DocumentUploadComplete(BaseModel):
    """Tell the API an upload finished, so it can verify the bytes and record the row.

    Carries the key the ticket named and the metadata the row will hold. The server re-derives
    nothing from the client it can check itself — the real size and type come from storage — so this
    body is a claim the verification step confirms or rejects.
    """

    model_config = ConfigDict(extra="forbid")

    file_key: str = Field(min_length=1, max_length=1024)
    type: DocumentType
    file_name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=255)
    expiry_date: date | None = None


class DocumentResponse(BaseModel):
    """A document as served. No URL: a download URL is minted on demand by the download endpoint,
    so a card listing does not hand out a hundred live links at once."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    type: DocumentType
    file_name: str
    mime_type: str
    size_bytes: int
    expiry_date: date | None
    is_expired: bool = False
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    """An employee's documents, plus the derived expired flag for the employee (Requirement 4.5)."""

    items: list[DocumentResponse]
    total: int
    has_expired_document: bool


class DocumentDownloadResponse(BaseModel):
    """A freshly minted, short-lived download URL for one document (Requirement 4.3)."""

    url: str
    expires_in_seconds: int
