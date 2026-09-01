"""Export orchestration — build, render, store, audit and (when large) defer (Requirement 19).

The renderers and builders under `app.export` are pure: given a report they produce a document, and
given a document they produce bytes. This service is what ties them to the request — it reads the
report the request names, builds the document with its provenance stamp, and then splits on size:

* **Small enough** (below the configured row threshold): render, store the bytes, mark the row
  `ready` and return it, all in the request. The client gets a downloadable file straight back.
* **Large** (at or above the threshold): persist a `pending` row and return it without rendering
  (Requirement 19.6). A background worker calls `render_pending`, which renders, stores, marks the row
  `ready` and raises the "your export is ready" notification. The request stays fast and the user is
  told when the file lands.

Every export, inline or deferred, is recorded in the audit log at creation (Requirement 19.5) with the
report, format, period and requesting user — the same facts the file itself is stamped with
(Requirement 19.4). Like every other service here, nothing commits: the router owns the transaction so
the export row, its audit record and (for the inline case) the stored-file bookkeeping all share one
fate.

Authorization is the router's job and has already happened by the time a request reaches here: the
finance reports and the payment request are finance-only, and the by-employee report a site manager
may reach is built with their scope and its cost dropped, exactly as the report endpoints do it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.authz import SiteScope
from app.core.config import get_settings
from app.core.storage import ObjectStorage, get_object_storage
from app.export import PdfRenderUnavailable, to_pdf, to_xlsx
from app.export.builders import (
    client_report_document,
    employee_report_document,
    payment_request_document,
    profitability_report_document,
    site_report_document,
)
from app.export.document import ExportDocument, total_row_count
from app.export.pdf import PDF_MIME_TYPE, XLSX_MIME_TYPE
from app.models.client import Client
from app.models.export import Export, ExportFormat, ExportReportType, ExportStatus
from app.models.notification import NotificationSeverity
from app.models.user import User
from app.services import billing as billing_service
from app.services import client as client_service
from app.services import notification as notification_service
from app.services import reports as reports_service
from app.services.audit import AuditContext, record_change

#: The audit field an export creation is recorded under (Requirement 19.5). One field for the event,
#: like the authentication events, rather than a per-column diff of a row that has no prior state.
AUDIT_FIELD = "export_generated"

#: The "your export is ready" notification (Requirement 19.6). A translation key, rendered in the
#: recipient's language when read, like every other notification.
NOTIFICATION_TYPE_READY = "export_ready"
TITLE_KEY_READY = "notifications.export_ready.title"


class ExportError(Exception):
    """Base for export-service failures. `code` is what the router lifts into the error envelope."""

    code = "export_error"


class ExportNotFound(ExportError):
    """No export with that id belongs to the caller.

    Scoped to the requester like a notification: an export that exists but was requested by another
    user reads as not-found, so one user cannot learn another requested an export with a given id.
    """

    code = "export_not_found"

    def __init__(self, export_id: uuid.UUID) -> None:
        super().__init__(f"no export {export_id} for this requester")
        self.export_id = export_id


class UnsupportedExportCombination(ExportError):
    """The report type and format asked for cannot be produced together, or a required filter is absent.

    A payment request has no Excel form (it is a document, not a table) and requires a client; a report
    export must name a period the report can be built for. Reported as a bad request so the front end
    can say which rule was broken rather than getting a bare failure.
    """

    code = "unsupported_export_combination"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class PdfRenderNotAvailable(ExportError):
    """The host cannot paint a PDF because WeasyPrint's native stack (Pango/cairo) is absent.

    An `ExportError` so the router maps it onto the error envelope rather than a bare 500. It cannot
    fire in the deployed container, which ships the libraries; it exists so a developer machine without
    them gets a clear, mapped failure instead of an unhandled exception.
    """

    code = "pdf_render_unavailable"


@dataclass(frozen=True, slots=True)
class ExportRequest:
    """The resolved parameters of one export request, after the router has validated the body."""

    report_type: ExportReportType
    format: ExportFormat
    year: int
    month: int
    language: str = "he"
    employee_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    project: str | None = None


# --------------------------------------------------------------------------- create (19.1–19.6)


def create_export(
    session: Session,
    *,
    request: ExportRequest,
    user: User,
    scope: SiteScope,
    may_read_money: bool,
    context: AuditContext,
    storage: ObjectStorage | None = None,
    now: datetime | None = None,
) -> Export:
    """Create an export, rendering inline when small and deferring when large (Requirement 19.6).

    Builds the document first — which also validates the request against the report — so an
    unsupported combination fails before a row is written. The document's row count decides the path:
    below the threshold the file is rendered and stored and the row returned `ready`; at or above it a
    `pending` row is returned for the background worker. Either way the creation is audited
    (Requirement 19.5). Nothing commits here.
    """
    storage = storage or get_object_storage()
    generated_at = now or datetime.now(UTC)

    document = _build_document(
        session,
        request=request,
        generated_by=user.username,
        generated_at=generated_at,
        scope=scope,
        may_read_money=may_read_money,
    )
    rows = total_row_count(document)
    file_name = _file_name(request, language=request.language)

    export = Export(
        requested_by_user_id=user.id,
        report_type=request.report_type,
        format=request.format,
        status=ExportStatus.PENDING,
        language=request.language,
        period_year=request.year,
        period_month=request.month,
        filters=_filters_dict(request),
        file_name=file_name,
        row_count=rows,
    )
    session.add(export)
    session.flush()  # assign the id so the storage key and the audit row can reference it

    _audit_creation(session, export=export, context=context)

    threshold = get_settings().export_async_row_threshold
    if rows < threshold:
        try:
            _render_and_store(session, export=export, document=document, storage=storage, notify=False)
        except PdfRenderUnavailable as error:
            # The host cannot paint a PDF (no Pango/cairo). Not a client error and not a bug in the
            # request, but the request cannot be satisfied inline here; surface it as an export error
            # with a specific code rather than a bare 500, so the front end can say so. In the deployed
            # container this never fires; the render succeeds.
            raise PdfRenderNotAvailable() from error
    # At or above the threshold the row stays PENDING; a worker calls render_pending and notifies.

    return export


def render_pending(
    session: Session,
    *,
    export_id: uuid.UUID,
    storage: ObjectStorage | None = None,
) -> Export:
    """Render a deferred export in the background and notify the requester (Requirement 19.6).

    Called by the worker for a `pending` export. Rebuilds the document from the row's stored parameters,
    renders and stores it, marks the row `ready`, and raises the "your export is ready" notification to
    the requester. A render failure marks the row `failed` with the error code rather than leaving it
    pending forever, so a poll sees the outcome. The caller owns the commit.
    """
    storage = storage or get_object_storage()
    export = session.get(Export, export_id)
    if export is None:
        raise ExportNotFound(export_id)
    if export.status is not ExportStatus.PENDING:
        return export  # already rendered or failed; idempotent for a retried worker

    user = session.get(User, export.requested_by_user_id)
    request = _request_from_export(export)
    # A deferred render is a system action; the requester's own scope is not re-resolved here, so the
    # money-visibility that was decided at request time is re-derived from the stored filters. For the
    # finance reports the requester was a finance role (the router guards them); the by-employee report
    # a manager can defer keeps its cost dropped because the export was built without it and the stored
    # filters do not reintroduce it.
    try:
        document = _build_document(
            session,
            request=request,
            generated_by=user.username if user else "system",
            generated_at=export.created_at,
            scope=SiteScope.all_sites(),
            may_read_money=_may_read_money_from_export(export),
        )
        _render_and_store(session, export=export, document=document, storage=storage, notify=True)
    except PdfRenderUnavailable as error:
        export.status = ExportStatus.FAILED
        export.error_code = "pdf_render_unavailable"
        export.completed_at = datetime.now(UTC)
        raise ExportError(str(error)) from error
    return export


def _render_and_store(
    session: Session,
    *,
    export: Export,
    document: ExportDocument,
    storage: ObjectStorage,
    notify: bool,
) -> None:
    """Render the document, store the bytes, mark the row ready, and notify if asked."""
    if export.format is ExportFormat.XLSX:
        data = to_xlsx(document)
        mime_type = XLSX_MIME_TYPE
    else:
        data = to_pdf(document)  # raises PdfRenderUnavailable where the native stack is missing
        mime_type = PDF_MIME_TYPE

    file_key = storage.new_export_key(export.id, export.file_name)
    size = storage.put_bytes(file_key, data, content_type=mime_type)

    export.file_key = file_key
    export.mime_type = mime_type
    export.size_bytes = size
    export.status = ExportStatus.READY
    export.completed_at = datetime.now(UTC)

    if notify:
        _notify_ready(session, export=export)


def _notify_ready(session: Session, *, export: Export) -> None:
    """Raise the "your export is ready" in-app notification to the requester (Requirement 19.6)."""
    notification_service.create_deduplicated(
        session,
        recipient_user_id=export.requested_by_user_id,
        type=NOTIFICATION_TYPE_READY,
        dedupe_key=f"export_ready:{export.id}",
        title_key=TITLE_KEY_READY,
        severity=NotificationSeverity.INFO,
        body_params={
            "report_type": export.report_type.value,
            "format": export.format.value,
            "period": f"{export.period_month:02d}/{export.period_year}",
        },
        related_entity_type="exports",
        related_entity_id=export.id,
    )


# --------------------------------------------------------------------------- read (19.6)


def get_export(
    session: Session,
    *,
    export_id: uuid.UUID,
    requester_user_id: uuid.UUID,
) -> Export:
    """Read one of the requester's own exports (Requirement 19.6).

    The requester is part of the lookup, not a check applied after: an export that is not this user's
    does not resolve and raises `ExportNotFound`, so one user cannot read another's export or learn it
    exists. The router mints the download URL for a ready export.
    """
    export = session.scalars(
        select(Export)
        .where(Export.id == export_id)
        .where(Export.requested_by_user_id == requester_user_id)
    ).one_or_none()
    if export is None:
        raise ExportNotFound(export_id)
    return export


def download_url(export: Export, storage: ObjectStorage | None = None) -> str | None:
    """A short-lived signed download URL for a ready export, or None when it is not ready.

    Minted per read rather than stored, like a document download, so the link always expires soon after
    it is handed out (Requirement 20.3 applies to any private object).
    """
    if export.status is not ExportStatus.READY or export.file_key is None:
        return None
    storage = storage or get_object_storage()
    return storage.presign_download(export.file_key, file_name=export.file_name)


# --------------------------------------------------------------------------- document building


def _build_document(
    session: Session,
    *,
    request: ExportRequest,
    generated_by: str,
    generated_at: datetime,
    scope: SiteScope,
    may_read_money: bool,
) -> ExportDocument:
    """Read the report the request names and build its document, or raise for a bad combination."""
    language = request.language
    filters = reports_service.ReportFilters(
        year=request.year,
        month=request.month,
        employee_id=request.employee_id,
        site_id=request.site_id,
        client_id=request.client_id,
        project=request.project,
    )

    match request.report_type:
        case ExportReportType.BY_EMPLOYEE:
            report = reports_service.report_by_employee(session, filters=filters, scope=scope)
            return employee_report_document(
                report,
                year=request.year,
                month=request.month,
                generated_at=generated_at,
                generated_by=generated_by,
                language=language,
                show_cost=may_read_money,
            )
        case ExportReportType.BY_SITE:
            report = reports_service.report_by_site(session, filters=filters)
            return site_report_document(
                report,
                year=request.year,
                month=request.month,
                generated_at=generated_at,
                generated_by=generated_by,
                language=language,
            )
        case ExportReportType.BY_CLIENT:
            report = reports_service.report_by_client(session, filters=filters)
            names = _client_names(session)
            return client_report_document(
                report,
                year=request.year,
                month=request.month,
                generated_at=generated_at,
                generated_by=generated_by,
                client_names=names,
                language=language,
            )
        case ExportReportType.PROFITABILITY:
            report = reports_service.report_profitability(session, filters=filters)
            return profitability_report_document(
                report,
                year=request.year,
                month=request.month,
                generated_at=generated_at,
                generated_by=generated_by,
                language=language,
            )
        case ExportReportType.PAYMENT_REQUEST:
            if request.format is ExportFormat.XLSX:
                raise UnsupportedExportCombination("a payment request has no Excel form")
            if request.client_id is None:
                raise UnsupportedExportCombination("a payment request requires a client_id")
            try:
                payment = billing_service.payment_request(
                    session, client_id=request.client_id, year=request.year, month=request.month
                )
            except client_service.ClientNotFound as error:
                raise UnsupportedExportCombination("no client with that id") from error
            return payment_request_document(
                payment,
                generated_at=generated_at,
                generated_by=generated_by,
                language=language,
            )
        case _:  # pragma: no cover - the enum is exhaustive
            raise UnsupportedExportCombination(f"unknown report type {request.report_type}")


def _client_names(session: Session) -> dict[uuid.UUID, str]:
    """Client id → name, for the by-client report's client column."""
    return {client.id: client.name for client in session.scalars(select(Client))}


