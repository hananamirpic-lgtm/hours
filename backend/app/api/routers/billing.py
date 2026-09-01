"""Billing router — calculate and read monthly billing and profit (Requirement 17).

HTTP only: guard, validate the body or path, call the billing service, map a refusal onto a status
code and the error envelope, and shape the response. The service owns every rule and never commits, so
`POST /api/billing/calculate` commits here — the records it upserts ride along in one transaction
(Requirement 13.2), the pattern the payroll and period routers follow.

Billing is billing data, so every endpoint here is behind `FinanceCaller`, which admits
administrators and accounting only (Requirement 2.5, 17.7). A site manager never receives billing
rates or amounts (Requirement 17.7) and so has no business on any of these endpoints; the employee
role is likewise absent. Nothing is redacted below because the caller is already a finance role — the
guard, not a per-field strip, is what keeps billing figures from the wrong reader here.

* `POST /api/billing/calculate` — compute a month's per-site billing and profit from its Approved or
  Locked entries (Requirement 17.6), replacing any existing drafts, and report the excluded
  unapproved hours (Requirement 17.6).
* `GET /api/billing` — the stored billing for a period, per site and per client (Requirement 17.3).
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import DbSession, FinanceCaller, api_error
from app.schemas.billing import (
    BillingCalculateRequest,
    BillingSummaryResponse,
    ClientBillingResponse,
    PaymentRequestLineResponse,
    PaymentRequestResponse,
    PaymentRequestSiteResponse,
    SiteBillingResponse,
)
from app.services import billing as billing_service
from app.services import client as client_service

router = APIRouter(prefix="/billing", tags=["billing"])

#: Which HTTP status each billing error maps onto. A gap in a site's billing-rate history is a bad
#: request — the request is well-formed but the data it needs is incomplete. An unknown client on the
#: payment-request path is a not-found.
_ERROR_STATUS: dict[str, HTTPStatus] = {
    billing_service.MissingSiteRate.code: HTTPStatus.BAD_REQUEST,
    client_service.ClientNotFound.code: HTTPStatus.NOT_FOUND,
}


def _raise_for(error: billing_service.BillingError) -> HTTPException:
    status = _ERROR_STATUS.get(getattr(error, "code", ""), HTTPStatus.BAD_REQUEST)
    return api_error(status, getattr(error, "code", "billing_error"))


# --------------------------------------------------------------------------- calculate


@router.post(
    "/calculate",
    response_model=BillingSummaryResponse,
    summary="Calculate a month's billing and profit",
    description=(
        "Compute per-site billing for a month from its Approved or Locked entries, pricing each "
        "site's hours at the site rate in force on the work date and its overtime at the site "
        "overtime rate where configured (Requirement 17.1, 17.2). Profit is billing minus the cost "
        "payroll allocated to the site (Requirement 17.4); figures aggregate per client and in total "
        "(Requirement 17.3). The response reports the count and hours of unapproved entries excluded "
        "(Requirement 17.6). Administrators and accounting only (Requirement 17.7). Recalculating "
        "replaces the existing drafts; records for a locked month are final."
    ),
    responses={
        HTTPStatus.OK: {"model": BillingSummaryResponse, "description": "The calculated summary"},
        HTTPStatus.BAD_REQUEST: {"description": "A billable day has no site rate in force"},
    },
)
def calculate_billing(
    payload: BillingCalculateRequest,
    caller: FinanceCaller,  # noqa: ARG001 — the type is the guard; the caller is unused past it
    session: DbSession,
) -> BillingSummaryResponse:
    try:
        result = billing_service.calculate_billing(session, year=payload.year, month=payload.month)
        session.commit()
    except billing_service.BillingError as error:
        session.rollback()
        raise _raise_for(error) from error

    computation = result.computation
    sites = [
        SiteBillingResponse(
            site_id=site.site_id,
            client_id=site.client_id,
            regular_minutes=site.regular_minutes,
            overtime_minutes=site.overtime_minutes,
            billing_rate_applied=site.billing_rate_applied,
            overtime_rate_applied=site.overtime_rate_applied,
            billing=site.billing,
            cost=site.cost,
            profit=site.profit,
            status=record.status,
            calculated_at=record.calculated_at,
        )
        for site, record in zip(computation.sites, result.records, strict=True)
    ]
    clients = [
        ClientBillingResponse(
            client_id=client.client_id,
            billing=client.billing,
            cost=client.cost,
            profit=client.profit,
        )
        for client in computation.clients
    ]
    return BillingSummaryResponse(
        year=payload.year,
        month=payload.month,
        sites=sites,
        clients=clients,
        total_billing=computation.total_billing,
        total_cost=computation.total_cost,
        total_profit=computation.total_profit,
        excluded_entry_count=result.excluded.entry_count,
        excluded_minutes=result.excluded.minutes,
    )


# --------------------------------------------------------------------------- read


@router.get(
    "",
    response_model=BillingSummaryResponse,
    summary="Read a period's stored billing",
    description=(
        "The stored billing records for a period, per site and aggregated per client (Requirement "
        "17.3). Administrators and accounting only (Requirement 17.7). The excluded-hours notice "
        "reflects the current unapproved hours in the period (Requirement 17.6). Cost and profit are "
        "computed at calculation time; the stored read reports the billed amounts."
    ),
)
def read_billing(
    caller: FinanceCaller,  # noqa: ARG001 — the type is the guard
    session: DbSession,
    year: Annotated[int, Query(ge=2000, le=2200)],
    month: Annotated[int, Query(ge=1, le=12)],
) -> BillingSummaryResponse:
    records = billing_service.list_records(session, year=year, month=month)
    clients, total_billing, total_cost, total_profit = billing_service.summarise_records(records)
    excluded = billing_service.excluded_hours(session, year=year, month=month)

    sites = [
        SiteBillingResponse(
            site_id=record.site_id,
            client_id=record.client_id,
            regular_minutes=record.regular_minutes,
            overtime_minutes=record.overtime_minutes,
            billing_rate_applied=record.billing_rate_applied,
            overtime_rate_applied=record.overtime_rate_applied,
            billing=record.total_amount,
            cost=None,
            profit=None,
            status=record.status,
            calculated_at=record.calculated_at,
        )
        for record in records
    ]
    return BillingSummaryResponse(
        year=year,
        month=month,
        sites=sites,
        clients=[
            ClientBillingResponse(
                client_id=client.client_id,
                billing=client.billing,
                cost=client.cost,
                profit=client.profit,
            )
            for client in clients
        ],
        total_billing=total_billing,
        total_cost=total_cost,
        total_profit=total_profit,
        excluded_entry_count=excluded.entry_count,
        excluded_minutes=excluded.minutes,
    )


# --------------------------------------------------------------------------- payment request (18.8)


@router.get(
    "/clients/{client_id}/payment-request",
    response_model=PaymentRequestResponse,
    summary="A client's payment request for a period",
    description=(
        "The per-client payment request for a period: the client, the period, and one section per "
        "billed site with the employees who worked it, their hours, the site rate and the amount, "
        "plus the client total (Requirement 18.8). It is derived from the stored billing records, so "
        "each site's amount is that site's billed total and the client total reconciles with the "
        "billing records for the same period (Requirement 17.3). Administrators and accounting only "
        "(Requirement 17.7). Figures are in ILS."
    ),
    responses={
        HTTPStatus.OK: {"model": PaymentRequestResponse, "description": "The payment request"},
        HTTPStatus.NOT_FOUND: {"description": "No client with that id"},
    },
)
def client_payment_request(
    client_id: uuid.UUID,
    caller: FinanceCaller,  # noqa: ARG001 — the type is the guard
    session: DbSession,
    year: Annotated[int, Query(ge=2000, le=2200)],
    month: Annotated[int, Query(ge=1, le=12)],
) -> PaymentRequestResponse:
    try:
        request = billing_service.payment_request(session, client_id=client_id, year=year, month=month)
    except client_service.ClientNotFound as error:
        raise api_error(_ERROR_STATUS.get(error.code, HTTPStatus.NOT_FOUND), error.code) from error

    return PaymentRequestResponse(
        client_id=request.client_id,
        client_name=request.client_name,
        year=request.year,
        month=request.month,
        sites=[
            PaymentRequestSiteResponse(
                site_id=site.site_id,
                site_name=site.site_name,
                site_number=site.site_number,
                billing_rate_applied=site.billing_rate_applied,
                overtime_rate_applied=site.overtime_rate_applied,
                lines=[
                    PaymentRequestLineResponse(
                        employee_id=line.employee_id,
                        employee_name=line.employee_name,
                        employee_name_en=line.employee_name_en,
                        regular_minutes=line.regular_minutes,
                        overtime_minutes=line.overtime_minutes,
                        total_minutes=line.total_minutes,
                        hours=line.hours,
                        rate=line.rate,
                        amount=line.amount,
                    )
                    for line in site.lines
                ],
                amount=site.amount,
            )
            for site in request.sites
        ],
        total_amount=request.total_amount,
    )
