"""Request and response schemas of the service-ticket API (R12.4, R3.12, R15.2, R16.1).

Locale-neutral like every other schema: no message text, only values the front ends format.

**One model per audience.** A customer response and a staff response are different classes with no
inheritance between them, and the customer classes are *declared* without the staff-only fields instead
of filtering them at serialisation time. A field a model does not have cannot be sent, whatever the
caller passes in. The customer-facing models are `CustomerTicketRow`, `CustomerTicketDetail`,
`CustomerTicketPage`, `CustomerProfile`, `FormConfig` and `ServiceTokenResponse`. They have no history, no
assigned technician name, no last-modified time, no customer id, no photo key and no credential
(R12.4). A staff customer record (`CustomerRecord`) carries no password hash or token version either
(R3.12): no schema in this module has a credential field.

**A customer response is never built from a staff model.** The customer ticket models do not read
attributes (`from_attributes` is off), so `CustomerTicketRow.model_validate(staff_model)` raises
instead of copying the fields it happens to share. The only way to fill them from stored data is
`customer_ticket_row` or `customer_ticket_detail`, which take the ORM `Ticket` and `Category` and
copy exactly the listed fields. Every response model also has `extra="forbid"`, so constructing one
with a field it does not declare raises.

**Requests** use `extra="forbid"`: an unknown field is a validation error, which the package's route
class answers with 422 `unknown_field`. They carry no length or format rules of their own. Limits,
trimming and the per-field codes (`invalid_full_name`, `invalid_label_he`, ...) belong to the services,
so a body with several bad fields reports all of them in one answer (R3.5, R16.6, R19.5). For the same
reason the status values of a status-change body are not typed as the four-value literal here: a
missing or unknown value must reach the lifecycle service to get `invalid_expected_status` or
`invalid_target_status` (R13.10, R14.11), not Pydantic's generic error.

Ticket submission is a multipart form read by the handler itself, so it has no request model here.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.service_tickets.models import Category, Locality, Ticket

TicketStatus = Literal["received", "in_progress", "closed", "cancelled"]
FieldLabelKey = Literal["city", "apartment", "phone", "category", "description", "photo", "urgent"]


class _Request(BaseModel):
    """Base of every input model: an unexpected field is an error, never silently dropped."""

    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    """Base of every output model: constructing one with an undeclared field raises."""

    model_config = ConfigDict(extra="forbid")


# --- requests: customer


class SignInRequest(_Request):
    """`POST /auth/sign-in`. The phone is normalised and checked by the service (R4, R5)."""

    phone: str
    password: str


class ChangePasswordRequest(_Request):
    """`POST /auth/change-password`. The format of the new password is checked by the service (R6.4)."""

    current_password: str
    new_password: str


class CancelTicketRequest(_Request):
    """`POST /tickets/{n}/cancel`. `expected_status` is the status the customer last saw (R13.1).

    Missing or not one of the four values gives 422 `invalid_expected_status` from the lifecycle
    service (R13.10), so the field accepts anything here.
    """

    expected_status: Any = None


# --- requests: staff


class ResolutionRequest(_Request):
    """The `resolution` of a status change to closed (R29.1): what was done and which parts were replaced.

    Every value is `Any` and defaults to `None`, like the history body, so that a missing or malformed value
    reaches the resolution service and gets its own code (`invalid_history_text`,
    `invalid_technician_name`, `invalid_request` for a `parts_replaced` that is not a boolean,
    `part_required`, `invalid_part`) together with the other failing fields. An unknown key is
    `unknown_field`.
    """

    entry_text: Any = None
    technician_name: Any = None
    parts_replaced: Any = None
    part_ids: Any = None


class StatusChangeRequest(_Request):
    """`POST /staff/tickets/{n}/status` (R14.6, R14.8, R14.11, R29.1).

    `expected_status` and `target_status` are validated by the lifecycle service (codes
    `invalid_expected_status` and `invalid_target_status`). `assigned_technician_name` is the optional
    name set when a ticket moves from received to in progress; its length is checked by the service.
    `resolution` is required for the target `closed` (`resolution_required`) and refused for any other
    target (`unknown_field`); the lifecycle service decides, so a null and a missing value are the same.
    """

    expected_status: Any = None
    target_status: Any = None
    assigned_technician_name: str | None = None
    resolution: ResolutionRequest | None = None


class AssignedTechnicianRequest(_Request):
    """`PUT /staff/tickets/{n}/assigned-technician`. Empty after trimming means unassigned (R16.2)."""

    assigned_technician_name: str


class HistoryEntryRequest(_Request):
    """Body of `POST /staff/tickets/{n}/history` and `PUT /staff/tickets/{n}/history/{id}` (R16.3, R16.5)."""

    entry_text: str
    technician_name: str


class CustomerCreateRequest(_Request):
    """`POST /staff/customers` (R3.2). Empty city or apartment is stored as absent by the service.

    `locality_id` (R30.6) is the optional settlement. It is `Any` so that a malformed value reaches the
    service and gets `invalid_settlement` under `city`, with every other failing field, instead of a
    generic validation error. When it names an active settlement the customer's city becomes its
    Hebrew name and a `city` in the same body is ignored.
    """

    phone: str
    full_name: str
    city: str | None = None
    apartment: str | None = None
    locality_id: Any = None


class CustomerUpdateRequest(_Request):
    """`PUT /staff/customers/{id}` (R3.6). The four editable values are sent together: a PUT replaces them.

    `locality_id` (R30.6) is optional and tells apart three cases, by whether the key was sent: absent
    leaves the stored settlement; a settlement id sets it (and `city` becomes its Hebrew name, any `city`
    in the body is ignored); `null` clears it. See `services.customers.update_customer` for what each case
    does with the city text. `Any`, like on create.
    """

    phone: str
    full_name: str
    city: str | None = None
    apartment: str | None = None
    locality_id: Any = None


class CategoryCreateRequest(_Request):
    """`POST /staff/categories` (R19.4)."""

    name_he: str
    name_en: str


class CategoryUpdateRequest(_Request):
    """`PUT /staff/categories/{id}` (R19.5): rename."""

    name_he: str
    name_en: str


class LocalityCreateRequest(_Request):
    """`POST /staff/localities` (R30.1): a settlement of the list.

    The two names are lenient on purpose (`Any`, absent means `None`), unlike the category bodies: a
    missing or non-string name then reaches the service and gets the list's own code
    (`invalid_settlement_name_he`, `invalid_settlement_name_en`) together with every other failing name,
    instead of the generic `invalid_name_he` of the field name, which is the category's code.
    """

    name_he: Any = None
    name_en: Any = None


class LocalityUpdateRequest(_Request):
    """`PUT /staff/localities/{id}` (R30.1): rename. Lenient like `LocalityCreateRequest`."""

    name_he: Any = None
    name_en: Any = None


class PartCreateRequest(_Request):
    """`POST /staff/parts` (R29.6): a replaceable part. Lenient like `LocalityCreateRequest`
    (`invalid_part_name_he`, `invalid_part_name_en`)."""

    name_he: Any = None
    name_en: Any = None


class PartUpdateRequest(_Request):
    """`PUT /staff/parts/{id}` (R29.6): rename. Lenient like `LocalityCreateRequest`."""

    name_he: Any = None
    name_en: Any = None


class LabelTextsRequest(_Request):
    """The Hebrew and English text of one field label."""

    he: str
    en: str


class LabelsUpdateRequest(_Request):
    """`PUT /staff/labels`: the labels to replace, keyed by form field. Fields not named are unchanged."""

    labels: dict[FieldLabelKey, LabelTextsRequest]


class SettingsUpdateRequest(_Request):
    """`PUT /staff/settings`: the two form options (R19.10). Strict booleans: no `"yes"` or `1`."""

    description_required: StrictBool
    photo_upload_enabled: StrictBool


# --- shared small models


class CategoryRef(_Response):
    """A category as a ticket or the form shows it: id and the two names, nothing about its state."""

    id: uuid.UUID
    name_he: str
    name_en: str


class LocalityRef(_Response):
    """A settlement as the ticket form lists it: id and the two names, nothing about its state (R30.2)."""

    id: uuid.UUID
    name_he: str
    name_en: str


class BrandingInfo(_Response):
    """The branding the forms show: the company name and whether a logo is stored (R28). Never the key."""

    company_name: str | None
    has_logo: bool


class LabelTexts(_Response):
    """The Hebrew and English text of one field label."""

    he: str
    en: str


class FormSettings(_Response):
    """The two form options (R19.10)."""

    description_required: bool
    photo_upload_enabled: bool


# --- customer responses


class ServiceTokenResponse(_Response):
    """A customer token (`sign-in`, `change-password`). Never a refresh token: there is none."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    must_change_password: bool


