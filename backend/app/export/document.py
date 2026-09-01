"""The `ExportDocument` — the format-neutral shape a report becomes before it is rendered.

This is the seam the whole package turns on (see the package note). A report or a payment request is
built into a document here; the Excel and PDF renderers read only this, so neither carries any
knowledge of payroll, billing or the report queries, and a new report renders in both formats as soon
as it can produce one of these.

The load-bearing idea is the *typed cell*. A figure is carried as its value plus a `CellKind` — never
as a pre-formatted string — because Requirement 19.1 asks that Excel numbers and dates land in cells
you can sum and subtract, which is only true if the workbook stores a real number and a real date.
Pre-formatting `"1,234.50 ₪"` into a string throws that away and cannot be undone. So the builder
states *what* a value is and each renderer decides *how* to show it: the Excel renderer sets the cell
type and number format, the PDF renderer formats the localized string.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


class CellKind(enum.Enum):
    """What a value *is*, independent of how a given format shows it.

    The distinction between the numeric kinds is not cosmetic. `MONEY` is a two-place decimal in ILS;
    `HOURS` is a two-place decimal of hours the eye reads as a duration; `INTEGER` is a whole count
    such as minutes or a headcount. The Excel renderer gives each its own number format so a column of
    money sums as money and a column of hours sums as hours, and the PDF renderer formats each into
    the string the locale expects. `DATE` and `DATETIME` are real temporal values so Excel stores them
    as dates, not as text a spreadsheet cannot sort chronologically (Requirement 19.1).
    """

    TEXT = "text"
    INTEGER = "integer"
    MONEY = "money"
    HOURS = "hours"
    DATE = "date"
    DATETIME = "datetime"


#: The Python types a cell of each kind may carry. A cell is validated against this on construction,
#: so a builder that puts a string where a number belongs fails at build time, in the report code, not
#: deep inside a renderer where the origin is lost.
_ALLOWED_TYPES: dict[CellKind, tuple[type, ...]] = {
    CellKind.TEXT: (str,),
    CellKind.INTEGER: (int,),
    CellKind.MONEY: (Decimal, int),
    CellKind.HOURS: (Decimal, int),
    CellKind.DATE: (date,),
    CellKind.DATETIME: (datetime,),
}


@dataclass(frozen=True, slots=True)
class Cell:
    """One value and what it is. `value` is `None` for a blank cell, allowed for any kind."""

    value: object | None
    kind: CellKind = CellKind.TEXT

    def __post_init__(self) -> None:
        if self.value is None:
            return
        # `date` is a superclass of `datetime`, so guard the narrower kind explicitly: a `datetime`
        # handed to a `DATE` cell would otherwise pass `isinstance(value, date)` and lose its time.
        if self.kind is CellKind.DATE and isinstance(self.value, datetime):
            raise TypeError("a DATE cell must carry a date, not a datetime")
        allowed = _ALLOWED_TYPES[self.kind]
        if not isinstance(self.value, allowed):
            raise TypeError(
                f"a {self.kind.name} cell cannot carry {type(self.value).__name__}"
            )


def cell(value: object | None, kind: CellKind = CellKind.TEXT) -> Cell:
    """Terse constructor so a builder reads as a table of cells rather than of `Cell(...)` calls."""
    return Cell(value=value, kind=kind)


@dataclass(frozen=True, slots=True)
class ExportColumn:
    """A column heading. `header` and `header_en` carry both languages so the renderer picks one."""

    header: str
    header_en: str
    kind: CellKind = CellKind.TEXT


@dataclass(frozen=True, slots=True)
class ExportTable:
    """One table: its columns, its rows of cells, and an optional totals row.

    A report is usually one table; the payment request is several, one per site, which is why a
    document holds a list of these rather than a single set of rows. `totals` is a row rendered
    distinctly (bold in the PDF, a summed row in Excel) — the report supplies it rather than the
    renderer computing it, so the total shown always equals the figure the report already reconciled.
    """

    columns: Sequence[ExportColumn]
    rows: Sequence[Sequence[Cell]]
    title: str | None = None
    title_en: str | None = None
    totals: Sequence[Cell] | None = None

    def __post_init__(self) -> None:
        width = len(self.columns)
        for index, row in enumerate(self.rows):
            if len(row) != width:
                raise ValueError(f"row {index} has {len(row)} cells, expected {width}")
        if self.totals is not None and len(self.totals) != width:
            raise ValueError(f"totals row has {len(self.totals)} cells, expected {width}")

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True, slots=True)
class ExportStamp:
    """The provenance every export carries (Requirement 19.4).

    Period, the filters applied, the moment it was generated and who generated it. Rendered into a
    header block in both formats so an archived file states, on its own face, what it is and where it
    came from. `filters` is an ordered mapping of a human label to its value, already resolved to
    display text by the builder — the stamp is presentation, not a place to re-run a query.
    """

    period_label: str
    generated_at: datetime
    generated_by: str
    filters: Sequence[tuple[str, str]] = ()
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class ExportDocument:
    """A whole export: a title, the provenance stamp, and one or more tables.

    Language is carried on the document (`language`), defaulting to Hebrew, and the renderers use it to
    pick the header and to set the PDF's direction. A document is built once and can be rendered to
    either format, which is what lets `POST /api/exports` choose the format at request time without the
    report being rebuilt per format.
    """

    title: str
    title_en: str
    stamp: ExportStamp
    tables: Sequence[ExportTable] = field(default_factory=tuple)
    language: str = "he"

    def localized_title(self) -> str:
        return self.title_en if self.language == "en" else self.title

    def is_rtl(self) -> bool:
        """Hebrew lays out right-to-left; English does not (Requirement 19.3, 21.3)."""
        return self.language != "en"


def total_row_count(document: ExportDocument) -> int:
    """Sum of the data rows across a document's tables — the size an async decision is made on."""
    return sum(table.row_count for table in document.tables)
