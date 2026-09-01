"""Billing schemas (Requirement 17).

Locale-neutral like every other schema: no message text, only the values the front end formats. These
carry the bodies and responses the billing endpoints exchange:

* `POST /api/billing/calculate` computes a month's per-site billing, replacing any existing draft, and
  returns the per-site, per-client and total figures with the count of excluded unapproved hours
  (Requirement 17.6).
* `GET /api/billing` returns the same summary for a period from the stored records (Requirement 17.3).

The whole payload is billing data, visible only to administrators and accounting (Requirement 2.5,
17.7); the router enforces that with a finance-role guard. Money is `Decimal` end to end so the
two-place, `ROUND_HALF_UP` figures the calculation produced are never coerced through a float
(Requirement 17.1). Minutes are integers, carried alongside each amount so the front end can show both
the hours and the amount without re-deriving one from the other. `excluded_entry_count` and
`excluded_minutes` report the unapproved hours a low billing figure might otherwise hide (17.6).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.payroll import CalculationStatus


class BillingCalculateRequest(BaseModel):
    """The body of `POST /api/billing/calculate` (Requirement 17.1, 17.3).

    Names the month to bill. Billing is computed for every site with billable, Approved or Locked
    hours in the month; there is no per-site or per-client selector, because the summary is the whole
    period's billing and the aggregation is what makes it useful. `extra="forbid"` refuses an unknown
    field so a stray value is a validation error rather than a silent no-op.
    """

    model_config = ConfigDict(extra="forbid")

    year: int = Field(ge=2000, le=2200)
    month: int = Field(ge=1, le=12)


class SiteBillingResponse(BaseModel):
    """One site's billing, cost and profit for a month (Requirement 17.1, 17.4).

    The minutes are the month's billable minutes at the site; `billing` is the summed billing;
    `cost` is the cost payroll allocated to the site; `profit` is `billing − cost`. The two applied
    rates are the ones the record stored for the invoice (Requirement 17.2).
    """

    model_config = ConfigDict(from_attributes=True)

    site_id: uuid.UUID
    client_id: uuid.UUID
    regular_minutes: int
    overtime_minutes: int
    billing_rate_applied: Decimal
    overtime_rate_applied: Decimal | None = None
    billing: Decimal
    #: Cost and profit are computed live at calculation time (Requirement 17.4). The stored read leaves
    #: them null, because `billing_records` stores the billed amount but not the cost it was compared
    #: against; the calculate response always carries them.
    cost: Decimal | None = None
    profit: Decimal | None = None
    status: CalculationStatus
    calculated_at: datetime | None = None


class ClientBillingResponse(BaseModel):
    """One client's billing, cost and profit for a month, summed over its sites (Requirement 17.3)."""

    client_id: uuid.UUID
    billing: Decimal
    cost: Decimal
    profit: Decimal


class BillingSummaryResponse(BaseModel):
    """A month's billing: per-site and per-client figures, totals, and the excluded-hours notice.

    `sites` is one row per site with billable hours; `clients` aggregates them per client (Requirement
    17.3); the `total_*` are the grand totals (Requirement 17.4). `excluded_entry_count` and
    `excluded_minutes` state the count and hours of unapproved entries left out, so a low billing
    figure is never mistaken for a low month (Requirement 17.6).
    """

    year: int
    month: int
    sites: list[SiteBillingResponse] = Field(default_factory=list)
    clients: list[ClientBillingResponse] = Field(default_factory=list)
    total_billing: Decimal
    total_cost: Decimal
    total_profit: Decimal
    excluded_entry_count: int = 0
    excluded_minutes: int = 0


# --------------------------------------------------------------------------- payment request (18.8)


class PaymentRequestLineResponse(BaseModel):
    """One (site, employee) line of a payment request (Requirement 18.8).

    The employee, the hours worked at the site in the period, the site rate applied on the invoice, and
    this employee's share of the site's billed amount. `hours` is the total minutes expressed in hours
    for display; `rate` is the site's standard billing rate. The share of overtime billing is folded
    into `amount`, which is a split of the site's authoritative billed total so the lines sum to it.
    """

    model_config = ConfigDict(from_attributes=True)

    employee_id: uuid.UUID
    employee_name: str
    employee_name_en: str
    regular_minutes: int
    overtime_minutes: int
    total_minutes: int
    hours: Decimal
    rate: Decimal
    amount: Decimal


class PaymentRequestSiteResponse(BaseModel):
    """One site's section of a payment request: its employee lines and the site's billed total (18.8).

    `amount` is the site's stored `billing_records.total_amount`; the `lines` amounts sum to it exactly
    (Requirement 17.3). The applied rates are the ones the invoice was cut at (Requirement 17.2).
    """

    model_config = ConfigDict(from_attributes=True)

    site_id: uuid.UUID
    site_name: str
    site_number: str
    billing_rate_applied: Decimal
    overtime_rate_applied: Decimal | None = None
    lines: list[PaymentRequestLineResponse] = Field(default_factory=list)
    amount: Decimal


class PaymentRequestResponse(BaseModel):
    """A client's payment request for a period (Requirement 18.8).

    Carries the client and the period, one section per billed site with per-employee hours, rate and
    amount, and the client total. The total is the sum of the site amounts — the stored
    `billing_records.total_amount` — so it reconciles with the billing records for the same period
    (Requirement 17.3). Billing data, so administrators and accounting only (Requirement 17.7); the
    router enforces that with the finance guard.
    """

    model_config = ConfigDict(from_attributes=True)

    client_id: uuid.UUID
    client_name: str
    year: int
    month: int
    sites: list[PaymentRequestSiteResponse] = Field(default_factory=list)
    total_amount: Decimal
