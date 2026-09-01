"""Role-based authorization at the HTTP boundary (Requirement 2).

Requirement 2.9 says authorization is enforced server-side on every endpoint, so the claims worth
testing are claims about requests: what each role gets from each group of endpoints, what a site
manager receives in the body, and what lands in the audit log when a request is refused.

No employee, site, payroll or time-entry router exists yet — they arrive with Milestones 2 to 6. So
this mounts one probe endpoint per *endpoint group*, each guarded exactly as the real endpoints in
that group will be, and asserts the matrix against those. That is deliberate and it is the same
approach `test_2fa_enrolment_guard.py` takes: the subject under test is the guard, not whichever
endpoint happens to land first, and the real routers inherit this behaviour by asking for the same
dependency types. Where a router later needs a different guard from its group, the matrix here is what
says which policy it is departing from.

The probe endpoints are written the way the real ones are meant to be — guard in the signature, site
check as one call, redaction at the return — so they double as the worked example of the intended
usage.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pyotp
import pytest
from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import Session

# Imported at module level, not inside the fixture: `from __future__ import annotations` makes every
# annotation a string, and FastAPI resolves a dependency annotation against the module's globals.
from app.api.deps import (
    CODE_INSUFFICIENT_ROLE,
    CODE_NOT_OWN_RECORD,
    CODE_SITE_OUT_OF_SCOPE,
    AttendanceWriterCaller,
    EmployeeCaller,
    FinanceCaller,
    HoursReaderCaller,
    PersonnelReaderCaller,
)
from app.core.authz import (
    ATTENDANCE_WRITE_ROLES,
    FINANCE_ROLES,
    HOURS_READ_ROLES,
    PERSONNEL_READ_ROLES,
)
from app.models.change_log import ChangeLog
from app.models.user import User, UserRole
from app.models.user_site import UserSite
from app.services.authz import (
    AUDIT_FIELD,
    REASON_NOT_OWN_RECORD,
    REASON_ROLE_NOT_PERMITTED,
    REASON_SITE_OUT_OF_SCOPE,
)
from auth_support import DEFAULT_PASSWORD
from sample_models import SampleSiteScoped

ALL_ROLES = list(UserRole)

#: The employee card as a serialiser would hand it over, wage fields and all. What a site manager gets
#: back is the same payload minus everything Requirement 2.5 withholds.
EMPLOYEE_PAYLOAD: dict[str, object] = {
    "id": "11111111-1111-1111-1111-111111111111",
    "full_name": "Ahmed Khalil",
    "position": "Foreman",
    "hourly_wage": "35.00",
    "overtime_rate": "43.75",
    "rates": [{"effective_from": "2025-01-01", "hourly_wage": "32.00"}],
    "site": {"name": "Ramat Gan Tower", "billing_rate": "60.00"},
}

WAGE_KEYS = ("hourly_wage", "overtime_rate", "rates")


# --------------------------------------------------------------------------- probe endpoints

probe = APIRouter(prefix="/api/probe", tags=["probe"])


@probe.get("/employees")
def list_employees(caller: PersonnelReaderCaller) -> dict[str, str]:
    """Employee list. Requirement 2.4 read access, denied to the employee role by 2.7."""
    return {"role": caller.role.value}


@probe.get("/sites/{site_id}/employee")
def read_employee_at_site(site_id: uuid.UUID, caller: PersonnelReaderCaller) -> dict[str, object]:
    """One employee at one site: the site check and the redaction in the shape they are meant to take."""
    caller.require_site(site_id)
    return caller.redact(EMPLOYEE_PAYLOAD)


@probe.get("/site-records")
def list_site_records(caller: HoursReaderCaller) -> dict[str, list[str]]:
    """A list endpoint scoped in the query, standing in for `GET /api/time-entries`."""
    statement = caller.scope_query(select(SampleSiteScoped), SampleSiteScoped.site_id)
    return {"labels": sorted(row.label for row in caller.session.scalars(statement))}


@probe.post("/time-entries")
def create_time_entry(site_id: uuid.UUID, caller: AttendanceWriterCaller) -> dict[str, bool]:
    """A manual entry: managers and admins only (2.4), and only at a site in scope (2.3)."""
    caller.require_site(site_id)
    return {"created": True}


@probe.get("/payroll")
def read_payroll(caller: FinanceCaller) -> dict[str, str]:
    """Payroll: closed to a site manager outright (2.5), not merely redacted."""
    return {"gross_pay": "7420.00"}


@probe.get("/my-scans")
def read_my_scans(employee_id: uuid.UUID, caller: EmployeeCaller) -> dict[str, str]:
    """An employee-facing endpoint. Requirement 2.7: their own record and nothing else."""
    caller.require_own_employee_record(employee_id)
    return {"employee_id": str(employee_id)}


#: Endpoint group → method, path, and the roles the requirements admit. The administrator is permitted
#: everywhere by 2.2 and is therefore not listed in any row; the matrix test adds them.
ENDPOINT_GROUPS: dict[str, tuple[str, str, frozenset[UserRole]]] = {
    "employees_read": ("GET", "/api/probe/employees", PERSONNEL_READ_ROLES),
    "hours_read": ("GET", "/api/probe/site-records", HOURS_READ_ROLES),
    "attendance_write": ("POST", "/api/probe/time-entries", ATTENDANCE_WRITE_ROLES),
    "payroll_read": ("GET", "/api/probe/payroll", FINANCE_ROLES),
    "own_scans": ("GET", "/api/probe/my-scans", frozenset({UserRole.EMPLOYEE})),
}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def site_id() -> uuid.UUID:
    """The site every site manager in these tests is assigned to."""
    return uuid.uuid4()


@pytest.fixture
def other_site_id() -> uuid.UUID:
    """A site belonging to somebody else. Nobody in these tests is assigned to it."""
    return uuid.uuid4()


@pytest.fixture
def authz_client(settings, session: Session) -> Iterator:
    """Test client for an application carrying one probe endpoint per endpoint group."""
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.include_router(probe)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(authz_client, make_user, session: Session, site_id: uuid.UUID):
    """Create a user of a role, sign them in, and return the user with their auth header.

    Two roles need setting up before they can do anything, and both are policy rather than convenience:
    an administrator must have completed 2FA enrolment or every endpoint answers 403 (Requirement 1.6),
    and a site manager with no `user_sites` row has an empty scope and can read nothing (2.3). The
    site-manager assignment is to `site_id`, so `other_site_id` is always out of scope.
    """

    def _sign_in(role: UserRole, *, assign_site: bool = True, **overrides) -> tuple[User, dict[str, str]]:
        payload = {"username": f"{role.value}-{uuid.uuid4().hex[:8]}", "password": DEFAULT_PASSWORD}

        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        elif role is UserRole.EMPLOYEE:
            overrides.setdefault("employee_id", uuid.uuid4())
            user = make_user(role=role, **overrides)
        else:
            user = make_user(role=role, **overrides)

        if role is UserRole.SITE_MANAGER and assign_site:
            session.add(UserSite(user_id=user.id, site_id=site_id))
            session.commit()

        payload["username"] = user.username
        response = authz_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        return user, {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _sign_in


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


def _denials(session: Session) -> list[ChangeLog]:
    session.expire_all()
    return list(session.scalars(select(ChangeLog).where(ChangeLog.field == AUDIT_FIELD)))


def _params(group: str, user: User, site_id: uuid.UUID) -> dict[str, str]:
    """Whatever the probe endpoint for `group` needs in its query string."""
    match group:
        case "attendance_write":
            return {"site_id": str(site_id)}
        case "own_scans":
            return {"employee_id": str(user.employee_id or uuid.uuid4())}
        case _:
            return {}


# --------------------------------------------------------------------------- the matrix


@pytest.mark.parametrize("group", list(ENDPOINT_GROUPS))
@pytest.mark.parametrize("role", ALL_ROLES)
def test_each_role_gets_the_access_its_requirement_describes(
    authz_client, sign_in, site_id: uuid.UUID, role: UserRole, group: str
):
    """Requirement 2.2 to 2.7, one cell at a time.

    Every cell is asserted, permitted and refused alike. A matrix that only checked the permitted half
    would pass against an endpoint that let everybody in.
    """
    method, path, permitted_roles = ENDPOINT_GROUPS[group]
    permitted = role is UserRole.ADMIN or role in permitted_roles
    user, headers = sign_in(role)

    response = authz_client.request(method, path, headers=headers, params=_params(group, user, site_id))

    assert (response.status_code == 200) is permitted, f"{role.value} on {group}: {response.text}"
    if not permitted:
        assert response.status_code == 403
        assert _code(response) == CODE_INSUFFICIENT_ROLE


def test_an_unauthenticated_request_is_refused_before_any_role_check(authz_client):
    """The role guards sit behind authentication, not in front of it."""
    response = authz_client.get("/api/probe/employees")

    assert response.status_code == 401
    assert _code(response) == "not_authenticated"


def test_an_admin_who_has_not_enrolled_in_2fa_is_still_blocked(authz_client, make_user):
    """The role guards depend on `EnrolledUser`, so Requirement 1.6 is not bypassed by having the
    strongest role in the system."""
    user = make_user(role=UserRole.ADMIN)
    tokens = authz_client.post(
        "/api/auth/login", json={"username": user.username, "password": DEFAULT_PASSWORD}
    ).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = authz_client.get("/api/probe/employees", headers=headers)

    assert response.status_code == 403
    assert _code(response) == "totp_enrolment_required"


# --------------------------------------------------------------------------- what a site manager sees


def test_a_site_manager_reading_an_employee_gets_no_wage_fields(authz_client, sign_in, site_id):
    """Requirement 2.5, at their own site so nothing else could be causing the refusal."""
    _, headers = sign_in(UserRole.SITE_MANAGER)

    response = authz_client.get(f"/api/probe/sites/{site_id}/employee", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["full_name"] == "Ahmed Khalil"
    for key in WAGE_KEYS:
        assert key not in body


def test_a_site_manager_gets_no_billing_rate_even_nested(authz_client, sign_in, site_id):
    """Requirements 2.5 and 17.7. Nested, because that is where a rate usually travels."""
    _, headers = sign_in(UserRole.SITE_MANAGER)

    body = authz_client.get(f"/api/probe/sites/{site_id}/employee", headers=headers).json()

    assert body["site"] == {"name": "Ramat Gan Tower"}


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.ACCOUNTING])
def test_a_finance_role_reading_the_same_employee_gets_the_wage_fields(
    authz_client, sign_in, site_id, role: UserRole
):
    """The other half of the redaction claim. Without this the test would pass against an endpoint
    that served nobody the wage."""
    _, headers = sign_in(role)

    body = authz_client.get(f"/api/probe/sites/{site_id}/employee", headers=headers).json()

    assert body["hourly_wage"] == "35.00"
    assert body["site"]["billing_rate"] == "60.00"


def test_a_site_manager_cannot_read_another_managers_site(authz_client, sign_in, other_site_id):
    """Requirement 2.3."""
    _, headers = sign_in(UserRole.SITE_MANAGER)

    response = authz_client.get(f"/api/probe/sites/{other_site_id}/employee", headers=headers)

    assert response.status_code == 403
    assert _code(response) == CODE_SITE_OUT_OF_SCOPE


def test_a_site_manager_cannot_write_to_a_site_they_are_not_assigned(authz_client, sign_in, other_site_id):
    """Requirement 2.3's second clause: reads are restricted *and* writes are rejected."""
    _, headers = sign_in(UserRole.SITE_MANAGER)

    response = authz_client.post(
        "/api/probe/time-entries", headers=headers, params={"site_id": str(other_site_id)}
    )

    assert response.status_code == 403
    assert _code(response) == CODE_SITE_OUT_OF_SCOPE


