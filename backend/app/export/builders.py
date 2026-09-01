"""Turn a report or a payment request into an `ExportDocument` (Requirement 19.1, 19.2, 19.4).

This is where the report-specific knowledge lives: which columns a report has, what each column is
called in Hebrew and English, and which `CellKind` each figure is. Everything downstream — the Excel
and PDF renderers — reads only the `ExportDocument` these functions produce, so the column layout of a
report is stated once here and both formats follow it.

Minutes are shown as hours (a two-place `Decimal`) because that is the unit the reports present to a
reader; the `HOURS` kind keeps the value numeric so an Excel column of hours still sums. Money stays a
`Decimal` and lands in a `MONEY` cell, never a pre-formatted string, which is the whole of the
typed-cell requirement (19.1).

Each builder produces the provenance stamp (Requirement 19.4) from the caller-supplied period,
filters, generation time and user, so an exported file states on its face what it is.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from app.export.document import (
    CellKind,
    ExportColumn,
    ExportDocument,
    ExportStamp,
    ExportTable,
    cell,
)
from app.services import billing as billing_service
from app.services import reports as reports_service

_MONTH_NAMES_HE = (
    "",
    "\u05d9\u05e0\u05d5\u05d0\u05e8",  # January
    "\u05e4\u05d1\u05e8\u05d5\u05d0\u05e8",  # February
    "\u05de\u05e8\u05e5",  # March
    "\u05d0\u05e4\u05e8\u05d9\u05dc",  # April
    "\u05de\u05d0\u05d9",  # May
    "\u05d9\u05d5\u05e0\u05d9",  # June
    "\u05d9\u05d5\u05dc\u05d9",  # July
    "\u05d0\u05d5\u05d2\u05d5\u05e1\u05d8",  # August
    "\u05e1\u05e4\u05d8\u05de\u05d1\u05e8",  # September
    "\u05d0\u05d5\u05e7\u05d8\u05d5\u05d1\u05e8",  # October
    "\u05e0\u05d5\u05d1\u05de\u05d1\u05e8",  # November
    "\u05d3\u05e6\u05de\u05d1\u05e8",  # December
)


def _period_label(year: int, month: int, *, language: str) -> str:
    if language == "en":
        return f"{month:02d}/{year}"
    return f"{_MONTH_NAMES_HE[month]} {year}"


def _hours(minutes: int) -> Decimal:
    """Minutes as a two-place decimal of hours, for an `HOURS` cell that still sums."""
    return (Decimal(minutes) / Decimal("60")).quantize(Decimal("0.01"))


def _stamp(
    *,
    year: int,
    month: int,
    generated_at: datetime,
    generated_by: str,
    language: str,
    filters: Sequence[tuple[str, str]] = (),
    currency: str | None = reports_service.CURRENCY,
) -> ExportStamp:
    return ExportStamp(
        period_label=_period_label(year, month, language=language),
        generated_at=generated_at,
        generated_by=generated_by,
        filters=tuple(filters),
        currency=currency,
    )


def _col(header_he: str, header_en: str, kind: CellKind = CellKind.TEXT) -> ExportColumn:
    return ExportColumn(header=header_he, header_en=header_en, kind=kind)


def _t(he: str, en: str, *, language: str) -> str:
    """Pick a label in the document's language. Keeps bilingual literals off the long lines."""
    return en if language == "en" else he


# --------------------------------------------------------------------------- by employee (18.1)


