"""Manual time entry and correction over HTTP (Requirement 12).

The read endpoint is covered in `test_time_entry_api.py`; here the subject is the write side Task 21
adds — `POST`, `PATCH` and `DELETE /api/time-entries`. The claims worth an HTTP test are the ones
about who may write and what the wire refuses: that an employee is forbidden from all three
operations (Requirement 12.6); that a site manager may only touch entries at their assigned sites
(Requirement 2.3); that a missing or blank reason is rejected (Requirement 12.3); that an overlap
comes back a 409 naming the conflict (Requirement 11.7, 12.5); and that a soft delete returns the
retained, marked-deleted entry (Requirement 12.7). The service rules are pinned in
`test_manual_entry_service.py`; here the subject is the wiring, the guard and the scope.

The sign-in helper mirrors `test_time_entry_api.py`: an admin enrols 2FA, a manager and an employee
do not.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.employee import Employee
from app.models.site import Site
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.models.user import UserRole
from app.models.user_site import UserSite
from auth_support import DEFAULT_PASSWORD


@pytest.fixture
def entries_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(entries_client, make_user):
    def _sign_in(role: UserRole, **user_overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **user_overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **user_overrides)
        payload["username"] = user.username

        response = entries_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        header = {"Authorization": f"Bearer {response.json()['access_token']}"}
        return header, user

    return _sign_in


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


_PASSPORT = iter(f"P{n:07d}" for n in range(1, 100000))


def _make_employee(session: Session) -> Employee:
    passport = next(_PASSPORT)
    employee = Employee(
        full_name="Worker",
        full_name_en="Worker",
        passport_number=passport,
        passport_number_hash=passport,
        phone="+972500000000",
        country="Israel",
        emergency_contact_name="Contact",
        emergency_contact_phone="+972500000001",
        start_date=date(2025, 1, 1),
    )
    session.add(employee)
    session.commit()
    return employee


def _make_site(session: Session, *, number: str = "S-1") -> Site:
    client = Client(name="Acme")
    session.add(client)
    session.flush()
    site = Site(
        name=f"Site {number}",
        site_number=number,
        client_id=client.id,
        qr_token=f"placeholder-{uuid.uuid4().hex}",
        qr_token_version=1,
    )
    session.add(site)
    session.commit()
    return site


def _iso(hour: int, minute: int = 0, day: int = 30) -> str:
    return datetime(2025, 8, day, hour, minute, tzinfo=UTC).isoformat()


def _create_body(employee: Employee, site: Site, *, check_in: str, check_out: str, reason="scan failed"):
    return {
        "employee_id": str(employee.id),
        "site_id": str(site.id),
        "check_in_at": check_in,
        "check_out_at": check_out,
        "reason": reason,
    }


def _make_entry(session: Session, *, employee: Employee, site: Site) -> TimeEntry:
    entry = TimeEntry(
        employee_id=employee.id, site_id=site.id, work_date=date(2025, 8, 30),
        check_in_at=datetime(2025, 8, 30, 8, tzinfo=UTC),
        check_out_at=datetime(2025, 8, 30, 12, tzinfo=UTC),
        total_minutes=240, source=TimeEntrySource.QR_SCAN, is_manual=False,
        status=TimeEntryStatus.DRAFT, flags=[],
    )
    session.add(entry)
    session.commit()
    return entry


# --------------------------------------------------------------------------- role: employee forbidden


def test_an_employee_may_not_create_edit_or_delete(entries_client, sign_in, session: Session):
    """Requirement 12.6: the employee role is forbidden from all three write operations."""
    employee = _make_employee(session)
    site = _make_site(session)
    headers, _ = sign_in(UserRole.EMPLOYEE, employee_id=employee.id)
    entry = _make_entry(session, employee=employee, site=site)

    created = entries_client.post(
        "/api/time-entries",
        json=_create_body(employee, site, check_in=_iso(14), check_out=_iso(16)),
        headers=headers,
    )
    assert created.status_code == 403
    assert _code(created) == "insufficient_role"

    edited = entries_client.patch(
        f"/api/time-entries/{entry.id}",
        json={"check_out_at": _iso(13), "reason": "x"},
        headers=headers,
    )
    assert edited.status_code == 403

    deleted = entries_client.request(
        "DELETE", f"/api/time-entries/{entry.id}", json={"reason": "x"}, headers=headers
    )
    assert deleted.status_code == 403


# --------------------------------------------------------------------------- reason mandatory


def test_a_missing_reason_is_rejected(entries_client, sign_in, session: Session):
    """Requirement 12.3: a create with no reason is a validation error."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)

    body = _create_body(employee, site, check_in=_iso(8), check_out=_iso(12))
    del body["reason"]
    response = entries_client.post("/api/time-entries", json=body, headers=headers)
    assert response.status_code == 422


