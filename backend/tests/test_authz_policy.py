"""The authorization policy itself (Requirement 2), tested without a database or a request.

`app.core.authz` is pure on purpose, and this is the payoff: every claim the requirements make about
what a role may see can be checked as a statement about values. The API-level matrix in
`test_authorization_matrix.py` then checks that the endpoints actually consult it.

The scoping assertions are made against a real SQLAlchemy statement rather than against a filtered
list, because "the restriction is in the WHERE clause" is the claim — a test that compared result
lists would pass just as happily against the post-fetch filtering the design rules out.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.authz import (
    ATTENDANCE_WRITE_ROLES,
    BILLING_FIELDS,
    FINANCE_ROLES,
    PAYROLL_FIELDS,
    RESTRICTED_MONEY_FIELDS,
    WAGE_FIELDS,
    SiteScope,
    is_own_record,
    may_read_money_fields,
    redact,
    scope_for_role,
)
from app.models.user import UserRole
from app.models.user_site import UserSite
from app.services.authz import apply_site_scope, assigned_site_ids, resolve_site_scope
from sample_models import SampleSiteScoped

ALL_ROLES = list(UserRole)


# --------------------------------------------------------------------------- the four roles


def test_there_are_exactly_five_roles():
    """Requirement 2.1 plus the operations-admin feature, asserted so that adding another role is a
    deliberate decision and not an accident. `operations_admin` is the full operational administrator
    with no financial visibility."""
    assert {role.value for role in UserRole} == {
        "admin",
        "operations_admin",
        "site_manager",
        "accounting",
        "employee",
    }


# --------------------------------------------------------------------------- money-field visibility


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.ACCOUNTING])
def test_a_finance_role_may_read_money_fields(role: UserRole):
    """Requirement 2.6: accounting reads payroll and billing; 2.2 gives the administrator everything."""
    assert may_read_money_fields(role)


@pytest.mark.parametrize(
    "role", [UserRole.OPERATIONS_ADMIN, UserRole.SITE_MANAGER, UserRole.EMPLOYEE]
)
def test_a_non_finance_role_may_not_read_money_fields(role: UserRole):
    """Requirement 2.5. The site manager's exclusion is the one the requirement is about."""
    assert not may_read_money_fields(role)


def test_the_restricted_set_covers_wages_payroll_and_billing():
    """The three clauses of Requirement 2.5, each contributing to one set."""
    assert WAGE_FIELDS <= RESTRICTED_MONEY_FIELDS
    assert PAYROLL_FIELDS <= RESTRICTED_MONEY_FIELDS
    assert BILLING_FIELDS <= RESTRICTED_MONEY_FIELDS
    assert {"hourly_wage", "billing_rate", "gross_pay"} <= RESTRICTED_MONEY_FIELDS


# --------------------------------------------------------------------------- redaction


def test_redaction_removes_wage_fields_for_a_site_manager():
    payload = {"id": "e1", "full_name": "Ahmed", "hourly_wage": "35.00", "position": "Foreman"}

    assert redact(payload, UserRole.SITE_MANAGER) == {
        "id": "e1",
        "full_name": "Ahmed",
        "position": "Foreman",
    }


def test_redaction_removes_rather_than_nulls():
    """A wage of `null` cannot be told apart from a wage that is genuinely unset, so a front end would
    show a site manager an empty wage field they can type into. Absent means absent."""
    redacted = redact({"hourly_wage": "35.00"}, UserRole.SITE_MANAGER)

    assert "hourly_wage" not in redacted


def test_redaction_reaches_nested_structures():
    """The fields in question are as likely to arrive inside a rates list as at the top level."""
    payload = {
        "employee": {"full_name": "Ahmed", "hourly_wage": "35.00"},
        "rates_history": [{"effective_from": "2025-01-01", "overtime_rate": "43.75"}],
    }

    assert redact(payload, UserRole.SITE_MANAGER) == {
        "employee": {"full_name": "Ahmed"},
        "rates_history": [{"effective_from": "2025-01-01"}],
    }


def test_redaction_leaves_a_finance_role_untouched():
    """So the call is safe to make unconditionally at the serialisation boundary."""
    payload = {"full_name": "Ahmed", "hourly_wage": "35.00", "billing_rate": "60.00"}

    assert redact(payload, UserRole.ACCOUNTING) == payload


def test_redaction_does_not_mutate_its_input():
    payload = {"full_name": "Ahmed", "hourly_wage": "35.00"}
    redact(payload, UserRole.SITE_MANAGER)

    assert payload["hourly_wage"] == "35.00"


def test_redaction_does_not_walk_into_strings():
    """A string is a sequence, and treating it as one would return it as a list of characters."""
    assert redact({"full_name": "Ahmed"}, UserRole.SITE_MANAGER) == {"full_name": "Ahmed"}