def test_an_admin_may_read_any_site(authz_client, sign_in, other_site_id):
    """Requirement 2.2. The site nobody is assigned to is still theirs to read."""
    _, headers = sign_in(UserRole.ADMIN)

    assert authz_client.get(f"/api/probe/sites/{other_site_id}/employee", headers=headers).status_code == 200


# --------------------------------------------------------------------------- list scoping


def test_a_list_endpoint_returns_only_the_managers_sites(
    authz_client, sign_in, session: Session, site_id, other_site_id
):
    """Requirement 2.3. The filtering happens in the query — see `test_authz_policy.py` for that
    claim; here what matters is that the endpoint consults the scope at all."""
    session.add_all(
        [
            SampleSiteScoped(site_id=site_id, label="mine"),
            SampleSiteScoped(site_id=other_site_id, label="theirs"),
        ]
    )
    session.commit()
    _, headers = sign_in(UserRole.SITE_MANAGER)

    response = authz_client.get("/api/probe/site-records", headers=headers)

    assert response.json() == {"labels": ["mine"]}


def test_a_manager_with_no_assignment_sees_nothing_rather_than_everything(
    authz_client, sign_in, session: Session, site_id
):
    """The failure mode this exists for: an empty filter list treated as no filter."""
    session.add(SampleSiteScoped(site_id=site_id, label="mine"))
    session.commit()
    _, headers = sign_in(UserRole.SITE_MANAGER, assign_site=False)

    response = authz_client.get("/api/probe/site-records", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"labels": []}


