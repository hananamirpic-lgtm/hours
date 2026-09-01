"""Payroll schemas (Requirement 16).

Locale-neutral like every other schema: no message text, only the values the front end formats. These
carry the bodies and responses the payroll endpoints exchange:

* `POST /api/payroll/calculate` computes an employee's month, replacing any existing draft
  (Requirement 16.10), and returns the resulting record.
* `GET /api/payroll` lists the records for a period (Requirement 16.6).
* `GET /api/payroll/{employee_id}/{year}/{month}` returns one record with its per-site allocation
  (Requirement 16.8).

The whole payload is wage data, visible only to administrators and accounting (Requirement 2.5, 2.6);
the router enforces that with a finance-role guard. Money is `Decimal` end to end so the two-place,
`ROUND_HALF_UP` figures the calculation produced are never coerced through a float (Requirement 16.7).
Minutes are integers, carried alongside each pay so the front end can show both the hours and the
amount without re-deriving one from the other.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.payroll import CalculationStatus


class PayrollCalculateRequest(BaseModel):
    """The body of `POST /api/payroll/calculate` (Requirement 16.5, 16.10).

    Names the employee and month to compute. `bonuses` and `deductions` are the month's adjustments
    (Requirement 16.5), defaulting to nothing. `travel` may override the allowance the service would
    otherwise derive from the daily travel rate over the days worked; omit it to let the service
    compute it, which honours a mid-month rate change. `extra="forbid"` refuses an unknown field so a
    stray value is a validation error rather than a silent no-op.
    """

    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    year: int = Field(ge=2000, le=2200)
    month: int = Field(ge=1, le=12)
    travel: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    bonuses: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    deductions: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)


class SiteAllocationResponse(BaseModel):
    """One site's share of the employee's monthly cost (Requirement 16.8).

    The minutes are the month's minutes worked at this site per bucket; `cost` is the corrected figure
    that, summed across a record's allocations, equals the record's worked pay to the agora.
    """

    model_config = ConfigDict(from_attributes=True)

    site_id: uuid.UUID
    regular_minutes: int
    overtime_minutes: int
    shabbat_minutes: int
    holiday_minutes: int
    cost: Decimal


class PayrollRecordResponse(BaseModel):
    """One employee's monthly payroll record with its per-site allocation (Requirement 16.6, 16.8).

    The four bucket minute totals and their pay, the allowances and deductions, the total, the status
    (`draft` while the month is open, `final` once locked), and when it was calculated. `allocations`
    is the per-site cost breakdown whose costs sum to the worked pay (the four bucket pays) exactly.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    year: int
    month: int

    regular_minutes: int
    overtime_minutes: int
    shabbat_minutes: int
    holiday_minutes: int

    regular_pay: Decimal
    overtime_pay: Decimal
    shabbat_pay: Decimal
    holiday_pay: Decimal

    travel: Decimal
    bonuses: Decimal
    deductions: Decimal
    total_pay: Decimal

    status: CalculationStatus
    calculated_at: datetime | None = None

    allocations: list[SiteAllocationResponse] = Field(default_factory=list)


class PayrollListResponse(BaseModel):
    """A page of payroll records for a period (Requirement 16.6)."""

    items: list[PayrollRecordResponse]
    total: int
    limit: int
    offset: int
