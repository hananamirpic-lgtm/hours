"""Employee records and their rate history (Requirement 3, and 16.9 for the resolver).

The service owns every business rule; the router only translates. Nothing here raises an HTTP error
or commits — the caller's unit of work decides the fate of the change and its audit rows together,
which is the invariant `app.services.audit` rests on. The one thing that does need care is that a
uniqueness check and a rate reconciliation happen against a flushed session, so a value written
earlier in the same transaction is visible to a check made later in it.

Three rules carry the weight.

**Passport uniqueness is over non-terminated employees, checked in code.** Requirement 3.6 says a
passport is unique across employees who are not terminated, and the database has a partial unique
index that says the same — but only in PostgreSQL. The unit tests run on SQLite, which has neither
that partial index nor a way to express it, so the rule is enforced here by a query and reported with
the conflicting employee named (which the index could never do — it can only reject). The database
index remains the backstop against a race the application check cannot see.

**Status is the whole lifecycle; nothing is deleted.** Requirement 3.8. A `terminated` employee's row
stays, so historical time entries keep a valid parent. Termination frees the passport for reuse,
which is exactly why the uniqueness check excludes terminated rows.

**Rate history is a non-overlapping chain, reconciled from a submitted list.** A client sends the
history it wants; the service sorts it, checks no two rows overlap, and writes them, replacing what
was there. The resolver then answers "the rate in force on this date" by finding the one row whose
period covers it — which is what makes a mid-month rate change split a month correctly (16.9).
"""

from __future__ import annotations

import contextlib
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from app.core import storage as storage_module
from app.core.crypto import get_encryptor
from app.core.storage import ObjectStorage
from app.models.employee import Employee, EmployeeRate, EmployeeStatus
from app.models.staffing_company import StaffingCompany
from app.models.user import User
from app.schemas.employee import (
    EmployeeCreate,
    EmployeeRateInput,
    EmployeeUpdate,
)
from app.services import user as user_service
from app.services.audit import AuditContext, record_change, record_model_changes, snapshot

# --------------------------------------------------------------------------- errors
# Domain errors, not HTTP errors: a service that knew about status codes could not be called from a
# scheduled job. Each carries a machine `code` the router lifts into the error envelope, matching how
# `app.services.auth` structures its failures.


class EmployeeError(Exception):
    """Base for every employee-service failure. `code` is what the front end translates."""

    code = "employee_error"


class EmployeeNotFound(EmployeeError):
    code = "employee_not_found"

    def __init__(self, employee_id: uuid.UUID) -> None:
        super().__init__(f"no employee {employee_id}")
        self.employee_id = employee_id


class NoEmployeeForCaller(EmployeeError):
    """The caller's login is not linked to an employee, so it has no photo of its own.

    Only the employee role carries an `employee_id`; a manager or accounting login has none and no
    personal record to attach a photo to. Mirrors `app.services.scan.NoEmployeeForCaller` so the
    self-service photo path refuses a not-linked caller exactly the way the scan path does — the
    router maps it to a 403.
    """

    code = "no_employee_for_caller"


class DuplicatePassport(EmployeeError):
    """A non-terminated employee already holds this passport number (Requirement 3.6).

    Carries the conflicting employee so the router can name them, which the requirement asks for and a
    bare uniqueness violation could never supply.
    """

    code = "duplicate_passport"

    def __init__(self, conflicting: Employee) -> None:
        super().__init__(f"passport already held by employee {conflicting.id}")
        self.conflicting = conflicting


class StaffingCompanyLinkNotFound(EmployeeError):
    """The staffing company an employee was linked to does not exist (Requirement 2.4).

    Raised on create or update when `staffing_company_id` references no staffing company. Reuses the
    staffing-company service's machine code so the front end translates it the same way whether the
    miss happens on the company screen or the employee screen; the router maps it to a 4xx.
    """

    code = "staffing_company_not_found"

    def __init__(self, staffing_company_id: uuid.UUID) -> None:
        super().__init__(f"no staffing company {staffing_company_id}")
        self.staffing_company_id = staffing_company_id


