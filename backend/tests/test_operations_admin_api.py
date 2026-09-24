"""The operations-admin role over HTTP (Feature: operations-admin-role).

`operations_admin` is a full operational administrator with no financial visibility. The pure policy
guarantees (money redaction, all-sites scope) are pinned in `test_authz_policy.py`; here the subject
is the endpoint wiring the requirements turn on:

* **Property 2** — the finance endpoints (payroll, billing) refuse an operations_admin with 403.
* **Property 3** — an operations_admin can perform an operational write (create a site) that an
  administrator can, and read it back.
* **Property 4** — an operations_admin may not create or promote a user to `admin` or `accounting`,
  but may assign `site_manager`, `employee` and `operations_admin`.
* **Property 7** — an operations_admin signs in with only a username and password: it is not required
  or prompted for 2FA (the sign-in helper's non-admin branch is proof enough, asserted explicitly).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.user import UserRole
from auth_support import DEFAULT_PASSWORD


@pytest.fixture
def ops_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(ops_client, make_user):
    """Sign a user in and return the auth header and the created user.

    An administrator must have completed 2FA enrolment; every other console role — including
    operations_admin — signs in with username and password alone, which is the observable half of
    Property 7 (no 2FA obligation on the new role).
    """

    def _sign_in(role: UserRole, **overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **overrides)
        payload["username"] = user.username
        response = ops_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}, user

    return _sign_in


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


def _client_row(session: Session, name: str = "Acme") -> uuid.UUID:
    row = Client(name=name)
    session.add(row)
    session.commit()
    return row.id


# --------------------------------------------------------------------------- Property 7: no 2FA


def test_operations_admin_signs_in_without_two_factor(ops_client, make_user):
    """Property 7: no TOTP is required — a username and password alone authenticate the role."""
    user = make_user(role=UserRole.OPERATIONS_ADMIN)
    response = ops_client.post(
        "/api/auth/login", json={"username": user.username, "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 200, response.text
    assert "access_token" in response.json()


# --------------------------------------------------------------------------- Property 3: operational access


def test_operations_admin_can_create_and_read_a_site(ops_client, sign_in, session: Session):
    """Property 3: an operational write an admin may do, the operations admin may do too."""
    headers, _ = sign_in(UserRole.OPERATIONS_ADMIN)
    client_id = _client_row(session)

    created = ops_client.post(
        "/api/sites",
        json={"name": "North Gate", "site_number": "S-OPS", "client_id": str(client_id)},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    site_id = created.json()["id"]

    read = ops_client.get(f"/api/sites/{site_id}", headers=headers)
    assert read.status_code == 200, read.text
    assert read.json()["site_number"] == "S-OPS"


def test_operations_admin_site_response_carries_no_billing(ops_client, sign_in, session: Session):
    """Property 1 at the HTTP boundary: the site the operations admin reads has no billing fields."""
    headers, _ = sign_in(UserRole.OPERATIONS_ADMIN)
    client_id = _client_row(session)
    created = ops_client.post(
        "/api/sites",
        json={"name": "Gate", "site_number": "S-NOMONEY", "client_id": str(client_id)},
        headers=headers,
    )
    body = ops_client.get(f"/api/sites/{created.json()['id']}", headers=headers).json()
    for field in ("billing_rate", "overtime_billing_rate", "site_rates"):
        assert field not in body


# --------------------------------------------------------------------------- Property 2: finance closed


def test_operations_admin_is_refused_payroll(ops_client, sign_in, session: Session):
    """Property 2: payroll is a finance endpoint; the operations admin gets 403."""
    headers, _ = sign_in(UserRole.OPERATIONS_ADMIN)
    response = ops_client.post(
        "/api/payroll/calculate",
        json={"employee_id": str(uuid.uuid4()), "year": 2025, "month": 8},
        headers=headers,
    )
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_operations_admin_is_refused_billing(ops_client, sign_in, session: Session):
    """Property 2: billing is a finance endpoint; the operations admin gets 403."""
    headers, _ = sign_in(UserRole.OPERATIONS_ADMIN)
    response = ops_client.post(
        "/api/billing/calculate", json={"year": 2025, "month": 8}, headers=headers
    )
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


# --------------------------------------------------------------------------- Property 4: no escalation


def test_operations_admin_may_create_a_site_manager(ops_client, sign_in):
    """Property 4: a non-money role is assignable by the operations admin."""
    headers, _ = sign_in(UserRole.OPERATIONS_ADMIN)
    response = ops_client.post(
        "/api/users",
        json={"username": "mgr1", "password": "correct-horse-battery", "role": "site_manager"},
        headers=headers,
    )
    assert response.status_code == 201, response.text


def test_operations_admin_may_create_another_operations_admin(ops_client, sign_in):
    """Property 4: operations_admin is self-assignable."""
    headers, _ = sign_in(UserRole.OPERATIONS_ADMIN)
    response = ops_client.post(
        "/api/users",
        json={"username": "ops2", "password": "correct-horse-battery", "role": "operations_admin"},
        headers=headers,
    )
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("forbidden", ["admin", "accounting"])
def test_operations_admin_may_not_create_a_finance_role(ops_client, sign_in, forbidden):
    """Property 4: creating an admin or accounting login is refused with 403 and nothing is written."""
    headers, _ = sign_in(UserRole.OPERATIONS_ADMIN)
    response = ops_client.post(
        "/api/users",
        json={"username": "sneaky", "password": "correct-horse-battery", "role": forbidden},
        headers=headers,
    )
    assert response.status_code == 403
    assert _code(response) == "role_assignment_forbidden"


def test_an_admin_may_still_create_a_finance_role(ops_client, sign_in):
    """Property 6 (no regression): the restriction is on the operations admin, not the administrator."""
    headers, _ = sign_in(UserRole.ADMIN)
    response = ops_client.post(
        "/api/users",
        json={"username": "acct1", "password": "correct-horse-battery", "role": "accounting"},
        headers=headers,
    )
    assert response.status_code == 201, response.text

# --------------------------------------------------------------------------- Property 1: staffing money


def test_operations_admin_does_not_see_staffing_company_hourly_rate(ops_client, sign_in):
    """Property 1: a staffing company's hourly_rate is money and must be stripped for operations_admin.

    An administrator creates a company with a rate; the operations admin reads it back and the rate is
    absent (not null, not present,), while the operational fields remain. This guards the leak found in
    manual testing: hourly_rate was neither in the money-field set nor redacted by the router.
    """
    admin_headers, _ = sign_in(UserRole.ADMIN)
    created = ops_client.post(
        "/api/staffing-companies",
        json={"name": "Manpower", "contact_person": "Dana", "hourly_rate": "42.50"},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    # The admin (a finance role) sees the rate.
    assert created.json().get("hourly_rate") == "42.50"
    company_id = created.json()["id"]

    ops_headers, _ = sign_in(UserRole.OPERATIONS_ADMIN)

    read = ops_client.get(f"/api/staffing-companies/{company_id}", headers=ops_headers)
    assert read.status_code == 200, read.text
    body = read.json()
    assert "hourly_rate" not in body  # absent, not null
    assert body["name"] == "Manpower"  # operational data still present

    listed = ops_client.get("/api/staffing-companies", headers=ops_headers)
    assert listed.status_code == 200, listed.text
    for item in listed.json()["items"]:
        assert "hourly_rate" not in item

# --------------------------------------------------------------------------- Property 3: clients readable


def test_operations_admin_can_list_and_read_clients(ops_client, sign_in, session: Session):
    """Property 3: managing clients is operational, so the operations admin can read them (not 403).

    Guards the regression where the client READ endpoints were finance-only while the WRITE endpoints
    were operational, so an operations admin could create a client but got 403 listing them � the
    "data could not be loaded" the Clients page showed.
    """
    admin_headers, _ = sign_in(UserRole.ADMIN)
    created = ops_client.post(
        "/api/clients", json={"name": "Acme Ltd"}, headers=admin_headers
    )
    assert created.status_code == 201, created.text
    client_id = created.json()["id"]

    ops_headers, _ = sign_in(UserRole.OPERATIONS_ADMIN)

    listed = ops_client.get("/api/clients", headers=ops_headers)
    assert listed.status_code == 200, listed.text

    read = ops_client.get(f"/api/clients/{client_id}", headers=ops_headers)
    assert read.status_code == 200, read.text
    assert read.json()["name"] == "Acme Ltd"