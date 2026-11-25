"""The Excel export of the ticket report (Requirement 31; design.md, "Excel export (R31)").

`GET /staff/reports/export` answers with the workbook that `build_workbook` returns. The writer is this
module's own and small, because the import-boundary test forbids importing the Hours `app.export`
package. It uses `openpyxl` (a dependency already) and a normal, not write-only, workbook: the hard cap
of `EXPORT_MAX_ROWS` rows keeps it small.

Which tickets. The caller passes the conditions that `reports.matching_conditions` built and checked
from the report's own filter parameters, so the sheet holds exactly the tickets of the report table
(not only its visible page), in the order of that table: creation time descending, then ticket number
descending (R31.1, R31.2).

Cost. At most four statements whatever the number of rows (no query per ticket, R31.5): the checks of
the id filters (at most three single-row reads, made by `matching_conditions`), a `COUNT` with the same
conditions that runs first and refuses a set above `EXPORT_MAX_ROWS` with `ExportTooLarge` before
anything is built (R31.4), the ticket rows joined to customer, category and settlement with explicit
columns, and one statement for the replaced parts of all of them (`reports.load_parts`). The module
constant `EXPORT_MAX_ROWS` is read when the export runs, so a test can patch it.

Columns, in this order (R31.2): ticket number (integer), opened date (a date cell, number format
`dd/mm/yyyy`; the stored Asia/Jerusalem creation date), customer name, phone (text, so the leading zero
stays), settlement (the settlement's name in the language when the ticket has one, else the stored city
text), apartment, issue category, status, assigned technician, urgent (yes or no), parts replaced (names
joined by a comma and a space, ordered by part sequence) and the staff user who opened the ticket (or
empty). The description is not exported. Headers, category, settlement, part and status names and the
yes or no words follow `lang`, `he` or `en`. A Hebrew sheet is right to left. The header row is bold,
frozen and carries the filter buttons; column widths follow the longest value, capped at 60.

Formulas (R31.3). Every text cell is written through `safe_text` and bound with the string data type, so
a value is never a formula or an error literal whatever it starts with: a first character of `=`, `+`,
`-`, `@`, a tab or a carriage return gets one single quote added in front, characters that XML 1.0
cannot hold are dropped (otherwise openpyxl raises `IllegalCharacterError` for text a user typed), and
text longer than Excel's cell limit of 32,767 characters is cut.

The module reads only, never commits, and raises no HTTP errors: `ExportTooLarge` and `InvalidFilter`
carry the API code.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from io import BytesIO
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.service_tickets.error_codes import CODE_EXPORT_TOO_LARGE
from app.service_tickets.grammars import InvalidFilter
from app.service_tickets.models import Category, Customer, Locality, Ticket, TicketPart
from app.service_tickets.services.reports import ReportPart, load_parts

#: Most tickets one export may hold; one more is `export_too_large` (R31.4). A test patches this.
EXPORT_MAX_ROWS = 10_000

#: The content type of an `.xlsx` workbook.
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: The time zone of the date in the file name (R31.5).
EXPORT_TIMEZONE = "Asia/Jerusalem"

#: Excel's limit for the text of one cell.
CELL_MAX_CHARS = 32_767

#: Column width cap, in characters (Excel's unit).
MAX_COLUMN_WIDTH = 60

DATE_FORMAT = "dd/mm/yyyy"

LANGUAGES = ("he", "en")
DEFAULT_LANGUAGE = "he"

#: A text that starts with one of these could be read as a formula (R31.3).
_FORMULA_STARTERS = ("=", "+", "-", "@", "\t", "\r")

#: Characters XML 1.0 cannot hold: the control characters other than tab, line feed and carriage return,
#: lone surrogates and the two non-characters. openpyxl rejects the first group with
#: `IllegalCharacterError`; the others would fail when the file is written.
_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")

HEADERS: dict[str, tuple[str, ...]] = {
    "he": (
        "מספר קריאה",
        "תאריך פתיחה",
        "שם הדייר",
        "טלפון",
        "ישוב",
        "דירה",
        "קטגוריית תקלה",
        "סטטוס",
        "טכנאי מטפל",
        "דחוף",
        "חלקים שהוחלפו",
        "נפתחה על ידי הצוות",
    ),
    "en": (
        "Ticket number",
        "Opened",
        "Customer",
        "Phone",
        "Settlement",
        "Apartment",
        "Issue category",
        "Status",
        "Assigned technician",
        "Urgent",
        "Parts replaced",
        "Opened by staff",
    ),
}

STATUS_NAMES: dict[str, dict[str, str]] = {
    "he": {
        "received": "נקלטה במערכת",
        "in_progress": "בטיפול",
        "closed": "טופלה",
        "cancelled": "בוטלה",
    },
    "en": {
        "received": "Received",
        "in_progress": "In Progress",
        "closed": "Resolved",
        "cancelled": "Cancelled",
    },
}

_YES_NO: dict[str, tuple[str, str]] = {"he": ("כן", "לא"), "en": ("Yes", "No")}

_SHEET_TITLE = {"he": "קריאות שירות", "en": "Service tickets"}


class ExportTooLarge(Exception):
    """More than `EXPORT_MAX_ROWS` tickets match (R31.4). `code` is the API code (HTTP 422)."""

    code = CODE_EXPORT_TOO_LARGE

    def __init__(self) -> None:
        super().__init__(self.code)


def parse_lang(raw: str | None) -> str:
    """The export language: absent is `he`; `he` and `en` exactly; anything else raises `InvalidFilter`."""
    if raw is None:
        return DEFAULT_LANGUAGE
    if raw in LANGUAGES:
        return raw
    raise InvalidFilter


def export_date() -> date:
    """Today's calendar date in Asia/Jerusalem, for the file name. A test patches this function."""
    return datetime.now(UTC).astimezone(ZoneInfo(EXPORT_TIMEZONE)).date()


