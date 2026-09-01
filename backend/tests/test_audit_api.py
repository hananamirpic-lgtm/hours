"""The audit-history endpoint over HTTP (Requirement 13.3, 13.4, 13.6).

The claims worth an HTTP test are the ones the requirement makes: that the audit trail is append-only
and exposes no endpoint to change it (Requirement 13.3); that a change is served with its actor,
timestamp and reason so the view can render a readable line (Requirement 13.1, 13.4); and that reading
is scoped — an administrator reads any entity's audit, a site manager only entities within their
assigned sites, and the console roles below them not at all (Requirement 13.6).

The write side that produces these rows is exercised in `test_manual_entry_api.py` and the employee
and site suites; here a manual correction is driven end to end once, so the assertion that "every
time-entry change appears with actor, timestamp and reason" is against rows a real endpoint wrote, not
hand-inserted ones. The sign-in helper mirrors `test_time_entry_api.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.models.change_log import ChangeLog
from app.models.client import Client
from app.models.employee import Employee
from app.models.site import EmployeeSite, Site
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.models.user import UserRole
from app.models.user_site import UserSite
from auth_support import DEFAULT_PASSWORD


@pytest.fixture
def audit_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(audit_client, make_user):
    def _sign_in(role: UserRole, **user_overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **user_overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **user_overrides)
        payload["username"] = user.username

        response = audit_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        header = {"Authorization": f"Bearer {response.json()['access_token']}"}
        return header, user

    return _sign_in


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


_PASSPORT = iter(f"A{n:07d}" for n in range(1, 100000))


def _make_employee(session: Session, *, name: str = "Worker") -> Employee:
    passport = next(_PASSPORT)
    employee = Employee(
        full_name=name,
        full_name_en=name,
        passport_number=passport,
        passport_number_hash=passport,
        phone="+972500000000",
        country="Israel",
        emergency_contact_name="Contact",
        emergency_contact_phone="+972500000001",
        start_date=date(2025, 1, 1),
    )
    session.add(employee)
    session.flush()
    return employee


def _make_site(session: Session, *, number: str = "S-1", name: str | None = None) -> Site:
    client = Client(name="Acme")
    session.add(client)
    session.flush()
    site = Site(
        name=name or f"Site {number}",
        site_number=number,
        client_id=client.id,
        qr_token=f"placeholder-{uuid.uuid4().hex}",
        qr_token_version=1,
    )
    session.add(site)
    session.flush()
    return site


def _make_entry(
    session: Session,
    *,
    employee: Employee,
    site: Site,
    work_date: date = date(2025, 8, 30),
    status: TimeEntryStatus = TimeEntryStatus.DRAFT,
) -> TimeEntry:
    check_in = datetime(work_date.year, work_date.month, work_date.day, 8, tzinfo=UTC)
    check_out = datetime(work_date.year, work_date.month, work_date.day, 12, tzinfo=UTC)
    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=work_date,
        check_in_at=check_in,
        check_out_at=check_out,
        total_minutes=240,
        source=TimeEntrySource.QR_SCAN,
        status=status,
    )
    session.add(entry)
    session.commit()
    return entry


def _audit(session: Session, *, entity_type: str, entity_id, field: str, actor_id=None, reason=None):
    """Write one audit row directly, for the reads that do not need a real mutating endpoint."""
    row = ChangeLog(
        entity_type=entity_type,
        entity_id=entity_id,
        changed_by_user_id=actor_id,
        field=field,
        old_value="old",
        new_value="new",
        reason=reason,
    )
    session.add(row)
    session.commit()
    return row


# --------------------------------------------------------------------------- append-only (13.3)


def test_no_endpoint_mutates_audit_rows(audit_client):
    """Requirement 13.3: the audit trail is append-only — only GET is mounted, no write verb exists."""
    routes = [route for route in audit_client.app.routes if getattr(route, "path", "") == "/api/audit"]
    assert routes, "the audit route should be mounted"
    methods: set[str] = set()
    for route in routes:
        methods |= set(route.methods or set())
    # GET (and the HEAD/OPTIONS FastAPI derives from it) only. No POST, PUT, PATCH or DELETE.
    assert methods <= {"GET", "HEAD", "OPTIONS"}
    assert "GET" in methods
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert verb not in methods


def test_the_write_verbs_are_not_accepted(audit_client, sign_in, session: Session):
    """A client trying to change an audit row is refused: no write verb is routed (Requirement 13.3).

    Neither the collection (`/api/audit`) nor a per-row path (`/api/audit/{id}`) accepts a mutating
    verb. A missing route answers 404 and a wrong verb on an existing route answers 405; either proves
    the same thing — there is no way to update or delete an audit row — so both are accepted, and a
    2xx would be the failure.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    row = _audit(session, entity_type="employees", entity_id=employee.id, field="position")

    for path in ("/api/audit", f"/api/audit/{row.id}"):
        for verb in ("POST", "PUT", "PATCH", "DELETE"):
            response = audit_client.request(verb, path, headers=headers)
            assert response.status_code in (404, 405), (
                f"{verb} {path} should not mutate audit: {response.status_code}"
            )