def employee_report_document(
    report: reports_service.EmployeeReport,
    *,
    year: int,
    month: int,
    generated_at: datetime,
    generated_by: str,
    language: str = "he",
    show_cost: bool = True,
) -> ExportDocument:
    """The by-employee report (Requirement 18.1) as a document.

    Cost is present only when the caller may read it (`show_cost`); a site manager's export carries the
    hours their sites earned but no wage (Requirement 2.5), so the cost column and total are dropped
    entirely rather than blanked.
    """
    columns = [
        _col("\u05e2\u05d5\u05d1\u05d3", "Employee"),
        _col("\u05e8\u05d2\u05d9\u05dc\u05d5\u05ea", "Regular", CellKind.HOURS),
        _col("\u05e0\u05d5\u05e1\u05e4\u05d5\u05ea", "Overtime", CellKind.HOURS),
        _col("\u05e9\u05d1\u05ea", "Shabbat", CellKind.HOURS),
        _col("\u05d7\u05d2", "Holiday", CellKind.HOURS),
        _col('\u05e1\u05d4"\u05db \u05e9\u05e2\u05d5\u05ea', "Total hours", CellKind.HOURS),
    ]
    if show_cost:
        columns.append(_col("\u05e2\u05dc\u05d5\u05ea", "Cost", CellKind.MONEY))

    rows = []
    for row in report.rows:
        cells = [
            cell(row.employee_name if language != "en" else row.employee_name_en),
            cell(_hours(row.regular_minutes), CellKind.HOURS),
            cell(_hours(row.overtime_minutes), CellKind.HOURS),
            cell(_hours(row.shabbat_minutes), CellKind.HOURS),
            cell(_hours(row.holiday_minutes), CellKind.HOURS),
            cell(_hours(row.total_minutes), CellKind.HOURS),
        ]
        if show_cost:
            cells.append(cell(row.cost, CellKind.MONEY))
        rows.append(cells)

    totals = [
        cell("\u05e1\u05d4\u05f4\u05db" if language != "en" else "Total"),
        cell(None, CellKind.HOURS),
        cell(None, CellKind.HOURS),
        cell(None, CellKind.HOURS),
        cell(None, CellKind.HOURS),
        cell(_hours(report.total_minutes), CellKind.HOURS),
    ]
    if show_cost:
        totals.append(cell(report.total_cost, CellKind.MONEY))

    table = ExportTable(columns=columns, rows=rows, totals=totals)
    return ExportDocument(
        title="\u05d3\u05d5\u05f4\u05d7 \u05e9\u05e2\u05d5\u05ea \u05dc\u05e4\u05d9 \u05e2\u05d5\u05d1\u05d3",
        title_en="Hours by employee",
        stamp=_stamp(
            year=year, month=month, generated_at=generated_at, generated_by=generated_by,
            language=language, currency=reports_service.CURRENCY if show_cost else None,
        ),
        tables=[table],
        language=language,
    )


# --------------------------------------------------------------------------- by site (18.2)


def site_report_document(
    report: reports_service.SiteReport,
    *,
    year: int,
    month: int,
    generated_at: datetime,
    generated_by: str,
    language: str = "he",
) -> ExportDocument:
    """The by-site report (Requirement 18.2): hours, billing, cost and profit per site."""
    columns = [
        _col("\u05d0\u05ea\u05e8", "Site"),
        _col("\u05e8\u05d2\u05d9\u05dc\u05d5\u05ea", "Regular", CellKind.HOURS),
        _col("\u05e0\u05d5\u05e1\u05e4\u05d5\u05ea", "Overtime", CellKind.HOURS),
        _col('\u05e1\u05d4"\u05db \u05e9\u05e2\u05d5\u05ea', "Total hours", CellKind.HOURS),
        _col("\u05d7\u05d9\u05d5\u05d1", "Billing", CellKind.MONEY),
        _col("\u05e2\u05dc\u05d5\u05ea", "Cost", CellKind.MONEY),
        _col("\u05e8\u05d5\u05d5\u05d7", "Profit", CellKind.MONEY),
    ]
    rows = [
        [
            cell(row.site_name),
            cell(_hours(row.regular_minutes), CellKind.HOURS),
            cell(_hours(row.overtime_minutes), CellKind.HOURS),
            cell(_hours(row.total_minutes), CellKind.HOURS),
            cell(row.billing, CellKind.MONEY),
            cell(row.cost, CellKind.MONEY),
            cell(row.profit, CellKind.MONEY),
        ]
        for row in report.rows
    ]
    totals = [
        cell("\u05e1\u05d4\u05f4\u05db" if language != "en" else "Total"),
        cell(None, CellKind.HOURS),
        cell(None, CellKind.HOURS),
        cell(_hours(report.total_minutes), CellKind.HOURS),
        cell(report.total_billing, CellKind.MONEY),
        cell(report.total_cost, CellKind.MONEY),
        cell(report.total_profit, CellKind.MONEY),
    ]
    table = ExportTable(columns=columns, rows=rows, totals=totals)
    return ExportDocument(
        title="\u05d3\u05d5\u05f4\u05d7 \u05dc\u05e4\u05d9 \u05d0\u05ea\u05e8",
        title_en="Report by site",
        stamp=_stamp(
            year=year, month=month, generated_at=generated_at, generated_by=generated_by,
            language=language,
        ),
        tables=[table],
        language=language,
    )