def test_an_unrestricted_role_sees_every_site(
    authz_client, sign_in, session: Session, site_id, other_site_id
):
    session.add_all(
        [
            SampleSiteScoped(site_id=site_id, label="mine"),
            SampleSiteScoped(site_id=other_site_id, label="theirs"),
        ]
    )
    session.commit()
    _, headers = sign_in(UserRole.ACCOUNTING)

    assert authz_client.get("/api/probe/site-records", headers=headers).json() == {
        "labels": ["mine", "theirs"]
    }


# --------------------------------------------------------------------------- own-record access


def test_an_employee_reads_their_own_record(authz_client, sign_in):
    """Requirement 2.7."""
    user, headers = sign_in(UserRole.EMPLOYEE)

    response = authz_client.get(
        "/api/probe/my-scans", headers=headers, params={"employee_id": str(user.employee_id)}
    )

    assert response.status_code == 200
    assert response.json() == {"employee_id": str(user.employee_id)}


def test_an_employee_cannot_read_another_employees_record(authz_client, sign_in):
    _, headers = sign_in(UserRole.EMPLOYEE)

    response = authz_client.get(
        "/api/probe/my-scans", headers=headers, params={"employee_id": str(uuid.uuid4())}
    )

    assert response.status_code == 403
    assert _code(response) == CODE_NOT_OWN_RECORD


