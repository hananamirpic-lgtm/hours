"""Report schemas (Requirement 18).

Locale-neutral like every other schema: no message text, only the values the front end formats. These
carry the responses the four report endpoints return, and every one of them carries the same envelope
around its rows — the `period`, the `filters` applied and the `currency` — because Requirement 18.7
says every report SHALL state its period and filters and show its figures in ILS. Putting that on a
shared base means an endpoint cannot forget it: the response *is* the envelope plus its rows.

* `GET /api/reports/by-employee` → `EmployeeReportResponse` — hours and cost per employee (18.1).
* `GET /api/reports/by-site` → `SiteReportResponse` — hours, billing, cost, profit per site (18.2).
* `GET /api/reports/by-client` → `ClientReportResponse` — amount per site and client total (18.3).
* `GET /api/reports/profitability` → `ProfitabilityResponse` — billing, cost, gross profit (18.4).

Money is `Decimal` end to end so the two-place figures the engines produced are never coerced through
a float. Minutes are integers, carried alongside each amount so the front end can show both the hours
and the amount without re-deriving one from the other.

The by-employee row's `cost` is `Decimal | None`: for a finance caller it is the employee's cost; for
a site manager it is stripped to `None`, because a manager may see hours for their sites but not the
wage behind them (Requirement 2.5). The router builds the row with cost omitted for a non-finance
caller, so the redaction happens once, at the boundary.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.services.reports import MissingReportKind


class ReportFiltersApplied(BaseModel):
    """The filters a report was run with, echoed back so the response is self-describing (18.7).

    `year` and `month` are the period, always present. The rest are the optional narrowings the
    profitability report accepts (Requirement 18.4); a report that did not use one reports it as
    `null`, so the reader can tell an unfiltered figure from a filtered one.
    """

    year: int
    month: int
    employee_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    project: str | None = None
    staffing_company_id: uuid.UUID | None = None


class _ReportEnvelope(BaseModel):
    """The period, the filters applied and the currency every report carries (Requirement 18.7).

    Shared by every report response so the requirement is met by construction rather than remembered
    per endpoint. `currency` defaults to `ILS`, the one currency the system deals in; it is a field
    rather than an assumption so an export or a client reading the JSON does not have to know it out
    of band.
    """

    year: int
    month: int
    filters: ReportFiltersApplied
    currency: str = "ILS"


# --------------------------------------------------------------------------- by employee (18.1)


class EmployeeReportRowResponse(BaseModel):
    """One employee's hours and cost for a period (Requirement 18.1).

    The four bucket minutes and their sum are always present; `cost` is present for a finance caller
    and `null` for a site manager, who may see the hours their sites earned but not the wage
    (Requirement 2.5).
    """

    employee_id: uuid.UUID
    employee_name: str
    employee_name_en: str
    employee_number: str | None = None
    regular_minutes: int
    overtime_minutes: int
    shabbat_minutes: int
    holiday_minutes: int
    total_minutes: int
    cost: Decimal | None = None


class EmployeeReportResponse(_ReportEnvelope):
    """The by-employee report (Requirement 18.1). `total_cost` is `null` when cost is redacted."""

    rows: list[EmployeeReportRowResponse] = Field(default_factory=list)
    total_minutes: int = 0
    total_cost: Decimal | None = None


class StaffingCompanyReportResponse(_ReportEnvelope):
    """The by-staffing-company report (Requirement 4).

    `total_minutes` is the sum of worked minutes over the period for the employees currently linked to
    the company; `total_payment` is those hours times the company's single flat `hourly_rate`, or
    `null` when the company has no rate set — the front end renders `null` as "unavailable" rather than
    zero (Requirement 4.5, 4.7).
    """

    company_id: uuid.UUID
    company_name: str
    hourly_rate: Decimal | None = None
    total_minutes: int = 0
    total_payment: Decimal | None = None


# --------------------------------------------------------------------------- by site (18.2)


class SiteReportRowResponse(BaseModel):
    """One site's hours, billing, cost and profit for a period (Requirement 18.2)."""

    site_id: uuid.UUID
    site_name: str
    client_id: uuid.UUID
    regular_minutes: int
    overtime_minutes: int
    total_minutes: int
    billing: Decimal
    cost: Decimal
    profit: Decimal