class CustomerProfile(_Response):
    """`GET /auth/me`: what the signed-in customer sees about themself. No id, no hash, no counters."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    full_name: str
    phone: str
    city: str | None
    apartment: str | None
    must_change_password: bool
    #: The customer's settlement with its current names (R30.6); null when the city is only text.
    locality: LocalityRef | None = None


class FormConfig(_Response):
    """`GET /form-config` and `GET /staff/form-config`: active categories oldest first, the seven labels,
    the two options, the active settlements oldest first (`settlements_required` is true when there is at
    least one, R30.2) and the branding (R28)."""

    categories: list[CategoryRef]
    labels: dict[FieldLabelKey, LabelTexts]
    settings: FormSettings
    localities: list[LocalityRef]
    settlements_required: bool
    branding: BrandingInfo


class TicketCreated(_Response):
    """`POST /tickets` answers only the number (R9.1)."""

    ticket_number: int


class CustomerTicketRow(_Response):
    """A row of the customer's ticket list (R12.1).

    `apartment` is the value stored on the ticket, not the customer record. Not built from attributes
    and not a base class of any staff model; see `customer_ticket_row`.
    """

    ticket_number: int
    category: CategoryRef
    created_date: date
    apartment: str
    status: TicketStatus
    urgent: bool


class CustomerTicketDetail(CustomerTicketRow):
    """An own ticket in full (R12.2). The values are those stored on the ticket, not the customer record.

    The row already carries `apartment`; the detail adds the city, phone, description and photo flag.
    """

    city: str
    phone: str
    description: str
    has_photo: bool
    #: The settlement of the ticket with its current names (R30.5); null for a ticket with a text city.
    locality: LocalityRef | None = None


class CustomerTicketPage(_Response):
    """`GET /tickets` for a customer: 50 per page, newest first."""

    items: list[CustomerTicketRow]
    page: int
    page_size: int
    total: int


def category_ref(category: Category) -> CategoryRef:
    return CategoryRef(id=category.id, name_he=category.name_he, name_en=category.name_en)


def locality_ref(locality: Locality) -> LocalityRef:
    return LocalityRef(id=locality.id, name_he=locality.name_he, name_en=locality.name_en)


def customer_ticket_row(ticket: Ticket, category: Category) -> CustomerTicketRow:
    """The customer row of a stored ticket. Copies exactly the six listed fields."""
    return CustomerTicketRow(
        ticket_number=ticket.ticket_number,
        category=category_ref(category),
        created_date=ticket.created_date,
        apartment=ticket.apartment,
        status=ticket.status,  # type: ignore[arg-type]  # the column check constraint holds the four values
        urgent=ticket.urgent,
    )


def customer_ticket_detail(
    ticket: Ticket, category: Category, locality: Locality | None = None
) -> CustomerTicketDetail:
    """The customer detail of a stored ticket. Copies exactly the eleven listed fields.

    `locality` is the ticket's settlement row, read by the caller, or `None` when it has none.
    """
    return CustomerTicketDetail(
        ticket_number=ticket.ticket_number,
        category=category_ref(category),
        created_date=ticket.created_date,
        apartment=ticket.apartment,
        status=ticket.status,  # type: ignore[arg-type]
        urgent=ticket.urgent,
        city=ticket.city,
        phone=ticket.phone,
        description=ticket.description,
        has_photo=ticket.photo_key is not None,
        locality=None if locality is None else locality_ref(locality),
    )


# --- staff responses


class HistoryEntryOut(_Response):
    """One treatment-history entry (R16.9). `edited_at` is null until the entry is edited."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    entry_text: str
    technician_name: str
    created_at: datetime
    edited_at: datetime | None