# --------------------------------------------------------------------------- site scope as a value


def test_an_unrestricted_scope_allows_any_site():
    assert SiteScope.all_sites().allows(uuid.uuid4())


def test_a_limited_scope_allows_only_its_sites():
    permitted, other = uuid.uuid4(), uuid.uuid4()
    scope = SiteScope.limited_to([permitted])

    assert scope.allows(permitted)
    assert not scope.allows(other)


def test_an_empty_scope_allows_nothing():
    """A site manager with no assignment yet. The bug this pins down is the one where an empty filter
    list is treated as no filter, and the manager sees every site instead of none."""
    scope = SiteScope.nothing()

    assert scope.is_empty
    assert not scope.allows(uuid.uuid4())


def test_an_unrestricted_scope_is_not_empty():
    assert not SiteScope.all_sites().is_empty


def test_a_missing_site_id_is_refused():
    """At a permission check, `None` means the caller could not say what they were asking for."""
    assert not SiteScope.all_sites().allows(None)
    assert not SiteScope.limited_to([uuid.uuid4()]).allows(None)


@pytest.mark.parametrize("role", sorted(FINANCE_ROLES))
def test_admin_and_accounting_are_scoped_to_every_site(role: UserRole):
    """Requirement 2.2, and 2.6's whole-company figures which cannot be assembled from a subset."""
    scope = scope_for_role(role, [])

    assert scope.unrestricted


def test_a_site_manager_is_scoped_to_their_assignments():
    """Requirement 2.3."""
    assigned = {uuid.uuid4(), uuid.uuid4()}
    scope = scope_for_role(UserRole.SITE_MANAGER, assigned)

    assert not scope.unrestricted
    assert scope.site_ids == assigned


def test_an_employee_has_no_site_scope():
    """Requirement 2.7: their access is to their own record, which is narrower than any site. An
    assignment lying around must not widen it."""
    scope = scope_for_role(UserRole.EMPLOYEE, [uuid.uuid4()])

    assert scope.is_empty


def test_an_attendance_writer_is_a_manager_or_an_admin():
    """Requirement 2.4 grants the manager manual entries; 2.6 denies them to accounting. The
    operations administrator has full operational access, so it writes attendance too."""
    assert {UserRole.ADMIN, UserRole.OPERATIONS_ADMIN, UserRole.SITE_MANAGER} == ATTENDANCE_WRITE_ROLES


# --------------------------------------------------------------------------- query-level scoping


def _compiled(statement) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


def test_an_unrestricted_scope_adds_no_predicate():
    """An administrator's query is the query they asked for."""
    statement = select(SampleSiteScoped)

    assert apply_site_scope(statement, SampleSiteScoped.site_id, SiteScope.all_sites()) is statement


def test_a_limited_scope_becomes_an_in_clause():
    site_id = uuid.uuid4()
    scoped = apply_site_scope(
        select(SampleSiteScoped), SampleSiteScoped.site_id, SiteScope.limited_to([site_id])
    )

    # Compared without dashes: the rendered form of a UUID literal is the dialect's business, and
    # SQLite writes it as plain hex.
    compiled = _compiled(scoped).replace("-", "")
    assert "WHERE" in compiled
    assert "IN" in compiled
    assert site_id.hex in compiled


def test_an_empty_scope_becomes_where_false():
    """Not a dropped filter. `IN ()` would behave the same way in PostgreSQL, but relying on that is
    relying on the one behaviour that, if it ever differed, would fail open."""
    scoped = apply_site_scope(select(SampleSiteScoped), SampleSiteScoped.site_id, SiteScope.nothing())

    assert "false" in _compiled(scoped).lower()


def test_scoping_filters_in_the_query_not_after_it(session: Session):
    """Executed, so the claim is about rows and not only about SQL text."""
    permitted, other = uuid.uuid4(), uuid.uuid4()
    session.add_all(
        [
            SampleSiteScoped(site_id=permitted, label="mine"),
            SampleSiteScoped(site_id=other, label="theirs"),
        ]
    )
    session.commit()

    scoped = apply_site_scope(
        select(SampleSiteScoped), SampleSiteScoped.site_id, SiteScope.limited_to([permitted])
    )

    assert [row.label for row in session.scalars(scoped)] == ["mine"]


def test_an_empty_scope_returns_no_rows(session: Session):
    session.add(SampleSiteScoped(site_id=uuid.uuid4(), label="somebody else's"))
    session.commit()

    scoped = apply_site_scope(select(SampleSiteScoped), SampleSiteScoped.site_id, SiteScope.nothing())

    assert session.scalars(scoped).all() == []


# --------------------------------------------------------------------------- resolving from the database


