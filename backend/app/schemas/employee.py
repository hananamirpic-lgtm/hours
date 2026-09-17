"""Employee request and response schemas (Requirement 3).

Locale-neutral like every other schema: no message text, only values the front end formats. Money is
`Decimal`, so it round-trips as a string with two places rather than as a float that loses agorot.

The response deliberately names its wage fields `hourly_wage`, `overtime_rate`,
`shabbat_holiday_rate`, `travel_allowance_daily` and nests the history under `rates` — the exact
names in `app.core.authz.WAGE_FIELDS`. That is what lets the caller's `redact` strip them for a site
manager (Requirement 2.5) without this schema knowing who is reading. A wage that is merely set to
`null` would be indistinguishable from an unset one; redaction removes the key entirely, so the
response served to a site manager has no wage key at all.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.employee import EmployeeStatus

#: Mandatory strings must not be blank or whitespace (Requirement 3.5). A single validator applied to
#: each keeps the rule in one place; a length-1 minimum alone would let a lone space through.
_MANDATORY_TEXT = Field(min_length=1, max_length=200)
_OPTIONAL_TEXT = Field(default=None, max_length=2000)


def _reject_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


class EmployeeRateInput(BaseModel):
    """One pay row a client submits when replacing an employee's rate history.

    `effective_to` is optional and open-ended: the last row of a history usually leaves it unset,
    meaning "still in force". The service is what checks the rows do not overlap and closes them into
    a clean chain; a client sends the rows it wants and the service reconciles them.
    """

    model_config = ConfigDict(extra="forbid")

    hourly_wage: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    overtime_rate: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    shabbat_holiday_rate: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    travel_allowance_daily: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    effective_from: date
    effective_to: date | None = None


class EmployeeRatesUpdate(BaseModel):
    """The body of `PUT /employees/{id}/rates`: the whole intended history, at least one row."""

    model_config = ConfigDict(extra="forbid")

    rates: list[EmployeeRateInput] = Field(min_length=1)


class EmployeeRateResponse(BaseModel):
    """One pay row as it is served back. Carries the id so a client can reference a specific row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hourly_wage: Decimal
    overtime_rate: Decimal
    shabbat_holiday_rate: Decimal
    travel_allowance_daily: Decimal
    effective_from: date
    effective_to: date | None


class EmployeeCreate(BaseModel):
    """Create an employee (Requirement 3.1–3.3).

    Only `full_name`, `full_name_en`, `passport_number`, `phone` and `start_date` are mandatory
    (Requirements 1.1–1.4). `country`, `emergency_contact_name` and `emergency_contact_phone` are
    optional: omitting them is accepted and carried through as null, but a blank/whitespace value is
    still rejected when one is supplied.

    The initial rate is part of creation rather than a second call, because an employee with no rate
    cannot be paid and every real create knows the wage. Passing `rate=None` is allowed for the rare
    case of a record captured before its pay is agreed; the rate can be set later through the rates
    endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    # Mandatory (Requirement 1.1): identity plus start_date below.
    full_name: str = _MANDATORY_TEXT
    full_name_en: str = _MANDATORY_TEXT
    passport_number: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=1, max_length=50)

    #: The staffing company that supplies this employee. Mandatory on create (Requirement 2.1–2.3):
    #: required, no default, so a missing value is a 422 naming the field. The DB column is nullable
    #: (migration 0007), so pre-existing rows created before this feature stay valid.
    staffing_company_id: uuid.UUID

    # Optional (Requirements 1.1–1.4): omission is allowed and carried through as null; when a value
    # is supplied it must not be blank (validated below).
    country: str | None = Field(default=None, max_length=200)
    emergency_contact_name: str | None = Field(default=None, max_length=200)
    emergency_contact_phone: str | None = Field(default=None, max_length=50)

    # Employment (Requirement 3.3). `start_date` is mandatory; `position` is not.
    start_date: date
    position: str | None = _OPTIONAL_TEXT

    # Optional (Requirement 3.2).
    date_of_birth: date | None = None
    address: str | None = _OPTIONAL_TEXT
    notes: str | None = _OPTIONAL_TEXT

    #: Object-storage key of an already-uploaded photo. Optional at create; upload flow lands in the
    #: documents task. The photo requirement of 3.1 is enforced there, not by rejecting a create.
    photo_key: str | None = Field(default=None, max_length=500)

    #: Status defaults to active; a create may set On Leave or Inactive but not Terminated, which is
    #: reached only by a status transition on an existing record.
    status: EmployeeStatus = EmployeeStatus.ACTIVE

    #: The opening pay row. Optional so a record can exist before pay is agreed.
    rate: EmployeeRateInput | None = None

    @field_validator("full_name", "full_name_en")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _reject_blank(value)

    @field_validator("passport_number", "phone")
    @classmethod
    def _not_blank_identifier(cls, value: str) -> str:
        return _reject_blank(value)

    @field_validator("country", "emergency_contact_name", "emergency_contact_phone")
    @classmethod
    def _not_blank_when_present(cls, value: str | None) -> str | None:
        return None if value is None else _reject_blank(value)

    @field_validator("status")
    @classmethod
    def _not_terminated_on_create(cls, value: EmployeeStatus) -> EmployeeStatus:
        if value is EmployeeStatus.TERMINATED:
            raise ValueError("cannot create an employee in the terminated state")
        return value


class EmployeeUpdate(BaseModel):
    """Patch an employee. Every field optional; a field left out is left unchanged.

    Status is not here: it moves through `PATCH /employees/{id}/status`, which is where the transition
    rules live. A mandatory field present in the body must still be non-blank — clearing a mandatory
    field is a validation error, not a way to erase it (Requirement 3.5).
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    full_name_en: str | None = Field(default=None, min_length=1, max_length=200)
    passport_number: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, min_length=1, max_length=50)
    staffing_company_id: uuid.UUID | None = None
    country: str | None = Field(default=None, min_length=1, max_length=200)
    emergency_contact_name: str | None = Field(default=None, min_length=1, max_length=200)
    emergency_contact_phone: str | None = Field(default=None, min_length=1, max_length=50)
    start_date: date | None = None
    position: str | None = Field(default=None, max_length=2000)
    date_of_birth: date | None = None
    address: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=2000)
    photo_key: str | None = Field(default=None, max_length=500)

    @field_validator(
        "full_name",
        "full_name_en",
        "country",
        "emergency_contact_name",
        "passport_number",
        "phone",
        "emergency_contact_phone",
    )
    @classmethod
    def _not_blank_when_present(cls, value: str | None) -> str | None:
        return None if value is None else _reject_blank(value)


