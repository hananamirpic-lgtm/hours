"""Reports router — monthly aggregations over the computed payroll and billing records (Requirement 18).

HTTP only: guard, read the period and filters from the query, call the reports service, and shape the
response with its period, filters and currency (Requirement 18.7). The reports are pure reads — the
service recomputes nothing, it groups the figures the payroll and billing engines already produced —
so there is no transaction to commit here.

Four endpoints, three of them finance-only and one shared:

* `GET /api/reports/by-employee` — hours and cost per employee (Requirement 18.1). This is the one
  report a site manager may reach, because it can show *hours* for their sites; but a manager may not
  see the wage, so the cost is stripped for a non-finance caller (Requirement 2.5) and their rows are
  narrowed to the employees who worked their sites. The guard is therefore `HoursReaderCaller`
  (administrators, accounting and site managers), and the caller's role decides whether cost is shown.
* `GET /api/reports/by-site` — hours, billing, cost and profit per site (Requirement 18.2).
* `GET /api/reports/by-client` — the amount per site and the client total (Requirement 18.3).
* `GET /api/reports/profitability` — total billing, cost and gross profit for the filters (18.4).

The last three carry billing, cost and profit — finance data — so each is behind `FinanceCaller`,
which admits administrators and accounting only (Requirement 2.5). A site manager never receives a
billing or profit figure and so has no business on any of them.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession, FinanceCaller, HoursReaderCaller
from app.schemas.reports import (
    ClientReportResponse,
    ClientReportRowResponse,
    ClientSiteAmountResponse,
    DashboardAttentionResponse,
    DashboardResponse,
    EmployeeReportResponse,
    EmployeeReportRowResponse,
    MissingReportFindingResponse,
    MissingReportsResponse,
    ProfitabilityResponse,
    ReportFiltersApplied,
    SiteReportResponse,
    SiteReportRowResponse,
)
from app.services import reports as reports_service

router = APIRouter(prefix="/reports", tags=["reports"])

#: Bounds for the year and month, matching the check constraints on the record tables so a bad value
#: is a validation error before it reaches a query.
_YEAR = Annotated[int, Query(ge=2000, le=2200)]
_MONTH = Annotated[int, Query(ge=1, le=12)]


def _filters_applied(filters: reports_service.ReportFilters) -> ReportFiltersApplied:
    """Echo the service filters back onto the response envelope (Requirement 18.7)."""
    return ReportFiltersApplied(
        year=filters.year,
        month=filters.month,
        employee_id=filters.employee_id,
        site_id=filters.site_id,
        client_id=filters.client_id,
        project=filters.project,
    )


# --------------------------------------------------------------------------- by employee (18.1)


@router.get(
    "/by-employee",
    response_model=EmployeeReportResponse,
    summary="Hours and cost per employee for a period",
    description=(
        "Hours and cost per employee for a month, from the payroll records (Requirement 18.1). Each "
        "row's cost is the employee's payroll total for the month, so the report reconciles with "
        "payroll exactly (Requirement 18.9). Administrators and accounting see cost; a site manager "
        "sees the hours their sites earned but not the wage, and their rows are narrowed to their "
        "sites (Requirement 2.5). Figures are in ILS (Requirement 18.7)."
    ),
)
def report_by_employee(
    caller: HoursReaderCaller,
    session: DbSession,
    year: _YEAR,
    month: _MONTH,
    employee_id: uuid.UUID | None = None,
) -> EmployeeReportResponse:
    filters = reports_service.ReportFilters(year=year, month=month, employee_id=employee_id)
    report = reports_service.report_by_employee(session, filters=filters, scope=caller.scope)

    show_cost = caller.may_read_money_fields
    rows = [
        EmployeeReportRowResponse(
            employee_id=row.employee_id,
            employee_name=row.employee_name,
            employee_name_en=row.employee_name_en,
            regular_minutes=row.regular_minutes,
            overtime_minutes=row.overtime_minutes,
            shabbat_minutes=row.shabbat_minutes,
            holiday_minutes=row.holiday_minutes,
            total_minutes=row.total_minutes,
            cost=row.cost if show_cost else None,
        )
        for row in report.rows
    ]
    return EmployeeReportResponse(
        year=year,
        month=month,
        filters=_filters_applied(filters),
        currency=reports_service.CURRENCY,
        rows=rows,
        total_minutes=report.total_minutes,
        total_cost=report.total_cost if show_cost else None,
    )


# --------------------------------------------------------------------------- by site (18.2)


@router.get(
    "/by-site",
    response_model=SiteReportResponse,
    summary="Hours, billing, cost and profit per site for a period",
    description=(
        "Hours, billing, cost and profit per site for a month (Requirement 18.2). Billing comes from "
        "the billing records; cost is the payroll cost allocated to the site; profit is billing minus "
        "cost. The totals equal the sum of the rows (Requirement 18.9). Administrators and accounting "
        "only (Requirement 2.5). Figures are in ILS (Requirement 18.7)."
    ),
)
def report_by_site(
    caller: FinanceCaller,  # noqa: ARG001 — the type is the guard
    session: DbSession,
    year: _YEAR,
    month: _MONTH,
    site_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
) -> SiteReportResponse:
    filters = reports_service.ReportFilters(
        year=year, month=month, site_id=site_id, client_id=client_id
    )
    report = reports_service.report_by_site(session, filters=filters)
    return SiteReportResponse(
        year=year,
        month=month,
        filters=_filters_applied(filters),
        currency=reports_service.CURRENCY,
        rows=[
            SiteReportRowResponse(
                site_id=row.site_id,
                site_name=row.site_name,
                client_id=row.client_id,
                regular_minutes=row.regular_minutes,
                overtime_minutes=row.overtime_minutes,
                total_minutes=row.total_minutes,
                billing=row.billing,
                cost=row.cost,
                profit=row.profit,
            )
            for row in report.rows
        ],
        total_minutes=report.total_minutes,
        total_billing=report.total_billing,
        total_cost=report.total_cost,
        total_profit=report.total_profit,
    )


# --------------------------------------------------------------------------- by client (18.3)


@router.get(
    "/by-client",
    response_model=ClientReportResponse,
    summary="Amount per site and client total for a period",
    description=(
        "The billed amount per site under each client, and the client total (Requirement 18.3). Each "
        "client total is the sum of its sites' amounts, and the grand total is the sum of the client "
        "totals (Requirement 18.9). Administrators and accounting only (Requirement 2.5). Figures are "
        "in ILS (Requirement 18.7)."
    ),
)
def report_by_client(
    caller: FinanceCaller,  # noqa: ARG001 — the type is the guard
    session: DbSession,
    year: _YEAR,
    month: _MONTH,
    client_id: uuid.UUID | None = None,
) -> ClientReportResponse:
    filters = reports_service.ReportFilters(year=year, month=month, client_id=client_id)
    report = reports_service.report_by_client(session, filters=filters)
    return ClientReportResponse(
        year=year,
        month=month,
        filters=_filters_applied(filters),
        currency=reports_service.CURRENCY,
        rows=[
            ClientReportRowResponse(
                client_id=row.client_id,
                sites=[
                    ClientSiteAmountResponse(
                        site_id=site.site_id, site_name=site.site_name, amount=site.amount
                    )
                    for site in row.sites
                ],
                total=row.total,
            )
            for row in report.rows
        ],
        total=report.total,
    )


# --------------------------------------------------------------------------- profitability (18.4)


@router.get(
    "/profitability",
    response_model=ProfitabilityResponse,
    summary="Total billing, cost and gross profit for the selected filters",
    description=(
        "Total billing, employee cost and gross profit for a month under the given filters — month, "
        "employee, site, client and project (Requirement 18.4). The figures are the sum over the "
        "sites the filters admit, so a filtered profitability figure reconciles with the by-site "
        "report over the same filters (Requirement 18.9). Administrators and accounting only "
        "(Requirement 2.5). Figures are in ILS (Requirement 18.7)."
    ),
)
def report_profitability(
    caller: FinanceCaller,  # noqa: ARG001 — the type is the guard
    session: DbSession,
    year: _YEAR,
    month: _MONTH,
    employee_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    project: Annotated[str | None, Query(max_length=200)] = None,
) -> ProfitabilityResponse:
    filters = reports_service.ReportFilters(
        year=year,
        month=month,
        employee_id=employee_id,
        site_id=site_id,
        client_id=client_id,
        project=project,
    )
    report = reports_service.report_profitability(session, filters=filters)
    return ProfitabilityResponse(
        year=year,
        month=month,
        filters=_filters_applied(filters),
        currency=reports_service.CURRENCY,
        total_billing=report.total_billing,
        total_cost=report.total_cost,
        total_profit=report.total_profit,
        site_count=report.site_count,
    )


# --------------------------------------------------------------------------- dashboard (18.5, 18.6)


@router.get(
    "/dashboard",
    response_model=DashboardResponse,
    summary="Administrator home dashboard for the current month",
    description=(
        "The administrator home dashboard (Requirement 18.5): for the current month on the server, "
        "the number of active employees and active sites, total work hours, billing, employee cost "
        "and gross profit, with the finance figures taken from the by-site report's totals so the "
        "dashboard reconciles with it (Requirement 18.9). It also carries the attention section "
        "(Requirement 18.6): counts of employees without a check-out, without a check-in, and days "
        "missing entirely, each of which the front end links to the filtered missing-report list. "
        "Billing, cost and profit are finance figures, so the endpoint is administrators and "
        "accounting only (Requirement 2.5). Figures are in ILS (Requirement 18.7)."
    ),
)
def dashboard(
    caller: FinanceCaller,  # noqa: ARG001 — the type is the guard
    session: DbSession,
) -> DashboardResponse:
    report = reports_service.build_dashboard(session, today=date.today())
    return DashboardResponse(
        year=report.year,
        month=report.month,
        currency=report.currency,
        active_employees=report.active_employees,
        active_sites=report.active_sites,
        total_minutes=report.total_minutes,
        total_billing=report.total_billing,
        total_cost=report.total_cost,
        total_profit=report.total_profit,
        attention=DashboardAttentionResponse(
            missing_checkout=report.attention.missing_checkout,
            missing_checkin=report.attention.missing_checkin,
            missing_reports=report.attention.missing_reports,
        ),
    )


# --------------------------------------------------------------------------- missing reports (14.3)


@router.get(
    "/missing-reports",
    response_model=MissingReportsResponse,
    summary="Missing time reports for a date range, scoped by role",
    description=(
        "Missing time reports of three kinds — missing check-out, missing check-in and both missing — "
        "for the days an employee was expected at a site (Requirement 14.3). The same detection feeds "
        "the notifications of Task 30, so a report and an alert never disagree. Administrators and "
        "accounting see every site; a site manager sees only the sites they are assigned "
        "(Requirement 2.3). Optional employee and site filters narrow further, and the date range is "
        "inclusive — a single day passes the same date for both bounds (Requirement 22.3)."
    ),
)
def report_missing_reports(
    caller: HoursReaderCaller,
    session: DbSession,
    date_from: date,
    date_to: date,
    employee_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
) -> MissingReportsResponse:
    findings = reports_service.detect_missing_reports(
        session,
        date_from=date_from,
        date_to=date_to,
        scope=caller.scope,
        employee_id=employee_id,
        site_id=site_id,
    )
    return MissingReportsResponse(
        date_from=date_from,
        date_to=date_to,
        employee_id=employee_id,
        site_id=site_id,
        findings=[
            MissingReportFindingResponse(
                employee_id=finding.employee_id,
                employee_name=finding.employee_name,
                employee_name_en=finding.employee_name_en,
                work_date=finding.work_date,
                site_id=finding.site_id,
                site_name=finding.site_name,
                kind=finding.kind,
            )
            for finding in findings
        ],
        total=len(findings),
    )