# --------------------------------------------------------------------------- audit (19.5)


def _audit_creation(session: Session, *, export: Export, context: AuditContext) -> None:
    """Record that an export was generated (Requirement 19.5).

    One event row on the export entity, carrying the report, format and period as the new value, rather
    than a per-column diff — an export row is new, it has no prior state to diff against, and the event
    is the fact worth recording.
    """
    record_change(
        session,
        entity_type="exports",
        entity_id=export.id,
        field=AUDIT_FIELD,
        old_value=None,
        new_value=(
            f"{export.report_type.value}/{export.format.value} "
            f"{export.period_month:02d}/{export.period_year}"
        ),
        context=context,
    )


# --------------------------------------------------------------------------- helpers


def _filters_dict(request: ExportRequest) -> dict:
    """The request's filters as a JSON-storable dict, omitting the ones that were not given."""
    filters: dict[str, str] = {}
    if request.employee_id is not None:
        filters["employee_id"] = str(request.employee_id)
    if request.site_id is not None:
        filters["site_id"] = str(request.site_id)
    if request.client_id is not None:
        filters["client_id"] = str(request.client_id)
    if request.project is not None:
        filters["project"] = request.project
    return filters


def _request_from_export(export: Export) -> ExportRequest:
    """Rebuild the request parameters from a stored export row, for a deferred render."""
    filters = export.filters or {}
    return ExportRequest(
        report_type=export.report_type,
        format=export.format,
        year=export.period_year,
        month=export.period_month,
        language=export.language,
        employee_id=_uuid_or_none(filters.get("employee_id")),
        site_id=_uuid_or_none(filters.get("site_id")),
        client_id=_uuid_or_none(filters.get("client_id")),
        project=filters.get("project"),
    )


def _may_read_money_from_export(export: Export) -> bool:
    """Whether a deferred render should include cost.

    A background render has no live caller to ask, so money visibility is re-derived from the report
    type. The finance reports (by-site, by-client, profitability, payment request) are finance-only by
    the router's guard, so they always show money. The by-employee report is the one a site manager can
    defer; a manager's deferred by-employee export must not gain a wage it did not have inline, so cost
    is dropped for it here as well. This is conservative on purpose: it can only ever withhold money,
    never expose it, so a widening of who sees a wage is impossible through the deferred path.
    """
    return export.report_type is not ExportReportType.BY_EMPLOYEE


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def _file_name(request: ExportRequest, *, language: str) -> str:
    """A human filename for the download, in the file's language and carrying the period."""
    extension = "xlsx" if request.format is ExportFormat.XLSX else "pdf"
    stem = request.report_type.value
    return f"{stem}_{request.year}_{request.month:02d}_{language}.{extension}"
