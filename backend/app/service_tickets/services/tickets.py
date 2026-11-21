"""Ticket submission: validation and creation.

Requirements 9.4, 9.5, 9.6, 9.8, 9.9, 9.13, 9.14 (the form rules), 10.3 and 10.6 (a number rises with
allocation order, and a failure after allocation burns it) and 14.1 (a new ticket is `received`).

Two steps, in this order, so an invalid submission can never use up one of the 90,000 numbers:

1. `validate_submission(values, settings, active_category_ids)` checks every field and collects every
   failure, one code per failing field, in form reading order (`city`, `apartment`, `phone`,
   `category_id`, `description`, `urgent`), then the names of unknown fields. It raises
   `SubmissionValidationError`, whose `code` is the first failing field's code, or returns a
   `CleanSubmission`. Nothing is read from or written to the database.
2. `create_ticket(session, customer, clean, photo_key, now, allocator)` takes a `CleanSubmission`, so it
   only ever runs after validation passed. It allocates the number, then asks `now` for the creation
   time (so `created_at` is taken right after the allocation, R10.3), computes `created_date` in the
   application time zone, and inserts the row.

Rules kept here:

* City, apartment and description are stored trimmed and otherwise verbatim, so markup such as `<b>x</b>`
  is kept as typed and shown literally by the front ends (R9.13). Length is counted in Unicode
  characters after trimming. A value PostgreSQL cannot store (a NUL character or a lone surrogate)
  counts as invalid, so it fails with its field's code and never surfaces as a database error.
* The phone is stored as the Normalized_Phone of the submitted value (R4, R9.8).
* A description is required to hold 1 to 2000 characters while the setting says so; otherwise an empty
  or whitespace-only one is accepted and stored as empty (R9.5).
* `urgent` is a boolean, or the text `true` or `false` (what a multipart form carries, any letter
  case, surrounding whitespace ignored). Omitted means false. Anything else is `invalid_urgent`.
* `category_id` is a UUID (or its canonical text) that is in `active_category_ids`. The caller reads
  that set, so a category deactivated before the request arrives is rejected (R9.4).
* The key `photo` is a known form field and is ignored here: the photo is handled by the router and the
  photo pipeline (Requirement 11). Any other unlisted key, such as a customer id, a status or a ticket
  number, is `unknown_field` (R9.9), and the owner is only ever `customer.id`.

`create_ticket` reads `customer.id` and nothing else from the customer, and writes nothing to
`svc_customers` (R9.14). It flushes the ticket so a constraint failure surfaces here, never commits and
never raises HTTP errors; the router owns the transaction. A failure after the number was allocated
leaves a gap in the sequence, which is accepted (R10.6).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.service_tickets.error_codes import (
    CODE_INVALID_APARTMENT,
    CODE_INVALID_CATEGORY,
    CODE_INVALID_CITY,
    CODE_INVALID_DESCRIPTION,
    CODE_INVALID_PHONE,
    CODE_INVALID_URGENT,
    CODE_UNKNOWN_FIELD,
)
from app.service_tickets.grammars import created_date_for, fold_key, parse_uuid_path
from app.service_tickets.models import Ticket
from app.service_tickets.phone import InvalidPhone, normalize_phone
from app.service_tickets.services.config_admin import FormSettings
from app.service_tickets.services.numbering import Allocator, allocate_ticket_number

CITY_MAX_LENGTH = 100
APARTMENT_MAX_LENGTH = 20
DESCRIPTION_MAX_LENGTH = 2000

#: The form fields that carry a value this module validates, in form reading order.
FIELD_CITY = "city"
FIELD_APARTMENT = "apartment"
FIELD_PHONE = "phone"
FIELD_CATEGORY = "category_id"
FIELD_DESCRIPTION = "description"
FIELD_URGENT = "urgent"
#: The photo is a file part handled outside this module; it is a known field and is not validated here.
FIELD_PHOTO = "photo"

VALIDATED_FIELDS = (
    FIELD_CITY,
    FIELD_APARTMENT,
    FIELD_PHONE,
    FIELD_CATEGORY,
    FIELD_DESCRIPTION,
    FIELD_URGENT,
)
KNOWN_FIELDS = frozenset((*VALIDATED_FIELDS, FIELD_PHOTO))

INITIAL_STATUS = "received"


class SubmissionValidationError(Exception):
    """One or more fields failed (HTTP 422 at the router).

    `fields` maps each failing field to its code, in form reading order with unknown fields last.
    `code` is the first failing field's code, which is what the error envelope puts at the top level.
    """

    def __init__(self, fields: Mapping[str, str]) -> None:
        if not fields:
            raise ValueError("SubmissionValidationError needs at least one failing field")
        self.fields: dict[str, str] = dict(fields)
        self.code = next(iter(self.fields.values()))
        super().__init__(self.code)


class _DescriptionSettings(Protocol):
    description_required: bool


@dataclass(frozen=True, slots=True)
class CleanSubmission:
    """A submission that passed every rule, with its values in the form they are stored."""

    city: str
    apartment: str
    phone: str
    category_id: uuid.UUID
    description: str
    urgent: bool


class _Owner(Protocol):
    """What `create_ticket` needs from a customer: the id. `Customer` rows satisfy it."""

    id: uuid.UUID


def _storable(text: str) -> bool:
    """False for text PostgreSQL cannot hold: a NUL character or a lone surrogate."""
    if "\x00" in text:
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _clean_required_text(value: object, max_length: int) -> str | None:
    """The trimmed text when it holds 1 to `max_length` storable characters, else `None`."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not 1 <= len(text) <= max_length or not _storable(text):
        return None
    return text