def export_filename(day: date | None = None) -> str:
    """`service-tickets-report-<YYYYMMDD>.xlsx`. The name holds only a date, never user text (R31.5)."""
    return f"service-tickets-report-{(day or export_date()):%Y%m%d}.xlsx"


def safe_text(value: object) -> str:
    """The string to store in a text cell for `value` (R31.3).

    `None` is the empty string. Characters that XML cannot hold are dropped first, so that a value like
    `"\\x00=1+1"` cannot hide its first character. Then a text whose first character is `=`, `+`, `-`,
    `@`, a tab or a carriage return gets one `'` in front, and the result is cut to `CELL_MAX_CHARS`
    characters (the quote counts). Newlines inside the text and non-Latin text pass through.
    """
    text = "" if value is None else str(value)
    text = _ILLEGAL_XML.sub("", text)
    if text.startswith(_FORMULA_STARTERS):
        text = "'" + text
    return text[:CELL_MAX_CHARS]


def _set_text(cell: Any, value: object) -> str:
    """Write `value` through `safe_text` with the string data type, so that openpyxl never reads it as a
    formula (`=...`) or as an error literal (`#N/A`). Returns the stored text."""
    text = safe_text(value)
    cell.value = text
    cell.data_type = "s"
    return text


def _width(text: str) -> int:
    """The length of the longest line of `text`: a cell with line breaks is as wide as its widest line."""
    return max((len(line) for line in text.splitlines()), default=0)


def _query(conditions: Sequence[ColumnElement[bool]]) -> Any:
    return (
        select(
            Ticket.id,
            Ticket.ticket_number,
            Ticket.created_date,
            Customer.full_name.label("customer_name"),
            Ticket.phone,
            Ticket.city,
            Locality.name_he.label("locality_name_he"),
            Locality.name_en.label("locality_name_en"),
            Ticket.apartment,
            Category.name_he.label("category_name_he"),
            Category.name_en.label("category_name_en"),
            Ticket.status,
            Ticket.assigned_technician_name,
            Ticket.urgent,
            Ticket.opened_by_username,
        )
        .join(Customer, Customer.id == Ticket.customer_id)
        .join(Category, Category.id == Ticket.category_id)
        .outerjoin(Locality, Locality.id == Ticket.locality_id)
        .where(*conditions)
        .order_by(Ticket.created_at.desc(), Ticket.ticket_number.desc())
        .limit(EXPORT_MAX_ROWS + 1)
    )


def build_workbook(session: Session, conditions: Sequence[ColumnElement[bool]], lang: str) -> bytes:
    """The `.xlsx` bytes for the tickets that match `conditions`, in language `lang` (`he` or `en`).

    Raises `ExportTooLarge`, before anything is built, when more than `EXPORT_MAX_ROWS` tickets match.
    """
    if lang not in LANGUAGES:
        raise InvalidFilter

    matching = session.execute(select(func.count()).select_from(Ticket).where(*conditions)).scalar_one()
    if int(matching) > EXPORT_MAX_ROWS:
        raise ExportTooLarge

    rows = session.execute(_query(conditions)).all()
    if len(rows) > EXPORT_MAX_ROWS:  # tickets arrived between the count and the read
        raise ExportTooLarge
    parts: dict[uuid.UUID, tuple[ReportPart, ...]] = {}
    if rows:
        parts = load_parts(session, TicketPart.ticket_id.in_(select(Ticket.id).where(*conditions)))

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = _SHEET_TITLE[lang]
    if lang == "he":
        sheet.sheet_view.rightToLeft = True

    headers = HEADERS[lang]
    widths = [len(header) for header in headers]
    bold = Font(bold=True)
    for column, header in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=column)
        _set_text(cell, header)
        cell.font = bold

    yes, no = _YES_NO[lang]
    statuses = STATUS_NAMES[lang]
    for index, row in enumerate(rows, start=2):
        locality_name = row.locality_name_he if lang == "he" else row.locality_name_en
        category_name = row.category_name_he if lang == "he" else row.category_name_en
        part_names = ", ".join(
            part.name_he if lang == "he" else part.name_en for part in parts.get(row.id, ())
        )
        texts = (
            row.customer_name,
            row.phone,
            locality_name if locality_name is not None else row.city,
            row.apartment,
            category_name,
            statuses.get(row.status, row.status),
            row.assigned_technician_name,
            yes if row.urgent else no,
            part_names,
            row.opened_by_username,
        )
        sheet.cell(row=index, column=1, value=int(row.ticket_number))
        widths[0] = max(widths[0], len(str(row.ticket_number)))
        date_cell = sheet.cell(row=index, column=2, value=row.created_date)
        date_cell.number_format = DATE_FORMAT
        widths[1] = max(widths[1], len(DATE_FORMAT))
        for offset, value in enumerate(texts):
            column = offset + 3
            stored = _set_text(sheet.cell(row=index, column=column), value)
            widths[column - 1] = max(widths[column - 1], _width(stored))

    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = min(width + 2, MAX_COLUMN_WIDTH)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
