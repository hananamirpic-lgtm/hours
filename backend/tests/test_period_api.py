"""Approval workflow and period locking over HTTP (Requirement 15).

The service rules are pinned in `test_period_service.py`; here the subject is the wiring, the guards
and the wire contract — the claims that need a request to prove:

* bulk status change moves a set forward, is scoped to the caller's sites, and refuses an illegal
  transition (Requirement 15.2, 15.3);
* only an administrator may reverse a status, and only with a reason (Requirement 15.2, 15.6);
* locking a month with unapproved entries warns and lists them (Requirement 15.7), and locking a
  clean month freezes the Approved entries (Requirement 15.4);
* a locked month blocks create, edit and delete for a manager, and an administrator override with a
  reason is permitted and recorded (Requirement 15.5, 15.6);
* lock, unlock and the period list are administrator-only.

The sign-in helper mirrors `test_manual_entry_api.py`: an admin enrols 2FA, others do not.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.change_log import ChangeLog
from app.models.client import Client
from app.models.employee import Employee
from app.models.site import Site
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.models.user import UserRole
from app.models.user_site import UserSite
from auth_support import DEFAULT_PASSWORD


@pytest.fixture
def periods_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(periods_client, make_user):
    def _sign_in(role: UserRole, **user_overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **user_overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **user_overrides)
        payload["username"] = user.username

        response = periods_client.post("/api/auth/login", json=payload)
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


def _make_entry(
    session: Session,
    *,
    employee: Employee,
    site: Site,
    status: TimeEntryStatus = TimeEntryStatus.DRAFT,
    day: int = 15,
    hour: int = 8,
) -> TimeEntry:
    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=date(2025, 8, day),
        check_in_at=datetime(2025, 8, day, hour, tzinfo=UTC),
        check_out_at=datetime(2025, 8, day, hour + 4, tzinfo=UTC),
        total_minutes=240,
        source=TimeEntrySource.QR_SCAN,
        is_manual=False,
        status=status,
        flags=[],
    )
    session.add(entry)
    session.commit()
    return entry


# ===================================================================== bulk status change


def test_a_manager_advances_their_sites_entries(periods_client, sign_in, session: Session):
    """Requirement 15.3: a site manager moves their sites' entries Draft → Review."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)
    site = _make_site(session)
    session.add(UserSite(user_id=manager.id, site_id=site.id))
    session.commit()
    entry = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.DRAFT)

    response = periods_client.post(
        "/api/time-entries/bulk-status",
        json={"entry_ids": [str(entry.id)], "target_status": "review"},
        headers=manager_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated_count"] == 1
    assert body["updated_ids"] == [str(entry.id)]


def test_a_manager_cannot_move_entries_outside_their_sites(periods_client, sign_in, session: Session):
    """Requirement 2.3: an entry at an unscoped site is not in the set the update touches."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)
    mine = _make_site(session, number="S-MINE")
    other = _make_site(session, number="S-OTHER")
    session.add(UserSite(user_id=manager.id, site_id=mine.id))
    session.commit()
    entry = _make_entry(session, employee=employee, site=other, status=TimeEntryStatus.DRAFT)

    response = periods_client.post(
        "/api/time-entries/bulk-status",
        json={"entry_ids": [str(entry.id)], "target_status": "review"},
        headers=manager_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated_count"] == 0
    # The entry is untouched.
    session.refresh(entry)
    assert entry.status is TimeEntryStatus.DRAFT


def test_an_illegal_transition_is_a_409(periods_client, sign_in, session: Session):
    """Requirement 15.2: Draft straight to Approved skips a rung and is refused."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    entry = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.DRAFT)

    response = periods_client.post(
        "/api/time-entries/bulk-status",
        json={"entry_ids": [str(entry.id)], "target_status": "approved"},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    error = response.json()["detail"]["error"]
    assert error["code"] == "invalid_transition"
    assert error["params"]["from_status"] == "draft"
    assert error["params"]["to_status"] == "approved"


def test_a_manager_may_not_reverse_a_status(periods_client, sign_in, session: Session):
    """Requirement 15.2: only an administrator may step a status back."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)
    site = _make_site(session)
    session.add(UserSite(user_id=manager.id, site_id=site.id))
    session.commit()
    entry = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.APPROVED)

    response = periods_client.post(
        "/api/time-entries/bulk-status",
        json={"entry_ids": [str(entry.id)], "target_status": "review", "reason": "undo"},
        headers=manager_headers,
    )
    assert response.status_code == 409
    assert _code(response) == "reversal_requires_admin"


def test_an_admin_reversal_needs_a_reason(periods_client, sign_in, session: Session):
    """Requirement 15.6: an administrator's reversal without a reason is a 400."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    entry = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.APPROVED)

    response = periods_client.post(
        "/api/time-entries/bulk-status",
        json={"entry_ids": [str(entry.id)], "target_status": "review"},
        headers=headers,
    )
    assert response.status_code == 400
    assert _code(response) == "reversal_requires_reason"


def test_an_admin_reverses_with_a_reason(periods_client, sign_in, session: Session):
    """Requirement 15.2, 15.6: an administrator steps a status back with a reason."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    entry = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.APPROVED)

    response = periods_client.post(
        "/api/time-entries/bulk-status",
        json={"entry_ids": [str(entry.id)], "target_status": "review", "reason": "approved in error"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated_count"] == 1
    session.refresh(entry)
    assert entry.status is TimeEntryStatus.REVIEW


# ===================================================================== lock warning and freeze


def test_locking_warns_on_unapproved_entries(periods_client, sign_in, session: Session):
    """Requirement 15.7: a month with unapproved entries warns and lists them, locking nothing."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.APPROVED, hour=6)
    draft = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.DRAFT, hour=12)

    response = periods_client.post("/api/periods/2025/8/lock", json={}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["locked"] is False
    assert [e["id"] for e in body["unapproved"]] == [str(draft.id)]


def test_locking_a_clean_month_freezes_approved_entries(periods_client, sign_in, session: Session):
    """Requirement 15.4: locking sets Approved entries to Locked."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    entry = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.APPROVED)

    response = periods_client.post("/api/periods/2025/8/lock", json={}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["locked"] is True
    assert body["locked_count"] == 1
    session.refresh(entry)
    assert entry.status is TimeEntryStatus.LOCKED


def test_forcing_a_lock_past_the_warning(periods_client, sign_in, session: Session):
    """Requirement 15.7: forcing locks Approved and leaves unapproved entries in place."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    approved = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.APPROVED, hour=6)
    draft = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.DRAFT, hour=12)

    response = periods_client.post("/api/periods/2025/8/lock", json={"force": True}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["locked"] is True
    session.refresh(approved)
    session.refresh(draft)
    assert approved.status is TimeEntryStatus.LOCKED
    assert draft.status is TimeEntryStatus.DRAFT


def test_lock_is_admin_only(periods_client, sign_in, session: Session):
    """Requirement 15.4: a site manager cannot lock a month."""
    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)
    response = periods_client.post("/api/periods/2025/8/lock", json={}, headers=manager_headers)
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


# ===================================================================== lock blocks writes


def _create_body(employee: Employee, site: Site, *, reason="scan failed", override=None):
    body = {
        "employee_id": str(employee.id),
        "site_id": str(site.id),
        "check_in_at": datetime(2025, 8, 15, 14, tzinfo=UTC).isoformat(),
        "check_out_at": datetime(2025, 8, 15, 16, tzinfo=UTC).isoformat(),
        "reason": reason,
    }
    if override is not None:
        body["override"] = override
    return body


def _lock_august(periods_client, headers) -> None:
    response = periods_client.post("/api/periods/2025/8/lock", json={"force": True}, headers=headers)
    assert response.status_code == 200, response.text


def test_a_locked_month_blocks_create_for_a_manager(periods_client, sign_in, session: Session):
    """Requirement 15.5: with the month locked, a manager's manual create is refused."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)
    site = _make_site(session)
    session.add(UserSite(user_id=manager.id, site_id=site.id))
    session.commit()
    _lock_august(periods_client, admin_headers)

    response = periods_client.post(
        "/api/time-entries", json=_create_body(employee, site), headers=manager_headers
    )
    assert response.status_code == 409
    assert _code(response) == "period_locked"


def test_a_locked_month_blocks_edit_and_delete_for_a_manager(periods_client, sign_in, session: Session):
    """Requirement 15.5: with the month locked, a manager's edit and delete are refused."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)
    site = _make_site(session)
    session.add(UserSite(user_id=manager.id, site_id=site.id))
    session.commit()
    entry = _make_entry(session, employee=employee, site=site, status=TimeEntryStatus.APPROVED)
    _lock_august(periods_client, admin_headers)

    edited = periods_client.patch(
        f"/api/time-entries/{entry.id}",
        json={"check_out_at": datetime(2025, 8, 15, 13, tzinfo=UTC).isoformat(), "reason": "fix"},
        headers=manager_headers,
    )
    assert edited.status_code == 409
    assert _code(edited) == "period_locked"

    deleted = periods_client.request(
        "DELETE", f"/api/time-entries/{entry.id}", json={"reason": "x"}, headers=manager_headers
    )
    assert deleted.status_code == 409
    assert _code(deleted) == "period_locked"


# ===================================================================== admin override


def test_a_manager_may_not_override_a_lock(periods_client, sign_in, session: Session):
    """Requirement 15.5: only an administrator may override a lock; a manager asking is refused."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    employee = _make_employee(session)
    site = _make_site(session)
    session.add(UserSite(user_id=manager.id, site_id=site.id))
    session.commit()
    _lock_august(periods_client, admin_headers)

    response = periods_client.post(
        "/api/time-entries",
        json=_create_body(employee, site, override=True),
        headers=manager_headers,
    )
    assert response.status_code == 403
    assert _code(response) == "override_requires_admin"


def test_an_admin_overrides_a_lock_with_a_reason_and_it_is_audited(
    periods_client, sign_in, session: Session
):
    """Requirement 15.5, 15.6: an admin writes into a locked month with a reason, recorded in audit."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    site = _make_site(session)
    _lock_august(periods_client, admin_headers)

    response = periods_client.post(
        "/api/time-entries",
        json=_create_body(employee, site, reason="late correction approved by ops", override=True),
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text

    overrides = session.scalars(
        select(ChangeLog).where(
            ChangeLog.entity_type == "period_locks", ChangeLog.field == "override"
        )
    ).all()
    assert len(overrides) == 1
    assert overrides[0].reason == "late correction approved by ops"
    assert "manual_create" in overrides[0].new_value


# ===================================================================== unlock and list


def test_unlock_reopens_the_month(periods_client, sign_in, session: Session):
    """Requirement 15.6: an admin unlocks a locked month with a reason."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    _lock_august(periods_client, admin_headers)

    response = periods_client.post(
        "/api/periods/2025/8/unlock", json={"reason": "correction window"}, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_locked"] is False
    assert body["unlock_reason"] == "correction window"


def test_unlock_needs_a_reason(periods_client, sign_in, session: Session):
    """Requirement 15.6: an unlock with no reason is a validation error."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    _lock_august(periods_client, admin_headers)

    response = periods_client.post(
        "/api/periods/2025/8/unlock", json={"reason": "  "}, headers=admin_headers
    )
    assert response.status_code == 422


def test_the_period_list_shows_touched_months(periods_client, sign_in, session: Session):
    """Requirement 15.4: the period list reports the months the workflow has touched."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    _lock_august(periods_client, admin_headers)

    response = periods_client.get("/api/periods", headers=admin_headers)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert any(item["year"] == 2025 and item["month"] == 8 and item["is_locked"] for item in items)