def _clean_description(value: object, *, required: bool) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) > DESCRIPTION_MAX_LENGTH or not _storable(text):
        return None
    if required and not text:
        return None
    return text


def _clean_urgent(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word == "true":
            return True
        if word == "false":
            return False
    return None


def _clean_category(value: object, active_category_ids: Collection[uuid.UUID]) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        category_id = value
    elif isinstance(value, str):
        category_id = parse_uuid_path(value)
    else:
        return None
    if category_id is None or category_id not in active_category_ids:
        return None
    return category_id


def validate_submission(
    values: Mapping[str, object],
    settings: FormSettings | _DescriptionSettings,
    active_category_ids: Collection[uuid.UUID],
) -> CleanSubmission:
    """Check every rule of Requirements 9.4, 9.5 and 9.9 and return the clean values.

    Raises `SubmissionValidationError` listing every failing field when any rule fails (R9.6). The
    check does not touch the database: `active_category_ids` is the set of active categories the caller
    read, and `settings.description_required` says whether an empty description is acceptable.
    """
    problems: dict[str, str] = {}

    city = _clean_required_text(values.get(FIELD_CITY), CITY_MAX_LENGTH)
    if city is None:
        problems[FIELD_CITY] = CODE_INVALID_CITY

    apartment = _clean_required_text(values.get(FIELD_APARTMENT), APARTMENT_MAX_LENGTH)
    if apartment is None:
        problems[FIELD_APARTMENT] = CODE_INVALID_APARTMENT

    phone_raw = values.get(FIELD_PHONE)
    phone = ""
    try:
        phone = normalize_phone(phone_raw if isinstance(phone_raw, str) else None)
    except InvalidPhone:
        problems[FIELD_PHONE] = CODE_INVALID_PHONE

    category_id = _clean_category(values.get(FIELD_CATEGORY), active_category_ids)
    if category_id is None:
        problems[FIELD_CATEGORY] = CODE_INVALID_CATEGORY

    description = _clean_description(
        values.get(FIELD_DESCRIPTION, ""), required=settings.description_required
    )
    if description is None:
        problems[FIELD_DESCRIPTION] = CODE_INVALID_DESCRIPTION

    urgent = _clean_urgent(values.get(FIELD_URGENT, False))
    if urgent is None:
        problems[FIELD_URGENT] = CODE_INVALID_URGENT

    for name in values:
        if name not in KNOWN_FIELDS:
            problems[name] = CODE_UNKNOWN_FIELD

    # `problems` is non-empty exactly when one of these is None; the checks also narrow the types.
    if problems or city is None or apartment is None or category_id is None:
        raise SubmissionValidationError(problems)
    if description is None or urgent is None:
        raise SubmissionValidationError(problems)
    return CleanSubmission(
        city=city,
        apartment=apartment,
        phone=phone,
        category_id=category_id,
        description=description,
        urgent=urgent,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def create_ticket(
    session: Session,
    customer: _Owner,
    values: CleanSubmission,
    photo_key: str | None = None,
    now: Callable[[], datetime] | None = None,
    allocator: Allocator | None = None,
    *,
    timezone: str | None = None,
) -> Ticket:
    """Insert a `received` ticket for `customer` and return it, flushed and not committed.

    `values` is the result of `validate_submission`, so the number is allocated only after validation
    passed. `created_at` comes from calling `now` right after the allocation and `created_date` is its
    calendar date in `timezone` (default: the application time zone, `Asia/Jerusalem`). The assigned
    technician name is empty, `updated_at` equals `created_at`, and the customer row is neither read
    beyond its id nor changed. Raises `TicketNumbersExhausted` when the sequence is used up.
    """
    number = allocate_ticket_number(session, allocator)
    created_at = (now or _utc_now)()
    zone = timezone if timezone is not None else get_settings().app_timezone
    ticket = Ticket(
        ticket_number=number,
        customer_id=customer.id,
        category_id=values.category_id,
        city=values.city,
        city_key=fold_key(values.city),
        apartment=values.apartment,
        phone=values.phone,
        description=values.description,
        photo_key=photo_key,
        urgent=values.urgent,
        status=INITIAL_STATUS,
        assigned_technician_name="",
        created_at=created_at,
        created_date=created_date_for(created_at, zone),
        updated_at=created_at,
    )
    session.add(ticket)
    session.flush()
    return ticket
