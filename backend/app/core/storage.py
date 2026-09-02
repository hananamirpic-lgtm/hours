"""Private object storage for employee documents (Requirement 4.2, 4.3, 20.3).

The file bytes never pass through the API. The client uploads straight to private storage with a
short-lived presigned PUT URL, and downloads straight from it with a short-lived presigned GET URL;
the API only ever holds the key and the metadata. That keeps large uploads off the application
process and, more importantly, means a document is only ever reachable through a URL that expires —
there is no public read path and no long-lived link (Requirement 20.3).

Two guards are the point of this module:

* **The presigned upload is constrained, not open.** A naive presigned PUT would let a client store
  anything of any size under the key. The upload URL is issued with conditions that bind the
  content type and cap the size, so storage itself rejects a wrong-type or oversize object — the
  client cannot talk the browser past them.
* **The type is verified server-side after the upload.** A `Content-Type` header is a claim, and a
  claim is not a verification (Requirement 4.2 asks for the real type). Once the client reports the
  upload finished, the service reads the first bytes back and checks the file's *magic number*
  against the allowed set. A PNG renamed to `.pdf`, or an executable sent with `image/jpeg`, fails
  here regardless of what header it carried.

The allowed types are PDF, JPEG and PNG, and the cap is 10 MB, both from Requirement 4.2. They live
here as the single source of truth so the presign conditions and the post-upload check cannot drift
apart.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.config import get_settings
from app.repositories.health import get_object_storage_client, get_object_storage_presign_client

# --------------------------------------------------------------------------- allowed types and size

#: Requirement 4.2. Maps each accepted MIME type to the leading bytes ("magic number") a real file of
#: that type begins with. The header a client sends is checked against this key set; the bytes read
#: back after upload are checked against these signatures, so a mislabelled file is caught even when
#: its header lied.
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
}

#: The MIME types an upload may declare. Anything else is refused before a URL is issued.
ALLOWED_MIME_TYPES: frozenset[str] = frozenset(_SIGNATURES)

#: The image subset, for flows that accept a photo rather than a document. A profile photo is a face,
#: not a passport scan, so a PDF is refused even though it is an allowed *document* type. Kept here
#: beside the signatures so the presign guard and the post-upload check read the same set and cannot
#: drift; every member is also in `ALLOWED_MIME_TYPES`, so the signatures already cover it.
IMAGE_MIME_TYPES: frozenset[str] = frozenset({"image/jpeg", "image/png"})

#: Requirement 4.2: 10 MB per file, to the byte.
MAX_FILE_BYTES: int = 10 * 1024 * 1024

#: How long an issued URL is valid. Short by design (Requirement 20.3): long enough to complete a
#: single upload or download, not long enough to be a durable link worth sharing.
UPLOAD_URL_TTL_SECONDS = 300
DOWNLOAD_URL_TTL_SECONDS = 300

#: Read only the first kilobyte back for the magic-number check. Every signature is a handful of
#: bytes at offset zero; there is no reason to pull the whole file to verify its type.
_SNIFF_BYTES = 1024


# --------------------------------------------------------------------------- errors


class StorageError(Exception):
    """Base for storage-layer failures. `code` is what the router lifts into the error envelope."""

    code = "storage_error"


class UnsupportedFileType(StorageError):
    """The declared or actual MIME type is not one of PDF, JPEG, PNG (Requirement 4.2)."""

    code = "unsupported_file_type"

    def __init__(self, mime_type: str) -> None:
        super().__init__(f"unsupported file type {mime_type!r}")
        self.mime_type = mime_type


class FileTooLarge(StorageError):
    """The uploaded object exceeds the 10 MB cap (Requirement 4.2)."""

    code = "file_too_large"

    def __init__(self, size_bytes: int) -> None:
        super().__init__(f"file is {size_bytes} bytes, over the {MAX_FILE_BYTES} limit")
        self.size_bytes = size_bytes


class UploadNotFound(StorageError):
    """No object exists at the key the client claims to have uploaded to."""

    code = "upload_not_found"

    def __init__(self, file_key: str) -> None:
        super().__init__(f"no object at {file_key!r}")
        self.file_key = file_key


class UploadTypeMismatch(StorageError):
    """The uploaded bytes are not of the type the file claims (Requirement 4.2, server-side check)."""

    code = "upload_type_mismatch"

    def __init__(self, declared: str) -> None:
        super().__init__(f"uploaded bytes are not a valid {declared}")
        self.declared = declared


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    """A short-lived, constrained URL a client PUTs a file to, and the key it lands under."""

    file_key: str
    url: str
    #: The header the client must send with the PUT, so the server-side conditions accept it.
    required_headers: dict[str, str]
    expires_in_seconds: int
    max_bytes: int


@dataclass(frozen=True, slots=True)
class VerifiedObject:
    """The facts about an uploaded object, confirmed server-side against storage."""

    file_key: str
    mime_type: str
    size_bytes: int


# --------------------------------------------------------------------------- validation helpers


def ensure_supported_type(mime_type: str) -> str:
    """Return `mime_type` if it is one PDF/JPEG/PNG, else raise `UnsupportedFileType`."""
    normalised = mime_type.strip().lower()
    if normalised not in ALLOWED_MIME_TYPES:
        raise UnsupportedFileType(mime_type)
    return normalised


def ensure_image_type(mime_type: str) -> str:
    """Return `mime_type` if it is JPEG or PNG, else raise `UnsupportedFileType`.

    The narrower gate for a profile photo: a PDF is a valid *document* but not a valid photo, so it is
    refused here even though `ensure_supported_type` would accept it. Both the presign step and the
    server-side verification go through this, so a PDF cannot slip past either.
    """
    normalised = mime_type.strip().lower()
    if normalised not in IMAGE_MIME_TYPES:
        raise UnsupportedFileType(mime_type)
    return normalised


def _matches_signature(mime_type: str, head: bytes) -> bool:
    return any(head.startswith(signature) for signature in _SIGNATURES.get(mime_type, ()))


# --------------------------------------------------------------------------- storage operations


class ObjectStorage:
    """Thin wrapper over the S3 client for the document upload/download lifecycle.

    A class rather than free functions so a test can substitute a fake with the same four methods and
    the service never touches boto3 directly. The default instance uses the process-wide client the
    health probe already builds and caches.
    """

    def __init__(self, client=None, *, bucket: str | None = None, presign_client=None) -> None:  # noqa: ANN001
        self._client = client if client is not None else get_object_storage_client()
        # A separate client for signing browser-facing URLs, built against the public endpoint (see
        # `get_object_storage_presign_client`). Falls back to the main client when not provided — a
        # test's fake passes one client and uses it for everything, which is correct for a fake.
        self._presign_client = (
            presign_client
            if presign_client is not None
            else (get_object_storage_presign_client() if client is None else client)
        )
        self._bucket = bucket if bucket is not None else get_settings().s3_bucket_documents

    def new_key(self, employee_id: uuid.UUID, file_name: str) -> str:
        """A unique, unguessable key for a new document, namespaced by employee.

        The random component means two uploads of the same file name never collide, and a key cannot
        be guessed from an employee id — belt and braces alongside the private bucket and the signed
        URLs, since the key is never a substitute for authorization.
        """
        safe_name = file_name.strip().replace("/", "_") or "document"
        return f"employees/{employee_id}/{uuid.uuid4()}/{safe_name}"

    def new_export_key(self, export_id: uuid.UUID, file_name: str) -> str:
        """A unique key for a rendered export (Requirement 19), namespaced under `exports/`.

        Exports are rendered server-side and their bytes stored with `put_bytes`, unlike documents,
        which the client uploads. The key is still unguessable and carries the export id, so a stored
        file traces back to its row and cannot be reached without the signed download URL.
        """
        safe_name = file_name.strip().replace("/", "_") or "export"
        return f"exports/{export_id}/{uuid.uuid4()}/{safe_name}"

    def put_bytes(self, file_key: str, data: bytes, *, content_type: str) -> int:
        """Store server-rendered bytes under `file_key` and return the byte count (Requirement 19).

        Used for exports, whose bytes are produced by the renderer rather than uploaded by a client, so
        there is no presigned PUT and no post-upload magic-number check — the API produced the bytes
        and knows their type. The object lands in the same private bucket the documents use, reachable
        only through a signed download URL.
        """
        self._client.put_object(
            Bucket=self._bucket, Key=file_key, Body=data, ContentType=content_type
        )
        return len(data)

    def presign_upload(self, file_key: str, mime_type: str) -> PresignedUpload:
        """A presigned PUT URL that storage will only accept for `mime_type` up to the size cap.

        The content type is pinned in the signature, so the client must send exactly that header and
        cannot store a different type under the key. The size cap is enforced server-side after the
        upload as well, because a plain presigned PUT cannot bound the body by itself; pinning the
        type here narrows the abuse surface and the post-upload check closes it.
        """
        content_type = ensure_supported_type(mime_type)
        url = self._presign_client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self._bucket, "Key": file_key, "ContentType": content_type},
            ExpiresIn=UPLOAD_URL_TTL_SECONDS,
        )
        return PresignedUpload(
            file_key=file_key,
            url=url,
            required_headers={"Content-Type": content_type},
            expires_in_seconds=UPLOAD_URL_TTL_SECONDS,
            max_bytes=MAX_FILE_BYTES,
        )

    def presign_download(self, file_key: str, *, file_name: str | None = None) -> str:
        """A short-lived GET URL for a file already stored (Requirement 4.3).

        Authorization is the caller's job, checked before this is ever called: a signed URL is a
        capability, and handing one out is the act of granting access, so the check must have
        happened first.
        """
        params: dict[str, str] = {"Bucket": self._bucket, "Key": file_key}
        if file_name:
            # Ask the browser to download under the original name rather than the opaque key.
            params["ResponseContentDisposition"] = f'attachment; filename="{file_name}"'
        return self._presign_client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=DOWNLOAD_URL_TTL_SECONDS
        )

    def verify_upload(self, file_key: str, declared_mime_type: str) -> VerifiedObject:
        """Confirm an object exists, is within the size cap, and really is its declared type.

        This is the server-side verification of Requirement 4.2. `head_object` gives the true stored
        size, which is checked against the 10 MB cap; the first bytes are read back and matched
        against the type's magic number, which catches a file whose header lied about what it is.
        Raises the specific storage error for each failure so the router can report which rule was
        broken.
        """
        declared = ensure_supported_type(declared_mime_type)

        try:
            head = self._client.head_object(Bucket=self._bucket, Key=file_key)
        except Exception as error:  # noqa: BLE001 - any failure to locate the object is "not found"
            raise UploadNotFound(file_key) from error

        size_bytes = int(head.get("ContentLength", 0))
        if size_bytes <= 0:
            raise UploadNotFound(file_key)
        if size_bytes > MAX_FILE_BYTES:
            raise FileTooLarge(size_bytes)

        head_bytes = self._read_head(file_key)
        if not _matches_signature(declared, head_bytes):
            raise UploadTypeMismatch(declared)

        return VerifiedObject(file_key=file_key, mime_type=declared, size_bytes=size_bytes)

    def delete(self, file_key: str) -> None:
        """Remove the stored object. Best-effort: a soft-deleted row can outlive its bytes."""
        self._client.delete_object(Bucket=self._bucket, Key=file_key)

    def _read_head(self, file_key: str) -> bytes:
        response = self._client.get_object(
            Bucket=self._bucket, Key=file_key, Range=f"bytes=0-{_SNIFF_BYTES - 1}"
        )
        body = response["Body"]
        try:
            return body.read(_SNIFF_BYTES)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()


def get_object_storage() -> ObjectStorage:
    """Default object storage over the shared S3 client. Overridden in tests with a fake."""
    return ObjectStorage()