# --------------------------------------------------------------------------- shape (13.1, 13.4)


def test_a_time_entry_change_appears_with_actor_timestamp_and_reason(
    audit_client, sign_in, session: Session
):
    """Requirement 13.1, 13.4: a correction is in the trail with who, when and why, ready to render.

    Drives a real correction through PATCH /api/time-entries so the audited rows are ones a mutating
    endpoint wrote, then reads them back through the audit endpoint. The changed check-out field is
    present, carries the reason the correction gave, is attributed to the acting user, and stamps a
    time — every part the readable line "… Abraham changed check-out from 15:30 to 16:00" is built
    from.
    """
    headers, admin = sign_in(UserRole.ADMIN)
    employee = _make_employee(session, name="Dana")
    site = _make_site(session)
    entry = _make_entry(session, employee=employee, site=site)

    correction = audit_client.patch(
        f"/api/time-entries/{entry.id}",
        headers=headers,
        json={"check_out_at": "2025-08-30T13:00:00Z", "reason": "clock drift corrected"},
    )
    assert correction.status_code == 200, correction.text

    response = audit_client.get(
        "/api/audit",
        params={"entity_type": "time_entries", "entity_id": str(entry.id)},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] >= 1

    by_field = {item["field"]: item for item in body["items"]}
    assert "check_out_at" in by_field, "the changed check-out should be in the trail"
    change = by_field["check_out_at"]
    assert change["actor_name"] == admin.username
    assert change["reason"] == "clock drift corrected"
    assert change["changed_at"] is not None
    assert change["old_value"] is not None and change["new_value"] is not None
    assert change["entity_type"] == "time_entries"
    assert change["entity_id"] == str(entry.id)


def test_a_system_change_has_no_actor_name(audit_client, sign_in, session: Session):
    """A change the system made on its own account (no actor) is served with a null name, not dropped."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    _audit(session, entity_type="employees", entity_id=employee.id, field="status", actor_id=None)

    response = audit_client.get(
        "/api/audit",
        params={"entity_type": "employees", "entity_id": str(employee.id)},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["actor_name"] is None


def test_rows_come_back_newest_first(audit_client, sign_in, session: Session):
    """The history reads latest-first, so a reader sees the most recent change at the top."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    first = _audit(session, entity_type="employees", entity_id=employee.id, field="position")
    second = _audit(session, entity_type="employees", entity_id=employee.id, field="notes")

    response = audit_client.get(
        "/api/audit",
        params={"entity_type": "employees", "entity_id": str(employee.id)},
        headers=headers,
    )
    ids = [item["id"] for item in response.json()["items"]]
    assert ids[:2] == [str(second.id), str(first.id)]


# --------------------------------------------------------------------------- scope (13.6)


def test_a_site_manager_reads_audit_for_a_time_entry_at_their_site(
    audit_client, sign_in, session: Session
):
    """Requirement 13.6: a manager may read audit for a time entry recorded at a site they run."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)
    mine = _make_site(session, number="S-MINE")
    session.add(UserSite(user_id=manager.id, site_id=mine.id))
    session.commit()
    entry = _make_entry(session, employee=employee, site=mine)
    _audit(session, entity_type="time_entries", entity_id=entry.id, field="check_out_at")

    response = audit_client.get(
        "/api/audit",
        params={"entity_type": "time_entries", "entity_id": str(entry.id)},
        headers=manager_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


def test_a_site_manager_is_refused_audit_for_a_time_entry_at_another_site(
    audit_client, sign_in, session: Session
):
    """Requirement 13.6: a manager may not read audit for an entity outside their assigned sites."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)
    mine = _make_site(session, number="S-MINE")
    other = _make_site(session, number="S-OTHER")
    session.add(UserSite(user_id=manager.id, site_id=mine.id))
    session.commit()
    entry = _make_entry(session, employee=employee, site=other)
    _audit(session, entity_type="time_entries", entity_id=entry.id, field="check_out_at")

    response = audit_client.get(
        "/api/audit",
        params={"entity_type": "time_entries", "entity_id": str(entry.id)},
        headers=manager_headers,
    )
    assert response.status_code == 403
    assert _code(response) == "site_out_of_scope"


