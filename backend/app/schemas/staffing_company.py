"""Staffing-company request and response schemas (Requirement 1).

Locale-neutral like every other schema: no message text, only values the front end formats. Money is
`Decimal`, so the optional flat `hourly_rate` round-trips as a string with two places rather than as a
float that loses agorot.

`name` and `contact_person` are mandatory and must not be blank; `_reject_blank` keeps that rule in one
place, since a length-1 minimum alone would let a lone space through. Everything else — the rate, the
telephone, and the free-text comments — is optional and simply carried through as null when omitted.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Mandatory strings must not be blank or whitespace (Requirement 1.4, 1.6). A single validator applied
#: to each keeps the rule in one place; a length-1 minimum alone would let a lone space through.
_MANDATORY_TEXT = Field(min_length=1, max_length=200)


def _reject_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


class StaffingCompanyCreate(BaseModel):
    """Create a staffing company (Requirement 1.3–1.8).

    `name` and `contact_person` are mandatory and non-blank; `hourly_rate`, `telephone`, and
    `comments` are optional. Omitting an optional is accepted and carried through as null.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = _MANDATORY_TEXT
    contact_person: str = _MANDATORY_TEXT
    hourly_rate: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    telephone: str | None = Field(default=None, max_length=100)
    comments: str | None = Field(default=None, max_length=2000)

    @field_validator("name", "contact_person")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _reject_blank(value)


class StaffingCompanyUpdate(BaseModel):
    """Patch a staffing company. Every field optional; a field left out is left unchanged.

    A mandatory field present in the body must still be non-blank — clearing `name` or
    `contact_person` is a validation error, not a way to erase it (Requirement 1.11).
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    contact_person: str | None = Field(default=None, min_length=1, max_length=200)
    hourly_rate: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    telephone: str | None = Field(default=None, max_length=100)
    comments: str | None = Field(default=None, max_length=2000)

    @field_validator("name", "contact_person")
    @classmethod
    def _not_blank_when_present(cls, value: str | None) -> str | None:
        return None if value is None else _reject_blank(value)


class StaffingCompanyResponse(BaseModel):
    """A staffing-company card as served, carrying every stored field including timestamps."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    contact_person: str
    hourly_rate: Decimal | None
    telephone: str | None
    comments: str | None
    created_at: datetime
    updated_at: datetime


class StaffingCompanyListItem(BaseModel):
    """A row in the staffing-company list. No comments or telephone — a list is a directory, and the
    card is where the detail lives."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    contact_person: str
    hourly_rate: Decimal | None


class StaffingCompanyListResponse(BaseModel):
    """A page of staffing companies with the total, so a client can render pagination controls."""

    items: list[StaffingCompanyListItem]
    total: int
    limit: int
    offset: int
