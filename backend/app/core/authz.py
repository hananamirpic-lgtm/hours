"""Authorization policy. Pure: no session, no request, no HTTP.

Requirement 2 in one place. Everything here is a statement about what a role may see or do, expressed
as data or as a function of values, so the policy can be read in one sitting and tested without a
database. The machinery that applies it lives elsewhere: `app.services.authz` turns a user into a site
scope and folds that scope into a query, and `app.api.deps` turns a refusal into a 403.

Three decisions worth reading before changing anything.

**Scope is a value, not a boolean.** `SiteScope` says either "every site" or "exactly these ids",
which is what lets a list endpoint push the restriction into its `WHERE` clause. The alternative —
a `can_read(site)` predicate — reads well and is wrong at scale and in correctness: filtering after
the fetch pages over rows the caller may not see, so page one of a manager's list can legitimately
come back empty while later pages hold their data (Requirement 22.5 asks for stable pagination, and
post-filtering cannot give it).

**Money fields are named, not inferred.** Requirement 2.5 forbids a site manager the wage rates,
payroll records, site billing rates and client billing. That is a list of fields, and it is written
here as a list of field names rather than left to each schema to remember, because the schema that
forgets is the one that leaks. `redact` walks a serialised payload and removes them, so a nested
`rates` block is covered by the same rule as a top-level column.

**Redaction removes rather than nulls.** A wage of `null` is indistinguishable from a wage that is
genuinely unset, so a front end cannot tell "you may not see this" from "there is nothing to see", and
a site manager would be shown an empty wage field they are able to type into. Absent means absent.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.models.user import UserRole

# --------------------------------------------------------------------------- role sets
# Named sets rather than role comparisons scattered through routers. Each cites the criterion it
# encodes, so a reader can check the policy against the requirement without reading the endpoints.

#: Requirement 2.2. Unrestricted access to every resource.
ROLES_WITH_FULL_ACCESS = frozenset({UserRole.ADMIN})

#: Roles whose data is not narrowed to a site assignment. Administrators by 2.2; accounting because
#: 2.6 gives them payroll, billing and reports for the business, which are whole-company figures and
#: cannot be assembled from a subset of sites. A site manager is absent by 2.3, and an employee is
#: absent because their scope is their own record (2.7), which is narrower than any site.
ROLES_WITH_ALL_SITES = frozenset({UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.ACCOUNTING})

#: Requirement 2.3: a site manager sees and writes only their assigned sites. The set of ids comes
#: from `user_sites` — see `app.services.authz.resolve_site_scope`.
ROLES_SCOPED_TO_ASSIGNED_SITES = frozenset({UserRole.SITE_MANAGER})

#: Requirements 2.5 and 17.7. Who may see wage rates, payroll records, site billing rates and client
#: billing. The site manager's exclusion is the point of the set; the employee's is a consequence of
#: 2.7, and their own wage is not exposed through these endpoints either. `operations_admin` is
#: deliberately absent: it is a full operational administrator with no financial visibility, so the
#: same redaction and finance-endpoint gating that hides money from a site manager hides it from this
#: role too. This one exclusion is what makes the whole "admin without money" role work.
FINANCE_ROLES = frozenset({UserRole.ADMIN, UserRole.ACCOUNTING})

#: Requirement 2.4 and the prohibition in 2.6: managers and administrators create and correct time
#: entries, accounting does not. Read access to hours is wider than this — see `HOURS_READ_ROLES`.
ATTENDANCE_WRITE_ROLES = frozenset({UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.SITE_MANAGER})

#: Requirements 2.4 and 2.6. Accounting reads approved hours; a manager reads the hours of their
#: sites. An employee reads their own entries, which is a different question — see `is_own_record`.
HOURS_READ_ROLES = frozenset(
    {UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.SITE_MANAGER, UserRole.ACCOUNTING}
)

#: Who may read employee records at all. Accounting is included because payroll is computed per
#: person; what they may see *of* a record is a separate question, answered by `FINANCE_ROLES` and
#: the redaction below.
PERSONNEL_READ_ROLES = frozenset(
    {UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.SITE_MANAGER, UserRole.ACCOUNTING}
)

#: Requirement 2.7. The employee role acts on its own record and nothing else, so no site scope and
#: no role set grants it anything; every employee-facing endpoint checks ownership instead.
OWN_RECORD_ONLY_ROLES = frozenset({UserRole.EMPLOYEE})


# --------------------------------------------------------------------------- restricted fields

#: Employee pay. Requirement 2.5 first clause.
WAGE_FIELDS = frozenset(
    {
        "hourly_wage",
        "overtime_rate",
        "shabbat_holiday_rate",
        "travel_allowance_daily",
        "rates",
    }
)

#: What a site's work is sold for, and what it earns. Requirements 2.5 and 17.7. `profit` and
#: `margin` are here because they are billing minus cost: publishing either to a site manager
#: discloses both of the numbers they may not see.
BILLING_FIELDS = frozenset(
    {
        "billing_rate",
        "overtime_billing_rate",
        "site_rates",
        "billing_amount",
        "billing_total",
        "profit",
        "profit_amount",
        "margin",
        # A staffing company's single flat rate and the payment a report computes from it. Both are
        # money the operations administrator must not see (added with the staffing-company feature).
        "hourly_rate",
        "total_payment",
    }
)

#: Computed pay. Requirement 2.5 second clause. Payroll *endpoints* are closed to a site manager
#: outright; these names cover a payroll figure reached through some other response.
PAYROLL_FIELDS = frozenset(
    {
        "gross_pay",
        "net_pay",
        "total_pay",
        "employee_cost",
        "allocated_cost",
        "deductions",
        "bonuses",
        "payroll",
    }
)

#: Every field name a role outside `FINANCE_ROLES` must not receive.
RESTRICTED_MONEY_FIELDS = WAGE_FIELDS | BILLING_FIELDS | PAYROLL_FIELDS


def may_read_money_fields(role: UserRole) -> bool:
    """Whether `role` may receive wage, payroll and billing figures (Requirement 2.5)."""
    return role in FINANCE_ROLES


def restricted_fields_for(role: UserRole) -> frozenset[str]:
    """Field names to strip from a response served to `role`. Empty for a finance role."""
    return frozenset() if may_read_money_fields(role) else RESTRICTED_MONEY_FIELDS


def redact(payload: Any, role: UserRole) -> Any:
    """Return `payload` with every field `role` may not see removed.

    Walks nested mappings and sequences, because the fields in question are as likely to arrive
    inside a `rates` list as at the top level. Returns a new structure and does not mutate the input:
    a response model is often the same object a service still holds a reference to.

    Removal rather than nulling — see the module note. A finance role gets the payload back
    unchanged, which keeps the call safe to make unconditionally at the serialisation boundary
    rather than only where someone remembered a site manager might be reading.
    """
    return _strip(payload, restricted_fields_for(role))


def _strip(value: Any, fields: frozenset[str]) -> Any:
    if not fields:
        return value
    if isinstance(value, Mapping):
        return {key: _strip(item, fields) for key, item in value.items() if key not in fields}
    # `str` and `bytes` are sequences and must not be walked as ones.
    if isinstance(value, list | tuple):
        stripped = [_strip(item, fields) for item in value]
        return type(value)(stripped) if isinstance(value, tuple) else stripped
    return value


# --------------------------------------------------------------------------- site scope


@dataclass(frozen=True, slots=True)
class SiteScope:
    """The set of sites a caller may read and write.

    Either unrestricted, or an explicit set of ids. An unrestricted scope is not "the set of all
    sites": enumerating every site to say "all" would mean a manager's grant and an administrator's
    lack of one were represented the same way, and a query built from that set would silently narrow
    itself whenever a new site was created between the enumeration and the query.

    An empty restricted scope is a real state — a site manager with no assignment yet — and it means
    "nothing", not "everything". `apply_site_scope` turns it into a `WHERE false`, which is the only
    reading that is safe: the alternative bug, where an empty filter list drops the filter, is how a
    manager with no sites ends up seeing all of them.
    """

    unrestricted: bool
    site_ids: frozenset[uuid.UUID]

    @classmethod
    def all_sites(cls) -> SiteScope:
        return cls(unrestricted=True, site_ids=frozenset())

    @classmethod
    def limited_to(cls, site_ids: Iterable[uuid.UUID]) -> SiteScope:
        return cls(unrestricted=False, site_ids=frozenset(site_ids))

    @classmethod
    def nothing(cls) -> SiteScope:
        """No site at all: the employee role, whose scope is their own record instead."""
        return cls(unrestricted=False, site_ids=frozenset())

    @property
    def is_empty(self) -> bool:
        """Whether this scope permits no site whatsoever."""
        return not self.unrestricted and not self.site_ids

    def allows(self, site_id: uuid.UUID | None) -> bool:
        """Whether a single site is inside the scope.

        `None` is refused rather than waved through. A missing site id at a permission check means the
        caller could not say what they were asking for, and defaulting that to "allowed" is the shape
        of bug that only shows up in the audit log afterwards.
        """
        if site_id is None:
            return False
        return self.unrestricted or site_id in self.site_ids


def scope_for_role(role: UserRole, assigned_site_ids: Iterable[uuid.UUID]) -> SiteScope:
    """The scope a role has, given whatever site assignments the caller holds.

    Pure, so the policy is testable without a database; `app.services.authz.resolve_site_scope` reads
    the assignments and calls this.
    """
    if role in ROLES_WITH_ALL_SITES:
        return SiteScope.all_sites()
    if role in ROLES_SCOPED_TO_ASSIGNED_SITES:
        return SiteScope.limited_to(assigned_site_ids)
    return SiteScope.nothing()


def is_own_record(*, role: UserRole, caller_employee_id: uuid.UUID | None, employee_id: uuid.UUID) -> bool:
    """Whether an employee-role caller is asking about themselves (Requirement 2.7).

    False when the caller has no linked employee record, which is the state of an employee login that
    was created without one. There is nothing to compare, so there is nothing to permit.
    """
    if role not in OWN_RECORD_ONLY_ROLES:
        return True
    return caller_employee_id is not None and caller_employee_id == employee_id