def test_a_site_manager_reads_audit_for_their_own_site(audit_client, sign_in, session: Session):
    """Requirement 13.6: a site's audit is scoped by the site's own id."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    mine = _make_site(session, number="S-MINE")
    session.add(UserSite(user_id=manager.id, site_id=mine.id))
    session.commit()
    _audit(session, entity_type="sites", entity_id=mine.id, field="status")

    response = audit_client.get(
        "/api/audit",
        params={"entity_type": "sites", "entity_id": str(mine.id)},
        headers=manager_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


def test_a_site_manager_is_refused_audit_for_another_managers_site(
    audit_client, sign_in, session: Session
):
    """Requirement 13.6: a manager cannot read another site's audit."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    mine = _make_site(session, number="S-MINE")
    other = _make_site(session, number="S-OTHER")
    session.add(UserSite(user_id=manager.id, site_id=mine.id))
    session.commit()
    _audit(session, entity_type="sites", entity_id=other.id, field="status")

    response = audit_client.get(
        "/api/audit",
        params={"entity_type": "sites", "entity_id": str(other.id)},
        headers=manager_headers,
    )
    assert response.status_code == 403
    assert _code(response) == "site_out_of_scope"


def test_a_site_manager_reads_audit_for_an_employee_assigned_to_their_site(
    audit_client, sign_in, session: Session
):
    """Requirement 13.6, 7.1: an employee's audit is scoped by the sites they are assigned to."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    mine = _make_site(session, number="S-MINE")
    employee = _make_employee(session)
    session.add(UserSite(user_id=manager.id, site_id=mine.id))
    session.add(EmployeeSite(employee_id=employee.id, site_id=mine.id, assigned_from=date(2025, 1, 1)))
    session.commit()
    _audit(session, entity_type="employees", entity_id=employee.id, field="position")

    response = audit_client.get(
        "/api/audit",
        params={"entity_type": "employees", "entity_id": str(employee.id)},
        headers=manager_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


def test_a_site_manager_is_refused_audit_for_an_unassigned_employee(
    audit_client, sign_in, session: Session
):
    """Requirement 13.6: an employee at no site of the manager's is outside their audit scope."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    mine = _make_site(session, number="S-MINE")
    employee = _make_employee(session)
    session.add(UserSite(user_id=manager.id, site_id=mine.id))
    session.commit()
    _audit(session, entity_type="employees", entity_id=employee.id, field="position")

    response = audit_client.get(
        "/api/audit",
        params={"entity_type": "employees", "entity_id": str(employee.id)},
        headers=manager_headers,
    )
    assert response.status_code == 403
    assert _code(response) == "site_out_of_scope"


