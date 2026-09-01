"""PDF rendering, right-to-left for Hebrew (Requirement 19.2, 19.3).

The design chose WeasyPrint for one reason: laying Hebrew out right-to-left is a solved problem in an
HTML+CSS layout engine and a wrong one in a hand-rolled PDF writer, which places glyphs left-to-right
and mangles bidirectional text. So this renderer does not draw a PDF. It builds an HTML document whose
`<html dir="rtl" lang="he">` and CSS carry the RTL layout, and hands that to WeasyPrint to paint.

That split is also what makes the RTL behaviour testable off the deployment target. WeasyPrint needs
Pango and cairo at runtime — present in the Linux container the app ships in, absent from a bare
developer machine — but the correctness the requirement asks about lives in the *HTML*: the direction,
the language, the Hebrew text, the logical `text-align`. `to_pdf_html` produces that string and is
pure Python, so a test asserts Hebrew renders right-to-left without a single native library. `to_pdf`
is the thin final step that turns the HTML into PDF bytes, and it raises `PdfRenderUnavailable` rather
than a raw import error when the native stack is missing, so a caller can tell "cannot render here"
apart from a genuine bug.

Escaping: every value that reaches the HTML goes through `html.escape`, so a client-supplied filter
value or an employee name containing `<` cannot break the markup or inject an element.
"""

from __future__ import annotations

import html
from collections.abc import Iterable

from app.export.document import Cell, CellKind, ExportColumn, ExportDocument, ExportTable

#: The MIME types the export service records and the client downloads under.
PDF_MIME_TYPE = "application/pdf"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class PdfRenderUnavailable(RuntimeError):
    """WeasyPrint's native rendering stack (Pango/cairo) is not present in this environment.

    Distinct from a programming error: it means the code is correct but the host cannot paint a PDF —
    the case on a developer machine without the GTK libraries. The container the app deploys to has
    them, so this never fires in the deployed system; raising it lets a test skip the byte-level
    assertion cleanly while still exercising the HTML that carries the RTL correctness.
    """


def to_pdf(document: ExportDocument) -> bytes:
    """Render a document to PDF bytes via WeasyPrint (Requirement 19.2).

    The HTML is built here and painted by WeasyPrint. Import is deferred to call time so importing this
    module — which the export service and its tests do — never triggers WeasyPrint's native library
    load; a machine without Pango can build the HTML and only fails if it actually asks for bytes.
    """
    markup = to_pdf_html(document)
    try:
        from weasyprint import HTML  # noqa: PLC0415 - deferred so the native load is call-time
    except OSError as error:  # the native libraries failed to load
        raise PdfRenderUnavailable(str(error)) from error
    try:
        return HTML(string=markup).write_pdf()
    except OSError as error:  # Pango/cairo missing at paint time
        raise PdfRenderUnavailable(str(error)) from error


def to_pdf_html(document: ExportDocument) -> str:
    """The HTML WeasyPrint paints — and the thing tests assert RTL correctness against.

    `dir` and `lang` on `<html>` are what make the whole document right-to-left for Hebrew
    (Requirement 19.3); the CSS uses logical `text-align` so the same stylesheet is correct in both
    directions. Pure Python, no native dependency.
    """
    direction = "rtl" if document.is_rtl() else "ltr"
    lang = document.language
    body = "\n".join(
        [
            _render_title(document),
            _render_stamp(document),
            *[_render_table(document, table) for table in document.tables],
        ]
    )
    return (
        "<!DOCTYPE html>\n"
        f'<html dir="{direction}" lang="{html.escape(lang)}">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<style>{_STYLE}</style>\n"
        f"<title>{html.escape(document.localized_title())}</title>\n"
        "</head>\n"
        f'<body dir="{direction}">\n'
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )


