"""Export schemas (Requirement 19).

Two endpoints:

* `POST /api/exports` → `ExportResponse` — request an export of a report or a payment request in Excel
  or PDF for a period. The response is the export row: `ready` with a download when the render finished
  inline, or `pending` when the report was large enough to render in the background (Requirement 19.6).
* `GET /api/exports/{id}` → `ExportResponse` — read an export back, with a short-lived download URL once
  it is ready.

The request carries the report type, the format, the period, the language the file is rendered in
(Requirement 19.3) and the report's own filters. The filters differ per report, so they are optional
fields the service validates against the chosen report rather than a single rigid shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.models.export import ExportFormat, ExportReportType, ExportStatus


class ExportCreateRequest(BaseModel):
    """A request to generate one export (Requirement 19.1, 19.2, 19.4).

    `report_type` and `format` name what to render and how; `year`/`month` are the period every export
    is stamped with (Requirement 19.4). `language` picks the file's language and direction — Hebrew is
    laid out right-to-left (Requirement 19.3). The optional ids are the report's filters, applied where
    the report supports them and echoed onto the stamp; a payment request requires `client_id`.
    """

    report_type: ExportReportType
    format: ExportFormat
    year: Annotated[int, Field(ge=2000, le=2200)]
    month: Annotated[int, Field(ge=1, le=12)]
    language: Annotated[str, Field(pattern="^(he|en)$")] = "he"
    employee_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    project: Annotated[str | None, Field(default=None, max_length=200)] = None


class ExportResponse(BaseModel):
    """One export row, with a download URL once it is ready (Requirement 19.6).

    `status` is `pending` while a large export renders in the background, `ready` when the file is
    available, or `failed` with an `error_code` when a background render raised. `download_url` is a
    short-lived signed URL, present only when the export is ready; it is not stored on the row but
    minted per read, like a document download, so it always expires soon after it is handed out.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    report_type: ExportReportType
    format: ExportFormat
    status: ExportStatus
    language: str
    period_year: int
    period_month: int
    filters: dict = Field(default_factory=dict)
    file_name: str
    mime_type: str | None = None
    size_bytes: int | None = None
    row_count: int | None = None
    error_code: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    download_url: str | None = None
