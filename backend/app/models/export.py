"""The `exports` table — a requested export and where it ended up (Requirement 19).

An export is a small state machine, not a file. `POST /api/exports` creates a row; the render either
finishes inline and leaves the row `ready` with a stored file, or — when the report is large — leaves
it `pending` for a background worker to fill in and notify on (Requirement 19.6). `GET /api/exports/{id}`
reads the row back and, when it is ready, hands out a short-lived download. So the row is the durable
record of the request and its outcome; the bytes live in object storage under `file_key`, the same
place employee documents live, never in this table.

The stamp fields — `report_type`, `format`, `period_year`/`period_month`, `filters`, `requested_by` —
are what every export is stamped with (Requirement 19.4). They are kept on the row rather than only
inside the rendered file so the export *list* can show them and so the audit record and the file agree
on what was generated.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Text, Uuid
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base


class ExportFormat(enum.StrEnum):
    """The two output formats (Requirement 19.1, 19.2). Values match the `export_format` enum."""

    XLSX = "xlsx"
    PDF = "pdf"


class ExportReportType(enum.StrEnum):
    """Which report or document is being exported (Requirement 19.1, 19.2).

    Every report and the payment request, so the seam is closed: adding a member here and a builder is
    all a new exportable report needs. `PDF` is valid for all of them; `XLSX` for the reports, since a
    payment request is a document rather than a table — but the model does not forbid it, the router
    decides which combinations it offers.
    """

    BY_EMPLOYEE = "by_employee"
    BY_SITE = "by_site"
    BY_CLIENT = "by_client"
    PROFITABILITY = "profitability"
    PAYMENT_REQUEST = "payment_request"


class ExportStatus(enum.StrEnum):
    """The export's lifecycle (Requirement 19.6).

    `READY` inline for a small export, or `PENDING` then `READY` for one rendered in the background;
    `FAILED` if the render raised, so a caller polling `GET /api/exports/{id}` learns the outcome
    rather than waiting forever.
    """

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


def _enum_column(enum_type: type[enum.Enum], name: str) -> Enum:
    return Enum(
        enum_type,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: JSONB in PostgreSQL, plain JSON on SQLite — the same variant the notifications table uses so the
#: unit tests can round-trip the filters dict without a PostgreSQL server.
_JSON_FILTERS = JSON().with_variant(postgresql.JSONB(), "postgresql")


class Export(Base):
    """One requested export and its outcome."""

    __tablename__ = "exports"

    __table_args__ = (
        Index("ix_exports_requested_by_user_id_created_at", "requested_by_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", name="fk_exports_requested_by_user_id_users"),
        nullable=False,
    )

    report_type: Mapped[ExportReportType] = mapped_column(
        _enum_column(ExportReportType, "export_report_type"), nullable=False
    )
    format: Mapped[ExportFormat] = mapped_column(
        _enum_column(ExportFormat, "export_format"), nullable=False
    )
    status: Mapped[ExportStatus] = mapped_column(
        _enum_column(ExportStatus, "export_status"), nullable=False, default=ExportStatus.PENDING
    )

    #: The interface language the file was rendered in (Requirement 19.3). Persisted so a re-render or
    #: an audit reads back the same language the file carries.
    language: Mapped[str] = mapped_column(Text, nullable=False, default="he")

    #: The period (Requirement 19.4). Present for the report exports; a payment request also carries a
    #: period, so both are non-null in practice.
    period_year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_month: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The filters applied, as the request supplied them (Requirement 19.4). A dict rather than columns
    #: because the filter set differs per report; kept on the row so the export list can show it.
    filters: Mapped[dict] = mapped_column(_JSON_FILTERS, nullable=False, default=dict)

    #: A human filename the download is offered under, in the file's language.
    file_name: Mapped[str] = mapped_column(Text, nullable=False)

    #: Storage key of the rendered bytes, set once the render succeeds; null while pending or on
    #: failure. The bytes live in object storage, not here.
    file_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Data rows across the document, the figure the async threshold is decided on (Requirement 19.6).
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Set when a background render fails, so a caller sees why rather than a bare `FAILED`.
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"Export({self.report_type} {self.format} {self.status})"
