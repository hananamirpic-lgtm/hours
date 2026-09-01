"""An in-memory object storage double for the document tests.

The real `ObjectStorage` talks to S3/MinIO; a unit test must not. This fake implements the same four
methods the service and router use — `new_key`, `presign_upload`, `presign_download`,
`verify_upload`, `delete` — over a dict, and enforces the *same* rules the real one does: it holds
the declared type and stored bytes, checks the size against the 10 MB cap, and matches the bytes
against the type's magic number. So a test that stores a PNG under an `application/pdf` claim, or a
file over the cap, fails through exactly the code paths Requirement 4.2 describes, without a network.

`put` is the test-only method standing in for the client's direct PUT to the presigned URL: a test
seeds the bytes the way a browser would, then calls the service's completion path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.storage import (
    DOWNLOAD_URL_TTL_SECONDS,
    MAX_FILE_BYTES,
    UPLOAD_URL_TTL_SECONDS,
    FileTooLarge,
    PresignedUpload,
    UploadNotFound,
    UploadTypeMismatch,
    VerifiedObject,
    _matches_signature,
    ensure_supported_type,
)


@dataclass
class _StoredObject:
    data: bytes


class FakeObjectStorage:
    """A dict-backed stand-in for `ObjectStorage` with the same surface and the same rules."""

    def __init__(self) -> None:
        self._objects: dict[str, _StoredObject] = {}
        #: Keys handed to `delete`, so a test can assert the bytes were removed on a soft delete.
        self.deleted_keys: list[str] = []

    # ------------------------------------------------------------------ test seeding
    def put(self, file_key: str, data: bytes) -> None:
        """Stand in for the client PUTting bytes to the presigned URL."""
        self._objects[file_key] = _StoredObject(data=data)

    # ------------------------------------------------------------------ the ObjectStorage surface
    def new_key(self, employee_id: uuid.UUID, file_name: str) -> str:
        safe = file_name.strip().replace("/", "_") or "document"
        return f"employees/{employee_id}/{uuid.uuid4()}/{safe}"

    def presign_upload(self, file_key: str, mime_type: str) -> PresignedUpload:
        content_type = ensure_supported_type(mime_type)
        return PresignedUpload(
            file_key=file_key,
            url=f"https://storage.local/upload/{file_key}",
            required_headers={"Content-Type": content_type},
            expires_in_seconds=UPLOAD_URL_TTL_SECONDS,
            max_bytes=MAX_FILE_BYTES,
        )

    def presign_download(self, file_key: str, *, file_name: str | None = None) -> str:
        return f"https://storage.local/download/{file_key}?ttl={DOWNLOAD_URL_TTL_SECONDS}"

    def verify_upload(self, file_key: str, declared_mime_type: str) -> VerifiedObject:
        declared = ensure_supported_type(declared_mime_type)
        stored = self._objects.get(file_key)
        if stored is None or not stored.data:
            raise UploadNotFound(file_key)
        size = len(stored.data)
        if size > MAX_FILE_BYTES:
            raise FileTooLarge(size)
        if not _matches_signature(declared, stored.data[:1024]):
            raise UploadTypeMismatch(declared)
        return VerifiedObject(file_key=file_key, mime_type=declared, size_bytes=size)

    def delete(self, file_key: str) -> None:
        self.deleted_keys.append(file_key)
        self._objects.pop(file_key, None)


# --------------------------------------------------------------------------- byte fixtures
# The smallest run of bytes each allowed type begins with, so a signature check passes for the right
# type and fails for the wrong one. Padded so the stored object is non-empty.

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"0" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 32
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

__all__ = [
    "JPEG_BYTES",
    "PDF_BYTES",
    "PNG_BYTES",
    "FakeObjectStorage",
]