class SiteReportResponse(_ReportEnvelope):
    """The by-site report, with totals that equal the sum of the rows (Requirement 18.2, 18.9)."""

    rows: list[SiteReportRowResponse] = Field(default_factory=list)
    total_minutes: int = 0
    total_billing: Decimal = Decimal("0.00")
    total_cost: Decimal = Decimal("0.00")
    total_profit: Decimal = Decimal("0.00")


# --------------------------------------------------------------------------- by client (18.3)


class ClientSiteAmountResponse(BaseModel):
    """One site's billed amount under a client (Requirement 18.3)."""

    site_id: uuid.UUID
    site_name: str
    amount: Decimal


class ClientReportRowResponse(BaseModel):
    """One client's per-site amounts and its total (Requirement 18.3, 18.9)."""

    client_id: uuid.UUID
    sites: list[ClientSiteAmountResponse] = Field(default_factory=list)
    total: Decimal


class ClientReportResponse(_ReportEnvelope):
    """The by-client report, with a grand total that equals the sum of the client totals (18.3, 18.9)."""

    rows: list[ClientReportRowResponse] = Field(default_factory=list)
    total: Decimal = Decimal("0.00")


# --------------------------------------------------------------------------- profitability (18.4)


class ProfitabilityResponse(_ReportEnvelope):
    """Total billing, cost and gross profit for the selected filters (Requirement 18.4).

    `site_count` states how many sites contributed, so a filtered figure carries its own breadth.
    """

    total_billing: Decimal = Decimal("0.00")
    total_cost: Decimal = Decimal("0.00")
    total_profit: Decimal = Decimal("0.00")
    site_count: int = 0


# --------------------------------------------------------------------------- missing reports (14.3)


class MissingReportFindingResponse(BaseModel):
    """One missing-report finding: an employee, a date, a site and which kind (Requirement 14.3).

    `kind` is one of `missing_checkout`, `missing_checkin` or `both_missing`. The employee names are
    carried in both languages so the front end can label the row without a second lookup; the site
    names it so a manager alert can identify employee, site and what is missing (Requirement 14.4).
    Locale-neutral: no message text, only the values the front end formats.
    """

    employee_id: uuid.UUID
    employee_name: str
    employee_name_en: str
    employee_number: str | None = None
    work_date: date
    site_id: uuid.UUID
    site_name: str
    kind: MissingReportKind


# --------------------------------------------------------------------------- dashboard (18.5, 18.6)


class DashboardAttentionResponse(BaseModel):
    """The attention counts the administrator home dashboard heads (Requirement 18.6).

    Three counts over the current month — employees without a check-out, without a check-in, and days
    missing entirely — each of which the front end links to the missing-report list filtered to that
    kind. Counts only: the list is a separate read, so the dashboard stays a fast overview.
    """

    missing_checkout: int = 0
    missing_checkin: int = 0
    missing_reports: int = 0


class DashboardResponse(BaseModel):
    """The administrator home dashboard for the current month (Requirement 18.5, 18.6).

    `year` and `month` name the current month on the server, so the response says which period its
    figures cover. `active_employees` and `active_sites` are current counts; `total_minutes`,
    `total_billing`, `total_cost` and `total_profit` are the month's figures, taken from the by-site
    report's totals so the dashboard reconciles with it (Requirement 18.9). `attention` heads the
    call-to-action section (Requirement 18.6). Money is ILS (Requirement 18.7); `total_minutes` is
    carried as minutes and the front end renders the hours.
    """

    year: int
    month: int
    currency: str = "ILS"
    active_employees: int = 0
    active_sites: int = 0
    total_minutes: int = 0
    total_billing: Decimal = Decimal("0.00")
    total_cost: Decimal = Decimal("0.00")
    total_profit: Decimal = Decimal("0.00")
    attention: DashboardAttentionResponse


class MissingReportsResponse(BaseModel):
    """The missing-report findings for a date range (Requirement 14.3).

    Carries the range it was run over and the filters applied so the response is self-describing, in
    the spirit of Requirement 18.7, even though this is an attention list rather than a money report.
    `total` is the number of findings, so a caller can show a count without walking the list.
    """

    date_from: date
    date_to: date
    employee_id: uuid.UUID | None = None
    site_id: uuid.UUID | None = None
    findings: list[MissingReportFindingResponse] = Field(default_factory=list)
    total: int = 0