class StaffTicketRow(_Response):
    """A row of the staff list and the report table (R15.2). Unrelated to every customer model."""

    ticket_number: int
    created_at: datetime
    customer_name: str
    city: str
    apartment: str
    category: CategoryRef
    status: TicketStatus
    assigned_technician_name: str
    urgent: bool
    #: The settlement with its current names, also when it was deactivated later (R30.5); null for a
    #: ticket with a text city.
    locality: LocalityRef | None = None


class PartRef(_Response):
    """A replaced part as the staff detail shows it: id and the current names, active or not (R29.7)."""

    id: uuid.UUID
    name_he: str
    name_en: str


class StaffTicketDetail(StaffTicketRow):
    """A ticket as staff open it (R16.1): the row plus contact data, history and last-modified time."""

    phone: str
    description: str
    has_photo: bool
    history: list[HistoryEntryOut]
    updated_at: datetime
    #: Username of the staff member who opened the ticket for the customer (R26.4); null when the
    #: customer opened it. Staff responses only: no customer model carries it (R26.6).
    opened_by_username: str | None = None
    #: The parts recorded by the last resolution, ordered by part sequence, with their current names
    #: (R29.7). Empty when there are none. Staff responses only: no customer model carries it (R29.9).
    parts_replaced: list[PartRef] = Field(default_factory=list)