class EmployeeStatusUpdate(BaseModel):
    """Move an employee to a new status (Requirement 3.4, 3.8).

    A reason is optional but recorded: a termination or a leave is exactly the change an audit reader
    later asks "why" of, and the field is where that answer goes.
    """

    model_config = ConfigDict(extra="forbid")

    status: EmployeeStatus
    reason: str | None = Field(default=None, max_length=500)


class EmployeeResponse(BaseModel):
    """An employee card as served.

    The wage fields and the `rates` list are named to match `WAGE_FIELDS`, so a site manager's
    response has them removed by redaction rather than nulled. The sensitive personal fields
    (passport, phone, address, date of birth) are returned in plaintext to an authorized reader — the
    encryption is at rest, and a finance or admin caller reading a card is entitled to see them.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    full_name_en: str
    #: The auto-generated 4-digit login number, server-assigned and read-only (Requirement 2.3, 6.1).
    #: Not a wage field, so every reader sees it.
    employee_number: str | None = None
    photo_key: str | None
    passport_number: str
    phone: str
    #: The current staffing-company link. Nullable so pre-existing employees serialize (Requirement
    #: 2.9–2.11). Not a wage field, so every reader sees it.
    staffing_company_id: uuid.UUID | None = None
    #: Nullable since migration 0006 made the column nullable, so an employee lacking one serializes.
    country: str | None
    date_of_birth: date | None
    address: str | None
    emergency_contact_name: str | None
    emergency_contact_phone: str | None
    notes: str | None
    start_date: date
    position: str | None
    status: EmployeeStatus
    created_at: datetime
    updated_at: datetime

    #: True when the employee holds a document past its expiry (Requirement 4.5). Derived, not
    #: stored: the documents service computes it against today so the card can surface an expired
    #: permit without loading the documents themselves. Not a wage field, so every reader sees it.
    has_expired_document: bool = False

    #: The pay row in force today, flattened onto the card for convenience. Same names as
    #: `WAGE_FIELDS`, so a site manager sees none of them.
    hourly_wage: Decimal | None = None
    overtime_rate: Decimal | None = None
    shabbat_holiday_rate: Decimal | None = None
    travel_allowance_daily: Decimal | None = None

    #: Full history, oldest first. Redacted away wholesale for a site manager.
    rates: list[EmployeeRateResponse] = Field(default_factory=list)


class EmployeeListItem(BaseModel):
    """A row in the employee list. No sensitive personal fields and no wage — a list is a directory,
    and the card is where the detail lives."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    full_name_en: str
    #: The auto-generated 4-digit login number, server-assigned and read-only (Requirement 2.3, 6.1).
    employee_number: str | None = None
    staffing_company_id: uuid.UUID | None = None
    country: str | None
    position: str | None
    status: EmployeeStatus
    start_date: date


class EmployeeListResponse(BaseModel):
    """A page of employees with the total, so a client can render pagination controls."""

    items: list[EmployeeListItem]
    total: int
    limit: int
    offset: int
