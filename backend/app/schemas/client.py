"""Client request and response schemas (Requirement 5).

Locale-neutral like every other schema: no message text, only values the front end formats. A client
is a business rather than a person, so nothing here is redacted or encrypted — the fields are ordinary
commercial detail.

Two rules the requirement pins to validation live here:

* **Email format (Requirement 5.5).** Checked with a deliberately small regex rather than a full
  RFC 5322 parser or the `email-validator` package, which is not a dependency. The aim is to catch a
  typo — a missing `@`, a missing domain — not to prove deliverability, which only sending can. An
  empty submission is normalised to `None`, because a client legitimately may have no email and a
  blank string is not an address.
* **Payment terms (Requirement 5.5).** An integer number of days plus optional free text, so
  "30 days" stays computable while "net on delivery" is still recordable. The days are non-negative;
  the note is just text.

Company name is the one mandatory field (Requirement 5.2); everything else is optional, and an
optional string sent blank is treated as absent rather than stored as an empty value that a reader
cannot tell from a real one.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Small on purpose: a non-empty local part, an `@`, a domain with at least one dot and a short tail.
#: It rejects the common typo and accepts ordinary addresses; it is not a deliverability check.
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _blank_to_none(value: str | None) -> str | None:
    """Normalise an optional string: strip it, and treat an empty result as absent."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _validate_email(value: str | None) -> str | None:
    normalised = _blank_to_none(value)
    if normalised is not None and not _EMAIL_PATTERN.match(normalised):
        raise ValueError("must be a valid email address")
    return normalised


class ClientCreate(BaseModel):
    """Create a client (Requirement 5.1). Only `name` is mandatory."""

    model_config = ConfigDict(extra="forbid")

    #: Mandatory (Requirement 5.2). Non-blank: a whitespace-only name is rejected.
    name: str = Field(min_length=1, max_length=200)

    company: str | None = Field(default=None, max_length=200)
    company_number: str | None = Field(default=None, max_length=100)
    contact_person: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=320)
    address: str | None = Field(default=None, max_length=2000)

    #: Days plus free text (Requirement 5.5). Non-negative days.
    payment_terms_days: int | None = Field(default=None, ge=0)
    payment_terms_notes: str | None = Field(default=None, max_length=2000)

    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator(
        "company",
        "company_number",
        "contact_person",
        "phone",
        "address",
        "payment_terms_notes",
        "notes",
    )
    @classmethod
    def _optional_blank_to_none(cls, value: str | None) -> str | None:
        return _blank_to_none(value)

    @field_validator("email")
    @classmethod
    def _email_format(cls, value: str | None) -> str | None:
        return _validate_email(value)


class ClientUpdate(BaseModel):
    """Patch a client. Every field optional; a field left out is left unchanged.

    A mandatory field present in the body must still be non-blank — clearing `name` is a validation
    error, not a way to erase it. An optional field can be cleared by sending an empty string, which
    normalises to `None`. `is_archived` is not here: archival is reached through the delete endpoint,
    which applies Requirement 5.4's rule, not by a plain field edit.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    company_number: str | None = Field(default=None, max_length=100)
    contact_person: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=320)
    address: str | None = Field(default=None, max_length=2000)
    payment_terms_days: int | None = Field(default=None, ge=0)
    payment_terms_notes: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def _name_not_blank_when_present(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator(
        "company",
        "company_number",
        "contact_person",
        "phone",
        "address",
        "payment_terms_notes",
        "notes",
    )
    @classmethod
    def _optional_blank_to_none(cls, value: str | None) -> str | None:
        return _blank_to_none(value)

    @field_validator("email")
    @classmethod
    def _email_format(cls, value: str | None) -> str | None:
        return _validate_email(value)


class ClientResponse(BaseModel):
    """A client card as served."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    company: str | None
    company_number: str | None
    contact_person: str | None
    phone: str | None
    email: str | None
    address: str | None
    payment_terms_days: int | None
    payment_terms_notes: str | None
    notes: str | None
    is_archived: bool
    created_at: datetime
    updated_at: datetime


class ClientListItem(BaseModel):
    """A row in the client list."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    company: str | None
    company_number: str | None
    is_archived: bool


class ClientListResponse(BaseModel):
    """A page of clients with the total, so a client can render pagination controls."""

    items: list[ClientListItem]
    total: int
    limit: int
    offset: int


class ClientSiteItem(BaseModel):
    """One of a client's sites, as listed on the client card (Requirement 5.3).

    A summary, not the full site card — the site's own endpoints serve that. Billing rates are not
    here at all: they are wage-adjacent data the site rate history owns, and a client-site listing
    has no business carrying them.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    site_number: str
    status: str


class ClientSitesResponse(BaseModel):
    """The sites a client owns (Requirement 5.3)."""

    client_id: uuid.UUID
    items: list[ClientSiteItem]
    total: int