class NoEmployeeNumberAvailable(EmployeeError):
    """Every number in 2000-2999 is held by a non-terminated employee or an active login (Requirement 2.6)."""

    code = "no_employee_number_available"


class InvalidStatusTransition(EmployeeError):
    """A status change the lifecycle does not allow (Requirement 3.4)."""

    code = "invalid_status_transition"

    def __init__(self, current: EmployeeStatus, requested: EmployeeStatus) -> None:
        super().__init__(f"cannot move from {current} to {requested}")
        self.current = current
        self.requested = requested


class OverlappingRates(EmployeeError):
    """Two submitted rate rows cover the same date (Requirement 3.3, 16.9)."""

    code = "overlapping_rates"


class InvalidRatePeriod(EmployeeError):
    """A rate row whose `effective_to` precedes its `effective_from`."""

    code = "invalid_rate_period"


# --------------------------------------------------------------------------- status transitions

#: The lifecycle of Requirement 3.4. Active, On Leave, Inactive move freely between one another — an
#: employee comes back from leave, is stood down, returns. Terminated is the one door that is meant to
#: be one-way: a terminated employee is off the books, and reinstating them is a rehire (a new record
#: or an explicit reactivation), not a quiet status flip that would silently re-collide their passport
#: against the uniqueness rule that let it be reused. A no-op transition (same status) is allowed so a
#: caller resubmitting the current status is not an error.
_TERMINAL = EmployeeStatus.TERMINATED
_REVERSIBLE = frozenset(
    {EmployeeStatus.ACTIVE, EmployeeStatus.ON_LEAVE, EmployeeStatus.INACTIVE}
)


def _transition_allowed(current: EmployeeStatus, requested: EmployeeStatus) -> bool:
    if current == requested:
        return True
    if current is _TERMINAL:
        # Leaving terminated is a rehire decision, not a status edit.
        return False
    if requested is _TERMINAL:
        return True
    return current in _REVERSIBLE and requested in _REVERSIBLE


# --------------------------------------------------------------------------- reads


def _base_select() -> Select[tuple[Employee]]:
    return select(Employee).options(selectinload(Employee.rates))


def get_employee(session: Session, employee_id: uuid.UUID) -> Employee:
    """Load one employee with its rate history, or raise `EmployeeNotFound`."""
    employee = session.scalars(_base_select().where(Employee.id == employee_id)).one_or_none()
    if employee is None:
        raise EmployeeNotFound(employee_id)
    return employee


@dataclass(frozen=True, slots=True)
class EmployeePage:
    """A page of employees plus the unfiltered-by-paging total, for list rendering."""

    items: Sequence[Employee]
    total: int