def test_the_scope_of_a_site_manager_comes_from_user_sites(session: Session, make_user):
    """The design says site-manager authorization reads exclusively from `user_sites`."""
    manager = make_user(role=UserRole.SITE_MANAGER)
    first, second = uuid.uuid4(), uuid.uuid4()
    session.add_all(
        [
            UserSite(user_id=manager.id, site_id=first),
            UserSite(user_id=manager.id, site_id=second),
        ]
    )
    session.commit()

    assert assigned_site_ids(session, manager) == {first, second}
    assert resolve_site_scope(session, manager).site_ids == {first, second}


def test_a_manager_with_no_assignment_resolves_to_an_empty_scope(session: Session, make_user):
    scope = resolve_site_scope(session, make_user(role=UserRole.SITE_MANAGER))

    assert scope.is_empty


def test_an_admins_assignments_do_not_narrow_their_scope(session: Session, make_user):
    """A row in `user_sites` for an administrator is not a restriction. It happens — an administrator
    who also manages a site — and reading it as a limit would quietly remove their access to
    everything else."""
    admin = make_user(role=UserRole.ADMIN)
    session.add(UserSite(user_id=admin.id, site_id=uuid.uuid4()))
    session.commit()

    assert resolve_site_scope(session, admin).unrestricted


def test_accounting_is_unrestricted(session: Session, make_user):
    assert resolve_site_scope(session, make_user(role=UserRole.ACCOUNTING)).unrestricted


# --------------------------------------------------------------------------- own-record access


def test_an_employee_may_act_on_their_own_record():
    """Requirement 2.7."""
    own = uuid.uuid4()

    assert is_own_record(role=UserRole.EMPLOYEE, caller_employee_id=own, employee_id=own)


def test_an_employee_may_not_act_on_somebody_elses_record():
    assert not is_own_record(
        role=UserRole.EMPLOYEE, caller_employee_id=uuid.uuid4(), employee_id=uuid.uuid4()
    )


def test_an_employee_login_with_no_linked_record_is_refused():
    """There is nothing to compare, so there is nothing to permit."""
    assert not is_own_record(role=UserRole.EMPLOYEE, caller_employee_id=None, employee_id=uuid.uuid4())


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.SITE_MANAGER, UserRole.ACCOUNTING])
def test_the_ownership_check_does_not_apply_to_console_roles(role: UserRole):
    """What limits them is their site scope, which is checked separately. Folding the two together
    here would make a manager's access to their own site depend on a link they do not have."""
    assert is_own_record(role=role, caller_employee_id=None, employee_id=uuid.uuid4())


# --------------------------------------------------------------------------- operations admin
# Feature: operations-admin-role. The operations administrator has full operational access and no
# financial visibility. These pin the policy-level guarantees; the endpoint-level 403s and the
# role-assignment refusal are checked in test_operations_admin_api.py.


def test_operations_admin_may_not_read_money_fields():
    """Property 1 (foundation): operations_admin is not a finance role, so money is redacted from it."""
    assert not may_read_money_fields(UserRole.OPERATIONS_ADMIN)
    assert UserRole.OPERATIONS_ADMIN not in FINANCE_ROLES


def test_operations_admin_responses_have_every_money_field_removed():
    """Property 1: redact strips every wage, billing and payroll field for an operations_admin.

    A payload that mixes operational data (a name, a site number) with money at several nesting
    depths comes back with the operational data intact and no money field anywhere.
    """
    payload = {
        "name": "North Gate",
        "site_number": "S-42",
        "billing_rate": "60.00",
        "site_rates": [{"billing_rate": "60.00", "effective_from": "2025-01-01"}],
        "employee": {
            "full_name": "Dana",
            "hourly_wage": "35.00",
            "rates": [{"overtime_rate": "52.50"}],
        },
        "payroll": {"gross_pay": "1000.00", "total_pay": "1000.00"},
    }
    result = redact(payload, UserRole.OPERATIONS_ADMIN)

    # Operational data survives.
    assert result["name"] == "North Gate"
    assert result["site_number"] == "S-42"
    assert result["employee"]["full_name"] == "Dana"

    # No money field remains, at any depth.
    flat = _all_keys(result)
    assert flat.isdisjoint(RESTRICTED_MONEY_FIELDS)
    assert flat.isdisjoint(WAGE_FIELDS | BILLING_FIELDS | PAYROLL_FIELDS)


def test_operations_admin_scope_is_all_sites():
    """Property 5: the operations administrator is not narrowed to a site assignment."""
    scope = scope_for_role(UserRole.OPERATIONS_ADMIN, assigned_site_ids=[])
    assert scope.unrestricted
    assert not scope.is_empty


def _all_keys(value: object) -> frozenset[str]:
    """Every mapping key appearing anywhere in a nested structure."""
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(key)
            keys |= _all_keys(item)
    elif isinstance(value, list | tuple):
        for item in value:
            keys |= _all_keys(item)
    return frozenset(keys)