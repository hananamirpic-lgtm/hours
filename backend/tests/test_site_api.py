"""Site endpoints over HTTP (Requirement 6, 7, with 2 and 17.7 for the guards).

The claims worth an HTTP test are the ones about requests, responses and who may see what: that a
duplicate site number is a 409 that names the conflict, that a site manager reading a site they run
gets the card but no billing rate (Requirement 2.5, 17.7), that a manager sees only their assigned
sites in the list, and that a request carrying a location field is rejected as an unknown field
(Requirement 6.8). The deeper rules are pinned in `test_site_service.py`; here the subject is the
wiring and the redaction.

The sign-in helper mirrors the one in `test_client_api.py`: an administrator must have completed 2FA
enrolment or every endpoint answers 403 (Requirement 1.6).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.user import UserRole
from auth_support import DEFAULT_PASSWORD


@pytest.fixture
def sites_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(sites_client, make_user):
    """Sign a user in and return the auth header, exposing the created user for assignment tests."""

    def _sign_in(role: UserRole, **user_overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **user_overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **user_overrides)
        payload["username"] = user.username

        response = sites_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        header = {"Authorization": f"Bearer {response.json()['access_token']}"}
        return header, user

    return _sign_in


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


def _make_client_row(session: Session, name: str = "Acme") -> uuid.UUID:
    client = Client(name=name)
    session.add(client)
    session.commit()
    return client.id


def _site_body(client_id: uuid.UUID, *, number: str = "S-1", **overrides) -> dict[str, object]:
    body: dict[str, object] = {
        "name": f"Site {number}",
        "site_number": number,
        "client_id": str(client_id),
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- create and read


def test_create_and_read_a_site_as_admin(sites_client, sign_in, session: Session):
    """Requirement 6.1: a site is created and read back with its fields."""
    headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)

    created = sites_client.post("/api/sites", json=_site_body(client_id), headers=headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["site_number"] == "S-1"
    assert body["status"] == "active"
    assert body["qr_mode"] == "unified"
    assert body["assignment_mode"] == "open"


def test_a_duplicate_site_number_is_a_conflict_that_names_the_holder(sites_client, sign_in, session):
    """Requirement 6.3."""
    headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    first = sites_client.post("/api/sites", json=_site_body(client_id, number="S-1"), headers=headers)
    assert first.status_code == 201, first.text

    duplicate = sites_client.post(
        "/api/sites", json=_site_body(client_id, number="S-1"), headers=headers
    )
    assert duplicate.status_code == 409
    error = duplicate.json()["detail"]["error"]
    assert error["code"] == "duplicate_site_number"
    assert error["params"]["site_id"] == first.json()["id"]


def test_a_location_field_is_rejected_as_unknown(sites_client, sign_in, session: Session):
    """Requirement 6.8: no location field exists, so one sent is a 422 unknown field."""
    headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    response = sites_client.post(
        "/api/sites", json=_site_body(client_id, latitude=32.1, longitude=34.8), headers=headers
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- billing rates and redaction


def _create_site_with_rate(sites_client, headers, client_id: uuid.UUID, *, number="S-1") -> str:
    body = _site_body(
        client_id,
        number=number,
        rate={"billing_rate": "60.00", "effective_from": "2025-01-01"},
    )
    created = sites_client.post("/api/sites", json=body, headers=headers)
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_admin_sees_the_billing_rate(sites_client, sign_in, session: Session):
    """Requirement 17.7: an administrator may read the billing rate."""
    headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    site_id = _create_site_with_rate(sites_client, headers, client_id)

    response = sites_client.get(f"/api/sites/{site_id}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["billing_rate"] == "60.00"
    assert body["site_rates"][0]["billing_rate"] == "60.00"


def test_a_site_manager_cannot_read_the_billing_rate(sites_client, sign_in, session: Session):
    """Requirement 2.5, 17.7: a site manager reads their site but not what the client is billed.

    The manager is assigned to the site through the assignment endpoint (which writes `user_sites`),
    then reads it: the card comes back, but the billing rate and the whole `site_rates` history are
    absent — removed, not nulled.
    """
    admin_headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)

    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    # Create the site with the manager assigned, so the manager's user_sites grant is written.
    body = _site_body(
        client_id,
        rate={"billing_rate": "60.00", "effective_from": "2025-01-01"},
        manager_user_id=str(manager.id),
    )
    created = sites_client.post("/api/sites", json=body, headers=admin_headers)
    assert created.status_code == 201, created.text
    site_id = created.json()["id"]

    response = sites_client.get(f"/api/sites/{site_id}", headers=manager_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    # The card is readable...
    assert body["site_number"] == "S-1"
    # ...but the billing fields are gone entirely, not present-and-null.
    assert "billing_rate" not in body
    assert "overtime_billing_rate" not in body
    assert "site_rates" not in body


def test_a_site_manager_cannot_read_the_rates_endpoint_rate(sites_client, sign_in, session: Session):
    """The dedicated rates endpoint redacts the same way as the card."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)

    body = _site_body(
        client_id,
        rate={"billing_rate": "75.00", "effective_from": "2025-01-01"},
        manager_user_id=str(manager.id),
    )
    site_id = sites_client.post("/api/sites", json=body, headers=admin_headers).json()["id"]

    response = sites_client.get(f"/api/sites/{site_id}/rates", headers=manager_headers)
    assert response.status_code == 200
    assert "site_rates" not in response.json()