# --------------------------------------------------------------------------- by client (18.3)


def client_report_document(
    report: reports_service.ClientReport,
    *,
    year: int,
    month: int,
    generated_at: datetime,
    generated_by: str,
    client_names: dict,
    language: str = "he",
) -> ExportDocument:
    """The by-client report (Requirement 18.3): amount per site under each client, and client totals.

    `client_names` maps a client id to a display name; a missing id falls back to the id string so the
    export never fails on a name lookup.
    """
    columns = [
        _col("\u05dc\u05e7\u05d5\u05d7", "Client"),
        _col("\u05d0\u05ea\u05e8", "Site"),
        _col("\u05e1\u05db\u05d5\u05dd", "Amount", CellKind.MONEY),
    ]
    rows = []
    for client_row in report.rows:
        client_name = str(client_names.get(client_row.client_id, client_row.client_id))
        first = True
        for site in client_row.sites:
            rows.append(
                [
                    cell(client_name if first else ""),
                    cell(site.site_name),
                    cell(site.amount, CellKind.MONEY),
                ]
            )
            first = False
        subtotal_label = _t(
            f"{client_name} — \u05e1\u05d4\u05f4\u05db", f"{client_name} — total", language=language
        )
        rows.append([cell(subtotal_label), cell(""), cell(client_row.total, CellKind.MONEY)])
    totals = [
        cell(_t("\u05e1\u05d4\u05f4\u05db \u05db\u05dc\u05dc\u05d9", "Grand total", language=language)),
        cell(""),
        cell(report.total, CellKind.MONEY),
    ]
    table = ExportTable(columns=columns, rows=rows, totals=totals)
    return ExportDocument(
        title="\u05d3\u05d5\u05f4\u05d7 \u05dc\u05e4\u05d9 \u05dc\u05e7\u05d5\u05d7",
        title_en="Report by client",
        stamp=_stamp(
            year=year, month=month, generated_at=generated_at, generated_by=generated_by,
            language=language,
        ),
        tables=[table],
        language=language,
    )


# --------------------------------------------------------------------------- profitability (18.4)