def test_a_blank_reason_is_rejected(entries_client, sign_in, session: Session):
    """Requirement 12.3: a whitespace-only reason satisfies 'present' but is not a reason."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)

    response = entries_client.post(
        "/api/time-entries",
        json=_create_body(employee, site, check_in=_iso(8), check_out=_iso(12), reason="   "),
        headers=headers,
    )
    assert response.status_code == 422


def test_a_delete_without_a_reason_is_rejected(entries_client, sign_in, session: Session):
    """Requirement 12.3, 12.7: a soft delete requires a reason."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    entry = _make_entry(session, employee=employee, site=site)

    response = entries_client.request(
        "DELETE", f"/api/time-entries/{entry.id}", json={"reason": ""}, headers=headers
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- happy paths


def test_admin_creates_a_manual_entry(entries_client, sign_in, session: Session):
    """Requirement 12.1, 12.4: an admin creates an entry, returned marked manual with its reason."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)

    response = entries_client.post(
        "/api/time-entries",
        json=_create_body(employee, site, check_in=_iso(8), check_out=_iso(12)),
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["is_manual"] is True
    assert body["source"] == "manual"
    assert body["manual_reason"] == "scan failed"
    assert body["total_minutes"] == 240


def test_admin_corrects_an_entry(entries_client, sign_in, session: Session):
    """Requirement 12.2: an admin edits a check-out time and the total is recomputed."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    entry = _make_entry(session, employee=employee, site=site)

    response = entries_client.patch(
        f"/api/time-entries/{entry.id}",
        json={"check_out_at": _iso(13), "reason": "worked later"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_manual"] is True
    assert body["total_minutes"] == 300


def test_admin_soft_deletes_an_entry(entries_client, sign_in, session: Session):
    """Requirement 12.7: a delete returns the retained entry marked deleted."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    entry = _make_entry(session, employee=employee, site=site)

    response = entries_client.request(
        "DELETE", f"/api/time-entries/{entry.id}",
        json={"reason": "duplicate"}, headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["deleted_at"] is not None

    # The row is retained: it is gone from the live hours view but still in the table.
    listed = entries_client.get("/api/time-entries", headers=headers)
    assert listed.json()["total"] == 0
    assert session.get(TimeEntry, entry.id) is not None


# --------------------------------------------------------------------------- overlap


def test_an_overlap_is_a_409_naming_the_conflict(entries_client, sign_in, session: Session):
    """Requirement 11.7, 12.5: an overlapping manual entry is a 409 that names the conflict."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    existing = _make_entry(session, employee=employee, site=site)  # 08:00–12:00

    response = entries_client.post(
        "/api/time-entries",
        json=_create_body(employee, site, check_in=_iso(11), check_out=_iso(15)),
        headers=headers,
    )
    assert response.status_code == 409, response.text
    error = response.json()["detail"]["error"]
    assert error["code"] == "overlap_rejected"
    assert error["params"]["time_entry_id"] == str(existing.id)


# --------------------------------------------------------------------------- scope


def test_a_site_manager_may_only_write_at_their_sites(entries_client, sign_in, session: Session):
    """Requirement 2.3: a manager creating at a site outside their scope is refused."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)
    mine = _make_site(session, number="S-MINE")
    other = _make_site(session, number="S-OTHER")
    session.add(UserSite(user_id=manager.id, site_id=mine.id))
    session.commit()

    at_mine = entries_client.post(
        "/api/time-entries",
        json=_create_body(employee, mine, check_in=_iso(8), check_out=_iso(12)),
        headers=manager_headers,
    )
    assert at_mine.status_code == 201, at_mine.text

    at_other = entries_client.post(
        "/api/time-entries",
        json=_create_body(employee, other, check_in=_iso(8), check_out=_iso(12)),
        headers=manager_headers,
    )
    assert at_other.status_code == 403
    assert _code(at_other) == "site_out_of_scope"


def test_a_manager_may_not_correct_an_entry_outside_their_sites(
    entries_client, sign_in, session: Session
):
    """Requirement 2.3: correcting an entry at an unscoped site is refused."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)
    mine = _make_site(session, number="S-MINE")
    other = _make_site(session, number="S-OTHER")
    session.add(UserSite(user_id=manager.id, site_id=mine.id))
    session.commit()
    entry = _make_entry(session, employee=employee, site=other)

    response = entries_client.patch(
        f"/api/time-entries/{entry.id}",
        json={"check_out_at": _iso(13), "reason": "x"},
        headers=manager_headers,
    )
    assert response.status_code == 403
    assert _code(response) == "site_out_of_scope"