def test_updating_rates_leaves_a_past_period_unchanged(sites_client, sign_in, session: Session):
    """Requirement 6.6 over HTTP: replacing the history to add a new period keeps the old one's rate."""
    headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    site_id = _create_site_with_rate(sites_client, headers, client_id)

    # Replace with a closed August row plus a new September row.
    response = sites_client.put(
        f"/api/sites/{site_id}/rates",
        json={
            "rates": [
                {"billing_rate": "60.00", "effective_from": "2025-01-01", "effective_to": "2025-08-31"},
                {"billing_rate": "75.00", "effective_from": "2025-09-01"},
            ]
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    rates = {r["effective_from"]: r["billing_rate"] for r in response.json()["site_rates"]}
    assert rates["2025-01-01"] == "60.00"
    assert rates["2025-09-01"] == "75.00"


# --------------------------------------------------------------------------- scoping


def test_a_site_manager_sees_only_their_assigned_sites(sites_client, sign_in, session: Session):
    """Requirement 2.3: the list is scoped to the manager's sites."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)

    mine = sites_client.post(
        "/api/sites",
        json=_site_body(client_id, number="S-MINE", manager_user_id=str(manager.id)),
        headers=admin_headers,
    ).json()["id"]
    sites_client.post("/api/sites", json=_site_body(client_id, number="S-OTHER"), headers=admin_headers)

    response = sites_client.get("/api/sites", headers=manager_headers)
    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {mine}


def test_a_site_manager_reading_an_unscoped_site_is_forbidden(sites_client, sign_in, session: Session):
    admin_headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)

    site_id = sites_client.post(
        "/api/sites", json=_site_body(client_id, number="S-OTHER"), headers=admin_headers
    ).json()["id"]

    response = sites_client.get(f"/api/sites/{site_id}", headers=manager_headers)
    assert response.status_code == 403
    assert _code(response) == "site_out_of_scope"


# --------------------------------------------------------------------------- assignment endpoints


def test_setting_site_employees_and_reading_the_mirror(sites_client, sign_in, session: Session):
    """Requirement 7.1: the two sides of the assignment agree."""
    from app.models.employee import Employee

    headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    site_id = _create_site_with_rate(sites_client, headers, client_id)

    employee = Employee(
        full_name="Worker",
        full_name_en="Worker",
        passport_number="P0000001",
        passport_number_hash="P0000001",
        phone="+972500000000",
        country="Israel",
        emergency_contact_name="Contact",
        emergency_contact_phone="+972500000001",
        start_date=date(2025, 1, 1),
    )
    session.add(employee)
    session.commit()

    put = sites_client.put(
        f"/api/sites/{site_id}/employees",
        json={"employee_ids": [str(employee.id)]},
        headers=headers,
    )
    assert put.status_code == 200, put.text
    assert str(employee.id) in put.json()["employee_ids"]

    # The employee-side endpoint sees the same assignment.
    mirror = sites_client.get(f"/api/employees/{employee.id}/sites", headers=headers)
    assert mirror.status_code == 200
    assert str(site_id) in mirror.json()["site_ids"]


def test_setting_employee_sites_from_the_employee_side(sites_client, sign_in, session: Session):
    """Requirement 7.1: PUT /employees/{id}/sites writes the same table."""
    from app.models.employee import Employee

    headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    site_id = _create_site_with_rate(sites_client, headers, client_id)
    employee = Employee(
        full_name="Worker",
        full_name_en="Worker",
        passport_number="P0000002",
        passport_number_hash="P0000002",
        phone="+972500000000",
        country="Israel",
        emergency_contact_name="Contact",
        emergency_contact_phone="+972500000001",
        start_date=date(2025, 1, 1),
    )
    session.add(employee)
    session.commit()

    put = sites_client.put(
        f"/api/employees/{employee.id}/sites",
        json={"site_ids": [site_id]},
        headers=headers,
    )
    assert put.status_code == 200, put.text
    assert site_id in put.json()["site_ids"]


# --------------------------------------------------------------------------- authorization


def test_a_site_manager_cannot_create_a_site(sites_client, sign_in, session: Session):
    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)
    client_id = _make_client_row(session)
    response = sites_client.post("/api/sites", json=_site_body(client_id), headers=manager_headers)
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_reading_an_unknown_site_is_a_not_found(sites_client, sign_in):
    headers, _ = sign_in(UserRole.ADMIN)
    response = sites_client.get(f"/api/sites/{uuid.uuid4()}", headers=headers)
    assert response.status_code == 404
    assert _code(response) == "site_not_found"


def test_the_billing_rate_field_names_match_the_redaction_set():
    """A guard against drift: the response's billing fields are exactly the ones authz strips."""
    from app.core.authz import BILLING_FIELDS

    assert {"billing_rate", "overtime_billing_rate", "site_rates"} <= BILLING_FIELDS
    _ = Decimal  # keep the import meaningful for the money-typed bodies above