def profitability_report_document(
    report: reports_service.ProfitabilityReport,
    *,
    year: int,
    month: int,
    generated_at: datetime,
    generated_by: str,
    language: str = "he",
    filters: Sequence[tuple[str, str]] = (),
) -> ExportDocument:
    """The profitability aggregate (Requirement 18.4): one row of billing, cost and profit totals."""
    columns = [
        _col("\u05de\u05d3\u05d3", "Metric"),
        _col("\u05e1\u05db\u05d5\u05dd", "Amount", CellKind.MONEY),
    ]
    billing_label = _t("\u05d7\u05d9\u05d5\u05d1", "Billing", language=language)
    cost_label = _t("\u05e2\u05dc\u05d5\u05ea", "Cost", language=language)
    profit_he = "\u05e8\u05d5\u05d5\u05d7 \u05d2\u05d5\u05dc\u05de\u05d9"
    profit_label = _t(profit_he, "Gross profit", language=language)
    rows = [
        [cell(billing_label), cell(report.total_billing, CellKind.MONEY)],
        [cell(cost_label), cell(report.total_cost, CellKind.MONEY)],
        [cell(profit_label), cell(report.total_profit, CellKind.MONEY)],
    ]
    table = ExportTable(columns=columns, rows=rows)
    site_count_label = (
        "\u05de\u05e1\u05e4\u05e8 \u05d0\u05ea\u05e8\u05d9\u05dd" if language != "en" else "Sites"
    )
    return ExportDocument(
        title="\u05d3\u05d5\u05f4\u05d7 \u05e8\u05d5\u05d5\u05d7\u05d9\u05d5\u05ea",
        title_en="Profitability report",
        stamp=_stamp(
            year=year, month=month, generated_at=generated_at, generated_by=generated_by,
            language=language, filters=[*filters, (site_count_label, str(report.site_count))],
        ),
        tables=[table],
        language=language,
    )


# --------------------------------------------------------------------------- payment request (18.8)


def payment_request_document(
    request: billing_service.PaymentRequest,
    *,
    generated_at: datetime,
    generated_by: str,
    language: str = "he",
) -> ExportDocument:
    """A client's payment request (Requirement 18.8, 19.2) as a document — one table per site.

    Each site is its own table so its employee lines and the site's billed total read together; the
    document's own totals are not used because the request carries a per-client grand total, which is
    rendered as a final one-row table.
    """
    columns = [
        _col("\u05e2\u05d5\u05d1\u05d3", "Employee"),
        _col('\u05e1\u05d4"\u05db \u05e9\u05e2\u05d5\u05ea', "Total hours", CellKind.HOURS),
        _col("\u05ea\u05e2\u05e8\u05d9\u05e3", "Rate", CellKind.MONEY),
        _col("\u05e1\u05db\u05d5\u05dd", "Amount", CellKind.MONEY),
    ]
    tables: list[ExportTable] = []
    for site in request.sites:
        rows = [
            [
                cell(line.employee_name if language != "en" else line.employee_name_en),
                cell(line.hours, CellKind.HOURS),
                cell(line.rate, CellKind.MONEY),
                cell(line.amount, CellKind.MONEY),
            ]
            for line in site.lines
        ]
        totals = [
            cell("\u05e1\u05d4\u05f4\u05db \u05d0\u05ea\u05e8" if language != "en" else "Site total"),
            cell(None, CellKind.HOURS),
            cell(None, CellKind.MONEY),
            cell(site.amount, CellKind.MONEY),
        ]
        site_title = f"{site.site_name} ({site.site_number})"
        tables.append(
            ExportTable(columns=columns, rows=rows, totals=totals, title=site_title, title_en=site_title)
        )

    # The client grand total, as a final single-row table so it reads apart from the site sections.
    tables.append(
        ExportTable(
            columns=[
                _col("", ""),
                _col("\u05e1\u05d4\u05f4\u05db \u05dc\u05e7\u05d5\u05d7", "Client total", CellKind.MONEY),
            ],
            rows=[[cell(""), cell(request.total_amount, CellKind.MONEY)]],
        )
    )

    title = (
        f"\u05d3\u05e8\u05d9\u05e9\u05ea \u05ea\u05e9\u05dc\u05d5\u05dd \u2014 {request.client_name}"
        if language != "en"
        else f"Payment request — {request.client_name}"
    )
    return ExportDocument(
        title=title,
        title_en=f"Payment request — {request.client_name}",
        stamp=_stamp(
            year=request.year, month=request.month, generated_at=generated_at,
            generated_by=generated_by, language=language,
            filters=[("\u05dc\u05e7\u05d5\u05d7" if language != "en" else "Client", request.client_name)],
        ),
        tables=tables,
        language=language,
    )
