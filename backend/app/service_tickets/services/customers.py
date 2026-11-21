"""Customer management for the service ticket system (Requirement 3, with Requirement 4 for phones).

Staff add, edit, deactivate, reactivate and reset customers here, and list and read them. Customers are
rows of `svc_customers`, never rows of `users` (R3.11).

Conventions of the package, kept here:

* The router owns the transaction. Nothing in this module commits or rolls back the caller's work. The
  only transaction control is the savepoint around an insert or update that can lose a race on the
  unique phone number, so a lost race can be told apart from any other failure without poisoning the
  caller's transaction.
* No HTTP. A business failure raises a `CustomerError` subclass carrying the API error `code`; the
  router chooses the status (422, 409, 404). `CustomerValidationError` also carries `fields`, one code
  per failing field in form reading order, which the router puts in the envelope (R3.5).
* No result carries the password hash or the initial password (R3.12). Every function returns a
  `CustomerView`, a frozen value with no such field, never the ORM row.
* Order of checks for create and edit: validation (422), then existence (404, edit only), then the
  duplicate phone (409), as the ticket endpoints do.

Token revocation (R3.7, R3.9, R3.13): `token_version` goes up in the same single `UPDATE` as the state
change, so two staff actions at once cannot lose an increment. Deactivate and reset bump it. Edit and
reactivate do not: a phone edit keeps outstanding tokens valid, and a reactivated customer signs in with
the password held before the deactivation.

Logging: this module logs nothing. A phone number must never reach a log line.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.service_tickets.error_codes import (
    CODE_CUSTOMER_NOT_FOUND,
    CODE_INVALID_APARTMENT,
    CODE_INVALID_CITY,
    CODE_INVALID_FULL_NAME,
    CODE_INVALID_PHONE,
    CODE_PHONE_ALREADY_REGISTERED,
)
from app.service_tickets.grammars import CUSTOMER_PAGE_SIZE, fold_key
from app.service_tickets.models import Customer, SigninFailure
from app.service_tickets.phone import InvalidPhone, normalize_phone, strip_search_phone_chars

#: The password of a new customer and of a reset customer (the Initial_Password of the glossary).
INITIAL_PASSWORD = "1234"

FULL_NAME_MAX_LENGTH = 100
CITY_MAX_LENGTH = 100
APARTMENT_MAX_LENGTH = 20


class CustomerError(Exception):
    """A business failure of this module. `code` is the API error code."""

    code: str = ""

    def __init__(self) -> None:
        super().__init__(self.code)


class CustomerNotFound(CustomerError):
    """No customer has the given id (HTTP 404 at the router)."""

    code = CODE_CUSTOMER_NOT_FOUND


class PhoneAlreadyRegistered(CustomerError):
    """Another customer, active or inactive, holds the phone number (HTTP 409 at the router)."""

    code = CODE_PHONE_ALREADY_REGISTERED


class CustomerValidationError(CustomerError):
    """One or more fields are invalid (HTTP 422 at the router).

    `fields` maps each failing field (`phone`, `full_name`, `city`, `apartment`) to its error code, in
    form reading order. `code` is the code of the first failing field, so a single failure reads as
    "422 and the code X".
    """

    def __init__(self, fields: dict[str, str]) -> None:
        self.fields = dict(fields)
        self.code = next(iter(self.fields.values()))
        super().__init__()


@dataclass(frozen=True, slots=True)
class CustomerView:
    """A customer as staff see them. There is deliberately no field for the hash."""

    id: uuid.UUID
    phone: str
    full_name: str
    city: str | None
    apartment: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CustomerPage:
    """One page of the customers list. `total` counts every match, not only this page."""

    items: tuple[CustomerView, ...]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class _CleanFields:
    phone: str
    full_name: str
    city: str | None
    apartment: str | None


def _now() -> datetime:
    return datetime.now(UTC)


def _view(customer: Customer) -> CustomerView:
    return CustomerView(
        id=customer.id,
        phone=customer.phone,
        full_name=customer.full_name,
        city=customer.city,
        apartment=customer.apartment,
        is_active=customer.is_active,
        created_at=customer.created_at,
        updated_at=customer.updated_at,
    )


def _optional_text(value: object, max_length: int) -> tuple[str | None, bool]:
    """The trimmed optional text and whether it is valid. Empty after trimming is stored as absent."""
    if value is None:
        return None, True
    if not isinstance(value, str):
        return None, False
    text = value.strip()
    if not text:
        return None, True
    return text, len(text) <= max_length


def _validate(*, phone: object, full_name: object, city: object, apartment: object) -> _CleanFields:
    """Normalize and check the four fields, collecting one code per failing field (R3.3, R3.5)."""
    problems: dict[str, str] = {}

    clean_phone = ""
    try:
        clean_phone = normalize_phone(phone if isinstance(phone, str) else None)
    except InvalidPhone:
        problems["phone"] = CODE_INVALID_PHONE

    clean_name = full_name.strip() if isinstance(full_name, str) else ""
    if not 1 <= len(clean_name) <= FULL_NAME_MAX_LENGTH:
        problems["full_name"] = CODE_INVALID_FULL_NAME

    clean_city, city_ok = _optional_text(city, CITY_MAX_LENGTH)
    if not city_ok:
        problems["city"] = CODE_INVALID_CITY

    clean_apartment, apartment_ok = _optional_text(apartment, APARTMENT_MAX_LENGTH)
    if not apartment_ok:
        problems["apartment"] = CODE_INVALID_APARTMENT

    if problems:
        raise CustomerValidationError(problems)
    return _CleanFields(clean_phone, clean_name, clean_city, clean_apartment)


def _phone_taken(session: Session, phone: str, *, except_id: uuid.UUID | None = None) -> bool:
    query = select(Customer.id).where(Customer.phone == phone)
    if except_id is not None:
        query = query.where(Customer.id != except_id)
    return session.execute(query.limit(1)).first() is not None


@contextmanager
def _phone_guard(session: Session, phone: str, *, except_id: uuid.UUID | None = None) -> Iterator[None]:
    """Run the body and flush it inside a savepoint, mapping a lost phone race to the 409.

    The body must make its change inside the block: `Session.begin_nested` flushes what is pending
    before it opens the savepoint, so a change made earlier would fail outside it and poison the
    caller's transaction. Two requests with the same new phone both pass the pre-check; the unique
    constraint lets exactly one through. The loser's `IntegrityError` rolls back to the savepoint
    only, then a read confirms the cause is the phone. Any other integrity failure is a bug and
    propagates unchanged.
    """
    try:
        with session.begin_nested():
            yield
            session.flush()
    except IntegrityError:
        if _phone_taken(session, phone, except_id=except_id):
            raise PhoneAlreadyRegistered from None
        raise


def _load(session: Session, customer_id: uuid.UUID) -> Customer:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise CustomerNotFound
    return customer


def create_customer(
    session: Session,
    *,
    phone: str,
    full_name: str,
    city: str | None = None,
    apartment: str | None = None,
) -> CustomerView:
    """Create an active customer with the initial password and the must-change flag set (R3.2)."""
    fields = _validate(phone=phone, full_name=full_name, city=city, apartment=apartment)
    if _phone_taken(session, fields.phone):
        raise PhoneAlreadyRegistered
    customer = Customer(
        phone=fields.phone,
        full_name=fields.full_name,
        full_name_key=fold_key(fields.full_name),
        city=fields.city,
        apartment=fields.apartment,
        password_hash=hash_password(INITIAL_PASSWORD),
        must_change_password=True,
        is_active=True,
        token_version=1,
    )
    with _phone_guard(session, fields.phone):
        session.add(customer)
    return _view(customer)


def update_customer(
    session: Session,
    customer_id: uuid.UUID,
    *,
    phone: str,
    full_name: str,
    city: str | None = None,
    apartment: str | None = None,
) -> CustomerView:
    """Replace name, phone, city and apartment (R3.6, R3.13).

    The password hash, the flags and `token_version` stay as they are, and no ticket is touched: a
    ticket stores its own city, apartment and phone, and staff views read the name from this row.
    """
    fields = _validate(phone=phone, full_name=full_name, city=city, apartment=apartment)
    customer = _load(session, customer_id)
    if _phone_taken(session, fields.phone, except_id=customer.id):
        raise PhoneAlreadyRegistered
    with _phone_guard(session, fields.phone, except_id=customer.id):
        customer.phone = fields.phone
        customer.full_name = fields.full_name
        customer.full_name_key = fold_key(fields.full_name)
        customer.city = fields.city
        customer.apartment = fields.apartment
        customer.updated_at = _now()
    return _view(customer)


def _apply(session: Session, customer_id: uuid.UUID, **values: object) -> CustomerView:
    """One `UPDATE` of a customer by primary key. Zero rows matched means the customer is missing."""
    result = session.execute(
        update(Customer)
        .where(Customer.id == customer_id)
        .values(updated_at=_now(), **values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        raise CustomerNotFound
    customer = session.get(Customer, customer_id, populate_existing=True)
    if customer is None:  # deleted between the update and the read; nothing deletes customers today
        raise CustomerNotFound
    return _view(customer)


def deactivate_customer(session: Session, customer_id: uuid.UUID) -> CustomerView:
    """Set the customer inactive and revoke every token issued so far (R3.7). Tickets are untouched."""
    return _apply(
        session, customer_id, is_active=False, token_version=Customer.token_version + 1
    )


def reactivate_customer(session: Session, customer_id: uuid.UUID) -> CustomerView:
    """Set the customer active. Password hash, flags and `token_version` are unchanged (R3.8)."""
    return _apply(session, customer_id, is_active=True)


def reset_password(session: Session, customer_id: uuid.UUID) -> CustomerView:
    """Give the customer the initial password again, active or inactive (R3.9).

    One `UPDATE` replaces the hash, sets the must-change flag and bumps `token_version`. The active
    flag is left alone. The failed sign-in rows of the customer's phone are deleted, which ends any lock.
    The customer row is locked first (`FOR UPDATE` on PostgreSQL) so a concurrent phone edit cannot
    leave the rows of the old number behind or delete those of the new one.
    """
    new_hash = hash_password(INITIAL_PASSWORD)  # slow by design, so before the row is locked
    phone = session.execute(
        select(Customer.phone).where(Customer.id == customer_id).with_for_update()
    ).scalar_one_or_none()
    if phone is None:
        raise CustomerNotFound
    view = _apply(
        session,
        customer_id,
        password_hash=new_hash,
        must_change_password=True,
        token_version=Customer.token_version + 1,
    )
    session.execute(delete(SigninFailure).where(SigninFailure.phone == phone))
    return view


def get_customer(session: Session, customer_id: uuid.UUID) -> CustomerView:
    """The customer with this id, or `CustomerNotFound`."""
    return _view(_load(session, customer_id))


def list_customers(
    session: Session,
    *,
    search: str | None = None,
    active: bool | None = None,
    page: int = 1,
) -> CustomerPage:
    """One page of customers, 50 per page, sorted by `full_name_key` (R3.10).

    `search` matches when the folded text is a substring of the name key, or when the text without
    spaces, hyphens and parentheses is a substring of the phone. The match is a plain substring: `%`,
    `_` and the escape character in the text are escaped, so they match only themselves. `active` is
    `True` for active only, `False` for inactive only and `None` for all; it combines with the search
    by AND. `page` is 1-based and already validated by `grammars.parse_page`; a page past the end is an
    empty list. A search text is never validated as a phone, so it never raises `invalid_phone` (R4.6).
    """
    conditions = []
    if search is not None:
        conditions.append(
            or_(
                Customer.full_name_key.contains(fold_key(search), autoescape=True),
                Customer.phone.contains(strip_search_phone_chars(search), autoescape=True),
            )
        )
    if active is not None:
        conditions.append(Customer.is_active == active)

    total = session.execute(select(func.count()).select_from(Customer).where(*conditions)).scalar_one()
    rows = (
        session.execute(
            select(Customer)
            .where(*conditions)
            .order_by(Customer.full_name_key, Customer.id)
            .offset((page - 1) * CUSTOMER_PAGE_SIZE)
            .limit(CUSTOMER_PAGE_SIZE)
        )
        .scalars()
        .all()
    )
    return CustomerPage(
        items=tuple(_view(row) for row in rows),
        page=page,
        page_size=CUSTOMER_PAGE_SIZE,
        total=int(total),
    )
