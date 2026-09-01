"""An in-memory object storage double for the export tests.

The real `ObjectStorage` talks to S3/MinIO; a unit test must not. Exports differ from documents in how
the bytes get into storage — the server renders them and calls `put_bytes`, rather than a client
PUTting to a presigned URL — so this fake implements the export surface the export service uses:
`new_export_key`, `put_bytes` and `presign_download`. It keeps the stored bytes so a test can read back
what a real render produced and assert the workbook is typed or the PDF carries Hebrew.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass
class _StoredExport:
    data: bytes
    content_type: str


class FakeExportStorage:
    """A dict-backed stand-in for `ObjectStorage`, implementing the export surface only."""

    def __init__(self) -> None:
        self._objects: dict[str, _StoredExport] = {}
        #: Keys presigned for download, so a test can assert a ready export handed out a URL.
        self.download_keys: list[str] = []

    def new_export_key(self, export_id: uuid.UUID, file_name: str) -> str:
        safe = file_name.strip().replace("/", "_") or "export"
        return f"exports/{export_id}/{uuid.uuid4()}/{safe}"

    def put_bytes(self, file_key: str, data: bytes, *, content_type: str) -> int:
        self._objects[file_key] = _StoredExport(data=data, content_type=content_type)
        return len(data)

    def presign_download(self, file_key: str, *, file_name: str | None = None) -> str:
        self.download_keys.append(file_key)
        return f"https://storage.local/download/{file_key}"

    # ------------------------------------------------------------------ test read-back
    def bytes_at(self, file_key: str) -> bytes:
        """The stored bytes, so a test can load the workbook or inspect the PDF."""
        return self._objects[file_key].data

    @property
    def stored_keys(self) -> list[str]:
        return list(self._objects)


__all__ = ["FakeExportStorage"]