def test_an_admin_reads_any_entitys_audit(audit_client, sign_in, session: Session):
    """Requirement 13.6: administrators read audit across every entity, no site scope."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    _audit(session, entity_type="employees", entity_id=employee.id, field="position")
    _audit(session, entity_type="sites", entity_id=site.id, field="status")

    for entity_type, entity_id in (("employees", employee.id), ("sites", site.id)):
        response = audit_client.get(
            "/api/audit",
            params={"entity_type": entity_type, "entity_id": str(entity_id)},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1


# --------------------------------------------------------------------------- role (13.6)


def test_accounting_may_not_read_the_audit_view(audit_client, sign_in, session: Session):
    """Requirement 13.6 names administrators and site managers only; accounting is refused."""
    headers, _ = sign_in(UserRole.ACCOUNTING)
    employee = _make_employee(session)
    _audit(session, entity_type="employees", entity_id=employee.id, field="position")

    response = audit_client.get(
        "/api/audit",
        params={"entity_type": "employees", "entity_id": str(employee.id)},
        headers=headers,
    )
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_an_employee_may_not_read_the_audit_view(audit_client, sign_in, session: Session):
    """Requirement 13.6: the employee role has no audit access."""
    employee = _make_employee(session)
    employee_headers, _ = sign_in(UserRole.EMPLOYEE, employee_id=employee.id)

    response = audit_client.get(
        "/api/audit",
        params={"entity_type": "employees", "entity_id": str(employee.id)},
        headers=employee_headers,
    )
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


# --------------------------------------------------------------------------- global feed (13.4, admin-only)


def test_admin_reads_the_global_feed_across_entity_types(audit_client, sign_in, session: Session):
    """Requirement 13.4: an administrator reads recent changes across every entity, newest first.

    The feed is not scoped to one entity, so a change to an employee, a change to a site and a change
    to a user all appear in the one page — including a `users` row, which has no site at all and is
    exactly the kind of row the admin-only decision exists for.
    """
    headers, admin = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    _audit(session, entity_type="employees", entity_id=employee.id, field="position")
    _audit(session, entity_type="sites", entity_id=site.id, field="status")
    _audit(session, entity_type="users", entity_id=admin.id, field="role")

    response = audit_client.get("/api/audit/all", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    types = {item["entity_type"] for item in body["items"]}
    assert {"employees", "sites", "users"} <= types
    # Newest first: the users row was written last, so it heads the page.
    assert body["items"][0]["entity_type"] == "users"


def test_a_site_manager_is_refused_the_global_feed(audit_client, sign_in, session: Session):
    """The global feed is administrator-only: a site manager, who reads per-entity audit on cards,
    gets a 403 here because a polymorphic cross-entity feed cannot be scoped to their sites."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    mine = _make_site(session, number="S-MINE")
    session.add(UserSite(user_id=manager.id, site_id=mine.id))
    session.commit()
    _audit(session, entity_type="sites", entity_id=mine.id, field="status")

    response = audit_client.get("/api/audit/all", headers=manager_headers)
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_accounting_and_employee_are_refused_the_global_feed(audit_client, sign_in, session: Session):
    """The global feed admits only the administrator; accounting and the employee role are refused."""
    accounting_headers, _ = sign_in(UserRole.ACCOUNTING)
    assert audit_client.get("/api/audit/all", headers=accounting_headers).status_code == 403

    employee = _make_employee(session)
    employee_headers, _ = sign_in(UserRole.EMPLOYEE, employee_id=employee.id)
    assert audit_client.get("/api/audit/all", headers=employee_headers).status_code == 403


def test_the_entity_type_filter_narrows_the_global_feed(audit_client, sign_in, session: Session):
    """Requirement 13.4: the `entity_type` filter returns only rows of that type."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    _audit(session, entity_type="employees", entity_id=employee.id, field="position")
    _audit(session, entity_type="sites", entity_id=site.id, field="status")

    response = audit_client.get("/api/audit/all", params={"entity_type": "sites"}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert {item["entity_type"] for item in body["items"]} == {"sites"}


def test_the_date_range_narrows_the_global_feed_inclusively(audit_client, sign_in, session: Session):
    """Requirement 13.4: the `changed_at` range is inclusive at both ends, in UTC.

    Three rows are stamped on three different days; a range naming the middle day at both ends returns
    exactly that day's row, proving both bounds are inclusive and neither neighbour leaks in.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)

    def _row_on(day: date) -> ChangeLog:
        row = ChangeLog(
            entity_type="employees",
            entity_id=employee.id,
            field="position",
            old_value="old",
            new_value="new",
            changed_at=datetime(day.year, day.month, day.day, 9, tzinfo=UTC),
        )
        session.add(row)
        session.commit()
        return row

    _row_on(date(2025, 8, 29))
    middle = _row_on(date(2025, 8, 30))
    _row_on(date(2025, 8, 31))

    response = audit_client.get(
        "/api/audit/all",
        params={"date_from": "2025-08-30", "date_to": "2025-08-30"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(middle.id)


def test_no_endpoint_mutates_the_global_feed(audit_client):
    """Requirement 13.3: the global feed is read-only — only GET is mounted on `/api/audit/all`."""
    routes = [
        route for route in audit_client.app.routes if getattr(route, "path", "") == "/api/audit/all"
    ]
    assert routes, "the global audit route should be mounted"
    methods: set[str] = set()
    for route in routes:
        methods |= set(route.methods or set())
    assert methods <= {"GET", "HEAD", "OPTIONS"}
    assert "GET" in methods
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert verb not in methods