# --------------------------------------------------------------------------- denials are audited
# Requirement 2.8. The attempt has to be recorded, and a record that vanishes because the request
# failed is not a record — which is why `record_denial` commits.


def test_a_role_refusal_is_written_to_the_audit_log(authz_client, sign_in, session: Session):
    _, headers = sign_in(UserRole.SITE_MANAGER)
    authz_client.get("/api/probe/payroll", headers=headers)

    denials = _denials(session)
    assert len(denials) == 1
    assert denials[0].reason == REASON_ROLE_NOT_PERMITTED
    assert "GET /api/probe/payroll" in denials[0].new_value


def test_a_refusal_is_attributed_to_the_caller(authz_client, sign_in, session: Session):
    """Attributed to the user, on the `users` entity, so the audit view renders one stream per person
    alongside their authentication events."""
    user, headers = sign_in(UserRole.SITE_MANAGER)
    authz_client.get("/api/probe/payroll", headers=headers)

    denial = _denials(session)[0]
    assert denial.entity_type == "users"
    assert denial.entity_id == user.id
    assert denial.changed_by_user_id == user.id


def test_an_out_of_scope_site_refusal_names_the_site(authz_client, sign_in, session: Session, other_site_id):
    _, headers = sign_in(UserRole.SITE_MANAGER)
    authz_client.get(f"/api/probe/sites/{other_site_id}/employee", headers=headers)

    denial = _denials(session)[0]
    assert denial.reason == REASON_SITE_OUT_OF_SCOPE
    assert str(other_site_id) in denial.new_value
    # The route template, not the resolved path, so denials on one endpoint group together.
    assert "/api/probe/sites/{site_id}/employee" in denial.new_value


def test_an_own_record_refusal_is_audited(authz_client, sign_in, session: Session):
    _, headers = sign_in(UserRole.EMPLOYEE)
    authz_client.get("/api/probe/my-scans", headers=headers, params={"employee_id": str(uuid.uuid4())})

    assert [denial.reason for denial in _denials(session)] == [REASON_NOT_OWN_RECORD]


def test_a_permitted_request_writes_no_denial(authz_client, sign_in, session: Session, site_id):
    """So the audit log is worth reading: a table with a row for every successful request would bury
    the refusals it exists to surface."""
    _, headers = sign_in(UserRole.SITE_MANAGER)
    assert authz_client.get(f"/api/probe/sites/{site_id}/employee", headers=headers).status_code == 200

    assert _denials(session) == []
