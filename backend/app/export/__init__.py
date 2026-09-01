"""Export rendering — Excel and PDF for every report and the payment request (Requirement 19).

The package is built around one seam, the `ExportDocument`. A report or a payment request is turned
into a document — a title, a stamp naming the period, the filters, the generation time and the user
(Requirement 19.4), and one or more tables of typed cells — and the two renderers, `to_xlsx` and
`to_pdf`, consume nothing but that. Neither renderer knows what a payroll record is; a report added
later renders in both formats the moment it can produce a document, and the RTL and typed-cell
behaviour is written once rather than once per report.

Why a typed cell rather than a formatted string. Requirement 19.1 asks that Excel numbers and dates
be usable in formulas, which they are only if the workbook stores them as a number and a date, not as
text that looks like one. So the document carries the *value* and its *kind* — text, integer, money,
hours, date, datetime — and each renderer decides how to present that kind: Excel sets the cell's
type and number format, the PDF formats it into the localized string. A report never pre-formats a
figure into a string, because a string cannot be un-formatted back into a number.
"""

from __future__ import annotations

from app.export.document import (
    CellKind,
    ExportColumn,
    ExportDocument,
    ExportStamp,
    ExportTable,
    cell,
)
from app.export.excel import to_xlsx
from app.export.pdf import PDF_MIME_TYPE, XLSX_MIME_TYPE, PdfRenderUnavailable, to_pdf, to_pdf_html

__all__ = [
    "PDF_MIME_TYPE",
    "XLSX_MIME_TYPE",
    "CellKind",
    "ExportColumn",
    "ExportDocument",
    "ExportStamp",
    "ExportTable",
    "PdfRenderUnavailable",
    "cell",
    "to_pdf",
    "to_pdf_html",
    "to_xlsx",
]