# The stylesheet lays the page out with logical properties so it is correct in either direction.
# `direction` is set on the elements from `dir`, and `text-align: start` follows it, so Hebrew aligns
# to the right and English to the left with no per-language stylesheet.
_STYLE = """
@page { size: A4; margin: 1.5cm; }
body { font-family: "DejaVu Sans", "Arial", sans-serif; font-size: 11px; color: #1a1a1a; }
h1 { font-size: 18px; margin: 0 0 4px 0; text-align: start; }
.stamp { margin: 0 0 16px 0; font-size: 10px; color: #444; }
.stamp .line { text-align: start; }
.stamp .label { font-weight: bold; }
table { width: 100%; border-collapse: collapse; margin: 0 0 18px 0; }
caption { text-align: start; font-weight: bold; font-size: 12px; margin-bottom: 4px; }
th, td { border: 1px solid #bbb; padding: 4px 6px; text-align: start; }
th { background: #f0f0f0; }
td.num { text-align: end; font-variant-numeric: tabular-nums; }
tr.totals td { font-weight: bold; background: #fafafa; }
""".strip()


def _render_title(document: ExportDocument) -> str:
    return f"<h1>{html.escape(document.localized_title())}</h1>"


def _render_stamp(document: ExportDocument) -> str:
    en = document.language == "en"
    stamp = document.stamp
    lines: list[tuple[str, str]] = [
        ("Period" if en else "\u05ea\u05e7\u05d5\u05e4\u05d4", stamp.period_label),
        (
            "Generated at" if en else "\u05d4\u05d5\u05e4\u05e7 \u05d1\u05ea\u05d0\u05e8\u05d9\u05da",
            stamp.generated_at.strftime("%d/%m/%Y %H:%M"),
        ),
        (
            "Generated by" if en else "\u05d4\u05d5\u05e4\u05e7 \u05e2\u05dc \u05d9\u05d3\u05d9",
            stamp.generated_by,
        ),
    ]
    lines.extend(stamp.filters)
    if stamp.currency:
        lines.append(("Currency" if en else "\u05de\u05d8\u05d1\u05e2", stamp.currency))

    rendered = "\n".join(
        f'<div class="line"><span class="label">{html.escape(label)}:</span> '
        f"{html.escape(value)}</div>"
        for label, value in lines
    )
    return f'<div class="stamp">\n{rendered}\n</div>'


def _render_table(document: ExportDocument, table: ExportTable) -> str:
    en = document.language == "en"
    parts: list[str] = ["<table>"]

    caption = table.title_en or table.title if en else table.title
    if caption:
        parts.append(f"<caption>{html.escape(caption)}</caption>")

    header_cells = "".join(
        f"<th>{html.escape(col.header_en if en else col.header)}</th>" for col in table.columns
    )
    parts.append(f"<thead><tr>{header_cells}</tr></thead>")

    parts.append("<tbody>")
    for row in table.rows:
        parts.append(f"<tr>{_render_row(document, table.columns, row)}</tr>")
    if table.totals is not None:
        parts.append(f'<tr class="totals">{_render_row(document, table.columns, table.totals)}</tr>')
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _render_row(document: ExportDocument, columns: Iterable[ExportColumn], row: Iterable[Cell]) -> str:
    cells: list[str] = []
    for one in row:
        text = _format_cell(document, one)
        css_class = ' class="num"' if _is_numeric(one.kind) else ""
        cells.append(f"<td{css_class}>{html.escape(text)}</td>")
    return "".join(cells)


def _is_numeric(kind: CellKind) -> bool:
    return kind in {CellKind.INTEGER, CellKind.MONEY, CellKind.HOURS}


def _format_cell(document: ExportDocument, one: Cell) -> str:
    """Format a typed value into the display string the PDF shows.

    The PDF is a rendered page, so a number here *is* a string — unlike the Excel cell, which stays a
    number. Formatting is done from the typed value at render time rather than pre-baked, so a report
    still hands the renderer a `Decimal` and the money/hours/date presentation is decided in one place.
    """
    value = one.value
    if value is None:
        return ""
    match one.kind:
        case CellKind.MONEY:
            return f"{value:,.2f}"
        case CellKind.HOURS:
            return f"{value:,.2f}"
        case CellKind.INTEGER:
            return f"{value:,}"
        case CellKind.DATE:
            return value.strftime("%d/%m/%Y")
        case CellKind.DATETIME:
            return value.strftime("%d/%m/%Y %H:%M")
        case _:
            return str(value)