class StaffTicketPage(_Response):
    """`GET /staff/tickets`: 25 per page."""

    items: list[StaffTicketRow]
    page: int
    page_size: int
    total: int


class CustomerRecord(_Response):
    """A customer as the staff Customers screen shows it (R3.10). No hash, no token version (R3.12)."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    full_name: str
    phone: str
    city: str | None
    apartment: str | None
    is_active: bool
    #: The settlement with its current names (R30.6); null when the city is only text.
    locality: LocalityRef | None = None


class CustomerRecordPage(_Response):
    """`GET /staff/customers`: 50 per page."""

    items: list[CustomerRecord]
    page: int
    page_size: int
    total: int


class CategoryRecord(_Response):
    """A category for configuration staff and for the list filters: active and inactive (R19.9)."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    name_he: str
    name_en: str
    is_active: bool


class CategoryList(_Response):
    """`GET /staff/categories`, oldest first."""

    items: list[CategoryRecord]


class CategoryDeleteResult(_Response):
    """`DELETE /staff/categories/{id}`: removed, or kept and deactivated because tickets use it (R19.12)."""

    result: Literal["removed", "deactivated"]


class LocalityRecord(_Response):
    """A settlement for configuration staff: active and inactive (R30.1)."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    name_he: str
    name_en: str
    is_active: bool


class LocalityList(_Response):
    """`GET /staff/localities`, oldest first."""

    items: list[LocalityRecord]


class LocalityDeleteResult(_Response):
    """`DELETE /staff/localities/{id}`: removed, or kept and deactivated because it is used (R30.1)."""

    result: Literal["removed", "deactivated"]


class PartRecord(_Response):
    """A replaceable part for configuration staff: active and inactive (R29.6)."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    name_he: str
    name_en: str
    is_active: bool


class PartList(_Response):
    """`GET /staff/parts`, oldest first."""

    items: list[PartRecord]


class PartDeleteResult(_Response):
    """`DELETE /staff/parts/{id}`: removed, or kept and deactivated because a ticket uses it (R29.6)."""

    result: Literal["removed", "deactivated"]


class LabelsResponse(_Response):
    """The seven field labels."""

    labels: dict[FieldLabelKey, LabelTexts]


class SettingsResponse(_Response):
    """`GET` and `PUT /staff/settings`."""

    settings: FormSettings


class NumberWarning(_Response):
    """How many ticket numbers are left, shown to configuration staff below 1,000 (R10.9)."""

    remaining: int


class ServiceDashboardResponse(_Response):
    """`GET /staff/dashboard`: three counts from one read (R17.1, R17.3)."""

    open: int
    in_progress: int
    urgent: int
    ticket_number_warning: NumberWarning | None = None


class StatusCounts(_Response):
    """Per-status counts. All four are always present, zero included (R18.7)."""

    received: int
    in_progress: int
    closed: int
    cancelled: int


class CategoryCount(_Response):
    """Tickets per category, for categories with at least one matching ticket (R18.7)."""

    category: CategoryRef
    count: int


class ReportTicketRow(StaffTicketRow):
    """A row of the report table (R29.7): the staff list row plus the names of the replaced parts.

    `parts` exists on the report row only; `GET /staff/tickets` keeps `StaffTicketRow` unchanged.
    """

    #: The parts recorded by the last resolution, ordered by part sequence, with their current names,
    #: active or not. Empty when there are none. Staff responses only (R29.9).
    parts: list[PartRef] = Field(default_factory=list)


class ReportTicketPage(_Response):
    """The ticket table of `GET /staff/reports`: 25 per page."""

    items: list[ReportTicketRow]
    page: int
    page_size: int
    total: int


class ReportResponse(_Response):
    """`GET /staff/reports` (R18). The per-status and per-category counts each sum to `total` (R18.8)."""

    total: int
    open: int
    by_status: StatusCounts
    by_category: list[CategoryCount]
    tickets: ReportTicketPage