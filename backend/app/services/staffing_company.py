"""Staffing-company records: create, list, view, edit, and a guarded delete (Requirement 1, 3).

The service owns every business rule; the router only translates. Nothing here raises an HTTP error or
commits — the caller's unit of work decides the fate of the change and its audit rows together, the
invariant `app.services.audit` rests on. This mirrors `app.services.site` and `app.services.client`
exactly, so the three read the same way.

One rule carries the weight. **A staffing company may not be deleted while an active employee is linked
to it (Requirement 3).** The guard runs in application code: it queries the employees whose
`staffing_company_id` is this company and whose status is not terminated, and if any exist it refuses
the delete and names them, so an administrator knows exactly which people to deactivate first. A
terminated employee does not block the delete — the guard filters them out — so a company all of whose
workers are off the books can be removed. There is no reliance on a database `ON DELETE` rule for the
user-facing refusal, because a foreign-key violation can never *name* the blocking employees.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.employee import Employee, EmployeeStatus
from app.models.staffing_company import StaffingCompany
from app.schemas.staffing_company import StaffingCompanyCreate, StaffingCompanyUpdate
from app.services.audit import AuditContext, record_model_changes, snapshot

# --------------------------------------------------------------------------- errors
# Domain errors, not HTTP errors, each carrying a machine `code` the router lifts into the error
# envelope — the same shape `app.services.site` and `app.services.client` use.


class StaffingCompanyError(Exception):
    """Base for every staffing-company failure. `code` is what the front end translates."""

    code = "staffing_company_error"


class StaffingCompanyNotFound(StaffingCompanyError):
    code = "staffing_company_not_found"

    def __init__(self, company_id: uuid.UUID) -> None:
        super().__init__(f"no staffing company {company_id}")
        self.company_id = company_id


@dataclass(frozen=True, slots=True)
class LinkedEmployee:
    """A minimal employee identity carried on a delete refusal, so the router can name the blockers."""

    id: uuid.UUID
    full_name: str


class StaffingCompanyHasActiveEmployees(StaffingCompanyError):
    """A delete was refused because active employees are still linked (Requirement 3.1, 3.2).

    Carries the active linked employees so the router can name each one, which the requirement asks for
    and a bare foreign-key violation could never supply.
    """

    code = "staffing_company_has_active_employees"

    def __init__(self, company_id: uuid.UUID, employees: Sequence[LinkedEmployee]) -> None:
        super().__init__(f"staffing company {company_id} still has {len(employees)} active employee(s)")
        self.company_id = company_id
        self.employees = list(employees)


# --------------------------------------------------------------------------- reads


def _base_select() -> Select[tuple[StaffingCompany]]:
    return select(StaffingCompany)


def get_staffing_company(session: Session, company_id: uuid.UUID) -> StaffingCompany:
    """Load one staffing company, or raise `StaffingCompanyNotFound`."""
    company = session.scalars(_base_select().where(StaffingCompany.id == company_id)).one_or_none()
    if company is None:
        raise StaffingCompanyNotFound(company_id)
    return company


@dataclass(frozen=True, slots=True)
class StaffingCompanyPage:
    """A page of staffing companies plus the unfiltered-by-paging total, for list rendering."""

    items: Sequence[StaffingCompany]
    total: int


def list_staffing_companies(
    session: Session, *, limit: int = 50, offset: int = 0
) -> StaffingCompanyPage:
    """A stable-sorted page of staffing companies (Requirement 1.9).

    Sort is `(name, id)` so the order is total and does not shift between pages when two companies share
    a name. Returns an empty page when none exist.
    """
    statement = _base_select()
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    ordered = statement.order_by(StaffingCompany.name, StaffingCompany.id).limit(limit).offset(offset)
    items = list(session.scalars(ordered))
    return StaffingCompanyPage(items=items, total=total)


# --------------------------------------------------------------------------- create / update


def create_staffing_company(
    session: Session, payload: StaffingCompanyCreate, *, context: AuditContext
) -> StaffingCompany:
    """Create a staffing company and an audit row per field (Requirement 1.3, 5.4)."""
    company = StaffingCompany(
        name=payload.name,
        contact_person=payload.contact_person,
        hourly_rate=payload.hourly_rate,
        telephone=payload.telephone,
        comments=payload.comments,
    )
    session.add(company)
    # Flush so the row has an id the audit rows can reference.
    session.flush()

    empty = dict.fromkeys(snapshot(company), None)
    record_model_changes(session, company, empty, context=context, reason="staffing_company_created")
    return company


_UPDATABLE_FIELDS = ("name", "contact_person", "hourly_rate", "telephone", "comments")


def update_staffing_company(
    session: Session, company_id: uuid.UUID, payload: StaffingCompanyUpdate, *, context: AuditContext
) -> StaffingCompany:
    """Apply a partial update (Requirement 1.11).

    Only the fields present in the request are touched (`exclude_unset`), so a patch that names one
    field leaves the rest alone, and only the fields that actually moved produce an audit row.
    """
    company = get_staffing_company(session, company_id)
    changes = payload.model_dump(exclude_unset=True)

    before = snapshot(company)
    for field in _UPDATABLE_FIELDS:
        if field in changes:
            setattr(company, field, changes[field])

    session.flush()
    record_model_changes(session, company, before, context=context)
    return company


# --------------------------------------------------------------------------- delete (guarded)


def _active_linked_employees(session: Session, company_id: uuid.UUID) -> list[LinkedEmployee]:
    """The non-terminated employees linked to this company, newest-name-first for a stable message."""
    rows = session.execute(
        select(Employee.id, Employee.full_name)
        .where(Employee.staffing_company_id == company_id)
        .where(Employee.status != EmployeeStatus.TERMINATED)
        .order_by(Employee.full_name, Employee.id)
    ).all()
    return [LinkedEmployee(id=row[0], full_name=row[1]) for row in rows]


def delete_staffing_company(session: Session, company_id: uuid.UUID, *, context: AuditContext) -> None:
    """Delete a staffing company, refused while an active employee is linked (Requirement 3).

    Loads the company (raising `StaffingCompanyNotFound` for an unknown id), then queries the active
    linked employees; if any exist it raises `StaffingCompanyHasActiveEmployees` carrying them, so the
    router can name each one and the administrator can deactivate them first. Only when no active
    employee remains linked is the row removed; an audit row records the removal so even a clean delete
    is traceable. Terminated employees do not block the delete — the guard filters them out.
    """
    company = get_staffing_company(session, company_id)
    active = _active_linked_employees(session, company_id)
    if active:
        raise StaffingCompanyHasActiveEmployees(company_id, active)

    before = snapshot(company)
    empty = dict.fromkeys(before, None)
    # One audit row per field, recording the values as they were before the row disappears.
    record_model_changes(session, company, empty, context=context, reason="staffing_company_deleted")

    session.delete(company)
    session.flush()