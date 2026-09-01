"""Site request and response schemas (Requirement 6, 7).

Locale-neutral like every other schema: no message text, only values the front end formats. Money is
`Decimal`, so it round-trips as a string with two places rather than as a float that loses agorot.

The billing-rate fields — `billing_rate`, `overtime_billing_rate` on a rate row, and the whole
`site_rates` history — are named to match `app.core.authz.BILLING_FIELDS`. That is what lets the
caller's `redact` strip them for a site manager (Requirement 2.5, 17.7) without this schema knowing
who is reading. Removal rather than nulling: a rate of `null` would be indistinguishable from an
unset one, so redaction removes the key entirely and a site manager's response has no billing key at
all.

There are no coordinate, radius or location fields anywhere here, by requirement (6.8). `extra="forbid"`
on every input model means a request that carries a `latitude` or `longitude` is rejected as an
unknown field rather than quietly ignored.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.site import AssignmentMode, QrMode, SiteStatus

_MANDATORY_TEXT = Field(min_length=1, max_length=200)


def _reject_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


# --------------------------------------------------------------------------- rates


class SiteRateInput(BaseModel):
    """One billing row a client submits when replacing a site's rate history.

    `overtime_billing_rate` is optional: leaving it unset means overtime bills at the standard rate
    (Requirement 17.2). `effective_to` is optional and open-ended; the last row of a history usually
    leaves it unset, meaning "still in force". The service checks the rows do not overlap and closes
    them into a clean chain; a client sends the rows it wants and the service reconciles them.
    """

    model_config = ConfigDict(extra="forbid")

    billing_rate: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    overtime_billing_rate: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    effective_from: date
    effective_to: date | None = None


class SiteRatesUpdate(BaseModel):
    """The body of `PUT /sites/{id}/rates`: the whole intended history, at least one row."""

    model_config = ConfigDict(extra="forbid")

    rates: list[SiteRateInput] = Field(min_length=1)


class SiteRateResponse(BaseModel):
    """One billing row as it is served back. Carries the id so a client can reference a specific row.

    Named to match `BILLING_FIELDS`, so the whole row is stripped for a site manager along with the
    `site_rates` list it sits in.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    billing_rate: Decimal
    overtime_billing_rate: Decimal | None
    effective_from: date
    effective_to: date | None


# --------------------------------------------------------------------------- site create / update


class SiteCreate(BaseModel):
    """Create a site (Requirement 6.1–6.4).

    The initial billing rate is part of creation rather than a second call, because a site with no
    rate cannot be billed and every real create knows the rate. Passing `rate=None` is allowed for a
    site captured before its rate is agreed; the rate can be set later through the rates endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    # Mandatory (Requirement 6.1).
    name: str = _MANDATORY_TEXT
    site_number: str = Field(min_length=1, max_length=100)
    client_id: uuid.UUID

    # Optional detail (Requirement 6.1).
    address: str | None = Field(default=None, max_length=2000)
    manager_user_id: uuid.UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)

    #: Status defaults to active (Requirement 6.2).
    status: SiteStatus = SiteStatus.ACTIVE
    #: QR and assignment modes default to the requirement's defaults (8.2, 7.3).
    qr_mode: QrMode = QrMode.UNIFIED
    assignment_mode: AssignmentMode = AssignmentMode.OPEN

    #: The opening billing row. Optional so a site can exist before its rate is agreed.
    rate: SiteRateInput | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        return _reject_blank(value)

    @field_validator("site_number")
    @classmethod
    def _site_number_not_blank(cls, value: str) -> str:
        return _reject_blank(value)


class SiteUpdate(BaseModel):
    """Patch a site. Every field optional; a field left out is left unchanged.

    The billing rates are not here: they move through `PUT /sites/{id}/rates`, which owns the
    effective-dated history. A mandatory field present in the body must still be non-blank — clearing
    the name or number is a validation error, not a way to erase it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    site_number: str | None = Field(default=None, min_length=1, max_length=100)
    client_id: uuid.UUID | None = None
    address: str | None = Field(default=None, max_length=2000)
    manager_user_id: uuid.UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: SiteStatus | None = None
    qr_mode: QrMode | None = None
    assignment_mode: AssignmentMode | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("name", "site_number")
    @classmethod
    def _not_blank_when_present(cls, value: str | None) -> str | None:
        return None if value is None else _reject_blank(value)


# --------------------------------------------------------------------------- assignment


class SiteEmployeesUpdate(BaseModel):
    """The body of `PUT /sites/{id}/employees`: the set of employees expected at this site.

    The whole set is replaced, not appended to: a client that owns the assignment screen states the
    intended membership, and reconciling row-by-row would leave a stale row behind on any edit that
    removed one.
    """

    model_config = ConfigDict(extra="forbid")

    employee_ids: list[uuid.UUID] = Field(default_factory=list)


class EmployeeSitesUpdate(BaseModel):
    """The body of `PUT /employees/{id}/sites`: the set of sites an employee is expected at.

    The mirror of `SiteEmployeesUpdate`, maintaining the same many-to-many table from the other side.
    """

    model_config = ConfigDict(extra="forbid")

    site_ids: list[uuid.UUID] = Field(default_factory=list)


# --------------------------------------------------------------------------- responses


class SiteResponse(BaseModel):
    """A site card as served.

    The `site_rates` list and the flattened current billing fields are named to match
    `BILLING_FIELDS`, so a site manager's response has them removed by redaction rather than nulled.
    Everything else — name, number, status, QR mode, assignment mode, the assigned employees — a
    manager may see; a manager reads the sites they run, just not what the client is billed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    site_number: str
    client_id: uuid.UUID
    manager_user_id: uuid.UUID | None
    address: str | None
    start_date: date | None
    end_date: date | None
    status: SiteStatus
    qr_mode: QrMode
    assignment_mode: AssignmentMode
    notes: str | None
    created_at: datetime
    updated_at: datetime

    #: The ids of the employees assigned to this site (Requirement 7.1). Not a billing field, so
    #: every reader sees it.
    employee_ids: list[uuid.UUID] = Field(default_factory=list)

    #: The billing rate in force today, flattened onto the card for convenience. Same names as
    #: `BILLING_FIELDS`, so a site manager sees neither.
    billing_rate: Decimal | None = None
    overtime_billing_rate: Decimal | None = None

    #: Full billing history, oldest first. Redacted away wholesale for a site manager.
    site_rates: list[SiteRateResponse] = Field(default_factory=list)


class SiteListItem(BaseModel):
    """A row in the site list. No billing fields — a list is a directory, and the card holds the
    detail. A site manager sees this list scoped to their sites."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    site_number: str
    client_id: uuid.UUID
    status: SiteStatus


class SiteListResponse(BaseModel):
    """A page of sites with the total, so a client can render pagination controls."""

    items: list[SiteListItem]
    total: int
    limit: int
    offset: int


class EmployeeSitesResponse(BaseModel):
    """The sites an employee is assigned to (Requirement 7.1), as served by
    `PUT /employees/{id}/sites` and readable by the same."""

    employee_id: uuid.UUID
    site_ids: list[uuid.UUID]