def list_employees(
    session: Session,
    *,
    status: EmployeeStatus | None = None,
    scope_statement: Select[tuple[Employee]] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> EmployeePage:
    """A stable-sorted page of employees (Requirement 22.5).

    `scope_statement` lets the router hand in a query already narrowed to the caller's sites; when it
    is `None` the service lists across all employees, which is the administrator and accounting case.
    Sort is `(full_name, id)` so the order is total and does not shift between pages when two people
    share a name.
    """
    statement = scope_statement if scope_statement is not None else _base_select()
    if status is not None:
        statement = statement.where(Employee.status == status)

    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    ordered = statement.order_by(Employee.full_name, Employee.id).limit(limit).offset(offset)
    items = list(session.scalars(ordered).unique())
    return EmployeePage(items=items, total=total)


# --------------------------------------------------------------------------- passport uniqueness


def _passport_hash(passport_number: str) -> str:
    """The keyed digest the uniqueness check and the `passport_number_hash` column both key on."""
    return get_encryptor().deterministic_hash(passport_number)


def _find_active_holder(
    session: Session, passport_hash: str, *, exclude_id: uuid.UUID | None = None
) -> Employee | None:
    """A non-terminated employee holding this passport, if any (Requirement 3.6).

    Excludes `exclude_id` so an update that leaves the passport unchanged does not collide with the
    row being updated. Terminated employees are excluded, which is what lets a recycled passport
    number be reused once its former holder is off the books.
    """
    statement = (
        select(Employee)
        .where(Employee.passport_number_hash == passport_hash)
        .where(Employee.status != EmployeeStatus.TERMINATED)
    )
    if exclude_id is not None:
        statement = statement.where(Employee.id != exclude_id)
    return session.scalars(statement).first()


# --------------------------------------------------------------------------- employee-number generation

#: The employee-number range (Requirement 2.1). Inclusive: 1000 candidates, all matching ^2\d{3}$.
EMPLOYEE_NUMBER_MIN = 2000
EMPLOYEE_NUMBER_MAX = 2999


def _number_in_use(session: Session, candidate: str) -> bool:
    """True if the candidate is held by a non-terminated employee OR any active login (Requirement 2.2).

    A number is "held" while a non-terminated employee carries it, exactly as the passport check
    excludes terminated rows so a released number is free (mirrors `_find_active_holder`); and while an
    active login owns that username, so the generated number can safely become the login's username.
    """
    employee_hit = session.scalar(
        select(Employee.id)
        .where(Employee.employee_number == candidate)
        .where(Employee.status != EmployeeStatus.TERMINATED)
    )
    if employee_hit is not None:
        return True
    login_hit = session.scalar(
        select(User.id).where(User.username == candidate).where(User.is_active.is_(True))
    )
    return login_hit is not None


def _generate_employee_number(session: Session) -> str:
    """A free 4-digit number, chosen at random and re-tried until unused (Requirements 2.1, 2.2, 2.6).

    Random-first rather than max+1 so a released (terminated) number is naturally reused and the search
    does not degrade as the range fills. The range is bounded (1000 values), so termination is
    guaranteed: a bounded number of random candidates are tried first, then a deterministic sweep over
    the whole range is made to be certain before giving up, returning the first free value. Only when
    every value in 2000-2999 is taken does it raise `NoEmployeeNumberAvailable`. Never commits and never
    raises an HTTP error — only the domain error.
    """
    for _ in range(EMPLOYEE_NUMBER_MAX - EMPLOYEE_NUMBER_MIN + 1):
        candidate = str(random.randint(EMPLOYEE_NUMBER_MIN, EMPLOYEE_NUMBER_MAX))
        if not _number_in_use(session, candidate):
            return candidate

    # Deterministic sweep: the random phase can revisit taken candidates, so a full pass over the range
    # is the certainty that a free value is found if one exists.
    for value in range(EMPLOYEE_NUMBER_MIN, EMPLOYEE_NUMBER_MAX + 1):
        candidate = str(value)
        if not _number_in_use(session, candidate):
            return candidate

    raise NoEmployeeNumberAvailable


# --------------------------------------------------------------------------- create


def _require_staffing_company(session: Session, staffing_company_id: uuid.UUID) -> None:
    """Confirm a staffing company exists, or raise `StaffingCompanyLinkNotFound` (Requirement 2.4).

    A read used by create and update before writing the link, so a request naming a company that does
    not exist is refused and (on update) the existing link is left untouched.
    """
    exists = session.scalar(
        select(StaffingCompany.id).where(StaffingCompany.id == staffing_company_id)
    )
    if exists is None:
        raise StaffingCompanyLinkNotFound(staffing_company_id)


def create_employee(session: Session, payload: EmployeeCreate, *, context: AuditContext) -> Employee:
    """Create an employee, its opening rate if given, and an audit row per field (Requirement 3.9).

    Passport uniqueness is checked before the insert so the conflicting employee can be named; the
    database's partial unique index is the backstop for the race between the check and the flush.
    """
    passport_hash = _passport_hash(payload.passport_number)
    existing = _find_active_holder(session, passport_hash)
    if existing is not None:
        raise DuplicatePassport(existing)

    # The staffing company is mandatory on create (Requirement 2.1-2.3); confirm it exists before the
    # insert so a bad reference is a clean refusal, not a foreign-key violation at flush.
    _require_staffing_company(session, payload.staffing_company_id)

    # A free 4-digit number is generated up front (Requirement 2.1-2.3) and doubles as the login
    # username below. It is chosen against the flushed-but-uncommitted session, so a number taken
    # earlier in this same transaction is already visible here.
    number = _generate_employee_number(session)

    employee = Employee(
        full_name=payload.full_name,
        full_name_en=payload.full_name_en,
        passport_number=payload.passport_number,
        passport_number_hash=payload.passport_number,  # DeterministicHash hashes on bind.
        employee_number=number,
        phone=payload.phone,
        country=payload.country,
        date_of_birth=payload.date_of_birth,
        address=payload.address,
        emergency_contact_name=payload.emergency_contact_name,
        emergency_contact_phone=payload.emergency_contact_phone,
        notes=payload.notes,
        start_date=payload.start_date,
        position=payload.position,
        status=payload.status,
        photo_key=payload.photo_key,
        staffing_company_id=payload.staffing_company_id,
    )
    session.add(employee)
    # Flush so the row has an id the audit rows and any rate rows can reference, and so the passport
    # hash is materialised for the uniqueness backstop.
    session.flush()

    _audit_creation(session, employee, context=context)

    # Auto-provision the employee-role login whose username is the number (Requirement 3.1-3.4). It
    # runs in this same transaction, so the login and the employee land or roll back together; the
    # employee service still commits nothing.
    user_service.create_employee_login(
        session, number=number, employee_id=employee.id, context=context
    )

    if payload.rate is not None:
        _write_rate_history(session, employee, [payload.rate], context=context)

    return employee


def _audit_creation(session: Session, employee: Employee, *, context: AuditContext) -> None:
    """One audit row per field set at creation, so a card's origin is as traceable as its edits.

    Diffing against an empty snapshot reuses the same field-by-field machinery an update uses, and the
    sensitive fields are redacted by the same rule, so a passport number never lands in the audit
    table even on the create path.
    """
    empty = dict.fromkeys(snapshot(employee), None)
    record_model_changes(session, employee, empty, context=context, reason="employee_created")


# --------------------------------------------------------------------------- update


#: Attribute names the update path may write. Status is not among them — it moves through
#: `change_status`, which owns the transition rules — and neither are the rate fields, which the
#: rates endpoint owns. Passport is handled specially because it drives the uniqueness check and the
#: hash column.
_UPDATABLE_FIELDS = (
    "full_name",
    "full_name_en",
    "phone",
    "country",
    "date_of_birth",
    "address",
    "emergency_contact_name",
    "emergency_contact_phone",
    "notes",
    "start_date",
    "position",
    "photo_key",
    "staffing_company_id",
)


def update_employee(
    session: Session, employee_id: uuid.UUID, payload: EmployeeUpdate, *, context: AuditContext
) -> Employee:
    """Apply a partial update, checking passport uniqueness if the passport changes.

    Only the fields present in the request are touched (`exclude_unset`), so a patch that names three
    fields leaves the rest alone, and only the fields that actually moved produce an audit row.
    """
    employee = get_employee(session, employee_id)
    changes = payload.model_dump(exclude_unset=True)

    before = snapshot(employee)

    if "passport_number" in changes:
        new_passport = changes["passport_number"]
        new_hash = _passport_hash(new_passport)
        if new_hash != employee.passport_number_hash:
            conflict = _find_active_holder(session, new_hash, exclude_id=employee.id)
            if conflict is not None:
                raise DuplicatePassport(conflict)
        employee.passport_number = new_passport
        employee.passport_number_hash = new_passport

    if changes.get("staffing_company_id") is not None:
        _require_staffing_company(session, changes["staffing_company_id"])

    for field in _UPDATABLE_FIELDS:
        if field in changes:
            setattr(employee, field, changes[field])

    session.flush()
    record_model_changes(session, employee, before, context=context)
    return employee


# --------------------------------------------------------------------------- status transition


def change_status(
    session: Session,
    employee_id: uuid.UUID,
    new_status: EmployeeStatus,
    *,
    context: AuditContext,
    reason: str | None = None,
) -> Employee:
    """Move an employee to `new_status`, or raise `InvalidStatusTransition` (Requirement 3.4, 3.8).

    A no-op (same status) is allowed and writes no audit row, so a client resubmitting the current
    status is harmless. There is no delete: the row stays, its history intact.
    """
    employee = get_employee(session, employee_id)
    current = employee.status
    if not _transition_allowed(current, new_status):
        raise InvalidStatusTransition(current, new_status)
    if current == new_status:
        return employee

    before = snapshot(employee, fields=["status"])
    employee.status = new_status
    session.flush()
    record_model_changes(
        session, employee, before, context=context, reason=reason, fields=["status"]
    )

    # Termination frees the number (the uniqueness check excludes terminated rows, so the value stays
    # on the row for history yet is available to a future create) and disables the linked login
    # (Requirement 4.2, 4.4, 4.5). Other transitions leave the login alone. A no-op if there is no
    # linked login or it is already inactive.
    if new_status is EmployeeStatus.TERMINATED:
        user_service.disable_employee_login(session, employee.id, context=context)

    return employee


# --------------------------------------------------------------------------- rate history


def _validate_rate_chain(rates: Sequence[EmployeeRateInput]) -> list[EmployeeRateInput]:
    """Sort the submitted rows and reject any that overlap or are internally out of order.

    Overlap is checked on the inclusive `[from, to]` periods, matching both `EmployeeRate.covers` and
    the database exclusion constraint: a row ending 15 August and a row starting 16 August are fine,
    but two rows both covering 16 August are not. An open-ended row (`effective_to is None`) may only
    be the last in the chain, because anything after it would fall inside its still-in-force period.
    """
    ordered = sorted(rates, key=lambda rate: rate.effective_from)
    for rate in ordered:
        if rate.effective_to is not None and rate.effective_to < rate.effective_from:
            raise InvalidRatePeriod
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if earlier.effective_to is None or earlier.effective_to >= later.effective_from:
            raise OverlappingRates
    return ordered


def replace_rate_history(
    session: Session,
    employee_id: uuid.UUID,
    rates: Sequence[EmployeeRateInput],
    *,
    context: AuditContext,
) -> Employee:
    """Replace an employee's rate history with the submitted, reconciled chain (Requirement 3.3).

    The whole history is replaced rather than appended to, because a client that owns the rates screen
    is stating the intended history, and reconciling row-by-row would leave a stale row behind on any
    edit that removed one. The overlap check runs first, so a rejected submission leaves the existing
    history untouched.
    """
    employee = get_employee(session, employee_id)
    _write_rate_history(session, employee, rates, context=context)
    return employee


def _write_rate_history(
    session: Session,
    employee: Employee,
    rates: Sequence[EmployeeRateInput],
    *,
    context: AuditContext,
) -> None:
    ordered = _validate_rate_chain(rates)

    for existing in list(employee.rates):
        session.delete(existing)
    employee.rates.clear()

    for rate in ordered:
        employee.rates.append(
            EmployeeRate(
                hourly_wage=rate.hourly_wage,
                overtime_rate=rate.overtime_rate,
                shabbat_holiday_rate=rate.shabbat_holiday_rate,
                travel_allowance_daily=rate.travel_allowance_daily,
                effective_from=rate.effective_from,
                effective_to=rate.effective_to,
            )
        )
    session.flush()

    # One audit row on the employee marking that pay changed. The rate values themselves are wage
    # data; the audit records that the history was rewritten and by whom, which is what a dispute
    # needs, without copying wage figures into a table that is never deleted.
    record_change(
        session,
        entity_type="employees",
        entity_id=employee.id,
        field="rates",
        old_value=None,
        new_value=f"{len(ordered)} rate period(s)",
        context=context,
        reason="rates_updated",
    )


def resolve_rate(employee: Employee, on_date: date) -> EmployeeRate | None:
    """The rate in force on `on_date`, or `None` if no row covers it (Requirement 16.9).

    Pure: it reads the already-loaded history and picks the one row whose inclusive period covers the
    date. Because the chain is non-overlapping, at most one row can match; the latest-starting match is
    returned defensively so a hand-inserted overlap resolves deterministically rather than by load
    order. This is the function payroll calls per work date to split a mid-month rate change.
    """
    covering = [rate for rate in employee.rates if rate.covers(on_date)]
    if not covering:
        return None
    return max(covering, key=lambda rate: rate.effective_from)


# --------------------------------------------------------------------------- self-service photo
# The employee mobile app lets a signed-in employee replace their *own* profile photo (Requirement
# 3.1, employee-facing). Two steps so the bytes never cross the API (Requirements 4.3, 20.3):
# `begin_photo_upload` mints a constrained presigned URL, the client PUTs straight to storage, and
# `complete_photo_upload` verifies the stored bytes before setting `photo_key`. A profile photo is a
# face, not a passport scan, so the accepted set is images only — a PDF is refused at both the presign
# guard and the server-side verification, even though it is a valid *document* type. Nothing here
# commits; the router owns the transaction, so the `photo_key` change and its audit row share a fate.


def begin_photo_upload(
    session: Session,
    employee_id: uuid.UUID,
    *,
    mime_type: str,
    storage: ObjectStorage,
) -> storage_module.PresignedUpload:
    """Issue a constrained presigned upload URL for the caller's own profile photo.

    The declared type is checked against the image set (`image/jpeg`, `image/png`) *before* a URL is
    minted, so a PDF or any other type is refused up front rather than caught only after an upload —
    `ensure_image_type` raises `UnsupportedFileType`, which the router maps to a 400. The employee is
    loaded to confirm it exists (a not-linked caller never reaches here; the router resolves the
    employee id from the caller). The presigned URL pins the content type and caps the size, and the
    real bytes are verified again at completion.
    """
    employee = get_employee(session, employee_id)
    content_type = storage_module.ensure_image_type(mime_type)
    file_key = storage.new_key(employee.id, f"photo.{_image_extension(content_type)}")
    return storage.presign_upload(file_key, content_type)


def complete_photo_upload(
    session: Session,
    employee_id: uuid.UUID,
    *,
    file_key: str,
    mime_type: str,
    storage: ObjectStorage,
    context: AuditContext,
) -> Employee:
    """Verify an uploaded image and set it as the caller's own profile photo (Requirement 3.1, 4.2).

    The verification is the point: the declared type is re-checked as an image (a PDF completion is
    refused here even if a URL had somehow been obtained for it), then `storage.verify_upload` reads
    the stored bytes back and confirms the object exists, is within the 10 MB cap, and really is its
    declared image type by magic number. Only then is `photo_key` set and the change audited under
    reason ``photo_uploaded``. A verification failure raises a storage error the router maps to a 4xx
    and leaves the employee's existing photo untouched.

    Replacing a photo deletes the previous object best-effort: the row is the record of truth, so a
    storage hiccup on the old key must not block the new photo being recorded.
    """
    employee = get_employee(session, employee_id)
    declared = storage_module.ensure_image_type(mime_type)
    verified = storage.verify_upload(file_key, declared)

    previous_key = employee.photo_key
    before = snapshot(employee, fields=["photo_key"])
    employee.photo_key = verified.file_key
    session.flush()
    record_model_changes(
        session, employee, before, context=context, reason="photo_uploaded", fields=["photo_key"]
    )

    # Remove the object the photo replaced, if any, and only if it actually changed. Best-effort: the
    # `photo_key` on the row is what matters, and a failure to delete the old bytes must not fail the
    # request. A missing `delete` on the storage double is tolerated for the same reason.
    if previous_key and previous_key != verified.file_key:
        delete = getattr(storage, "delete", None)
        if callable(delete):
            with contextlib.suppress(Exception):
                delete(previous_key)

    return employee


def photo_download_url(
    session: Session,
    employee_id: uuid.UUID,
    *,
    storage: ObjectStorage,
) -> str | None:
    """A short-lived signed GET URL for the caller's own photo, or ``None`` if they have none.

    The mobile screen renders the URL as an ``<img>`` and a ``None`` as a placeholder. Like every
    signed URL, minting it is the grant, so the caller's ownership must have been established before
    this is reached — the router resolves the employee from the caller, so the URL is always for the
    caller's own photo.
    """
    employee = get_employee(session, employee_id)
    if not employee.photo_key:
        return None
    return storage.presign_download(employee.photo_key)


def _image_extension(content_type: str) -> str:
    """The file extension for a stored photo, from its verified image content type."""
    return "png" if content_type == "image/png" else "jpg"
