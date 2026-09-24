"""Employee endpoints over HTTP (Requirement 3, with 2.5 for the site-manager redaction).

The claims worth an HTTP test are the ones about requests and responses: that a create with a blank
mandatory field is a field-level 422, that a duplicate passport is a 409 that names the conflict, and
that a site manager reading an employee card receives no wage fields (Requirement 2.5) while an admin
does. The deeper rules — uniqueness over non-terminated employees, rate resolution — are pinned in
`test_employee_service.py`; here the subject is the wiring.

The sign-in helper mirrors the one in `test_authorization_matrix.py`: an administrator must have
completed 2FA enrolment or every endpoint answers 403 (Requirement 1.6).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.models.staffing_company import StaffingCompany
from app.models.user import UserRole
from auth_support import DEFAULT_PASSWORD


@pytest.fixture
def employees_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(employees_client, make_user, session: Session):
    """Create a user of a role, sign them in, and return the auth header."""

    def _sign_in(role: UserRole) -> dict[str, str]:
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role)
        payload["username"] = user.username

        response = employees_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _sign_in


#: The id of a staffing company seeded once per test by the autouse fixture below, so a create body
#: can satisfy the mandatory employee-to-company link (Requirement 2.1) without every test spelling it
#: out. A list so the fixture can rebind it per test without a `global` statement.
_seeded_staffing_company_id: list[str] = []


@pytest.fixture(autouse=True)
def _seed_staffing_company(session: Session):
    """Seed one staffing company so `_new_employee_body` can link new employees to it."""
    company = StaffingCompany(name="Acme Staffing", contact_person="Dana Levi")
    session.add(company)
    session.commit()
    _seeded_staffing_company_id[:] = [str(company.id)]
    yield
    _seeded_staffing_company_id.clear()


def _new_employee_body(passport: str = "A1234567", **overrides) -> dict[str, object]:
    body: dict[str, object] = {
        "full_name": "Ahmed Khalil",
        "full_name_en": "Ahmed Khalil",
        "passport_number": passport,
        "phone": "+972500000000",
        "country": "Jordan",
        "emergency_contact_name": "Layla Khalil",
        "emergency_contact_phone": "+972500000001",
        "start_date": "2025-01-01",
        "staffing_company_id": _seeded_staffing_company_id[0] if _seeded_staffing_company_id else None,
        "rate": {
            "hourly_wage": "35.00",
            "overtime_rate": "43.75",
            "shabbat_holiday_rate": "70.00",
            "effective_from": "2025-01-01",
        },
    }
    body.update(overrides)
    return body


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


# --------------------------------------------------------------------------- mandatory fields


def test_a_create_with_a_blank_mandatory_field_is_a_field_level_error(employees_client, sign_in):
    """Requirement 3.5. A blank name is rejected before it reaches the service, with a 422 that names
    the field so the front end can mark it."""
    headers = sign_in(UserRole.ADMIN)
    body = _new_employee_body(full_name="   ")

    response = employees_client.post("/api/employees", json=body, headers=headers)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("full_name" in str(item.get("loc", "")) for item in detail)


def test_a_create_missing_a_mandatory_field_is_rejected(employees_client, sign_in):
    """Requirement 1.1, 1.3. A missing mandatory field (here full_name) is rejected with a 422."""
    headers = sign_in(UserRole.ADMIN)
    body = _new_employee_body()
    del body["full_name"]

    response = employees_client.post("/api/employees", json=body, headers=headers)

    assert response.status_code == 422


def test_a_create_omitting_optional_fields_succeeds(employees_client, sign_in):
    """Requirement 1.2, 1.4. country, emergency_contact_name and emergency_contact_phone are optional:
    omitting them is accepted and carried through as null, and the employee is still created."""
    headers = sign_in(UserRole.ADMIN)
    body = _new_employee_body()
    del body["country"]
    del body["emergency_contact_name"]
    del body["emergency_contact_phone"]

    response = employees_client.post("/api/employees", json=body, headers=headers)

    assert response.status_code == 201, response.text
    created = response.json()
    assert created["country"] is None
    assert created["emergency_contact_name"] is None
    assert created["emergency_contact_phone"] is None


# --------------------------------------------------------------------------- duplicate passport


def test_a_duplicate_passport_is_a_conflict_that_names_the_holder(employees_client, sign_in):
    """Requirement 3.6."""
    headers = sign_in(UserRole.ADMIN)
    first = employees_client.post(
        "/api/employees", json=_new_employee_body(passport="A1234567"), headers=headers
    )
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]

    duplicate = employees_client.post(
        "/api/employees",
        json=_new_employee_body(passport="a1234567", full_name="Different Person"),
        headers=headers,
    )

    assert duplicate.status_code == 409
    body = duplicate.json()["detail"]["error"]
    assert body["code"] == "duplicate_passport"
    assert body["params"]["employee_id"] == first_id


# --------------------------------------------------------------------------- wage redaction


def test_a_site_manager_reading_an_employee_gets_no_wage_fields(employees_client, sign_in):
    """Requirement 2.5: the card served to a site manager has no wage fields at all — absent, not
    null — while the non-wage detail is intact."""
    admin = sign_in(UserRole.ADMIN)
    created = employees_client.post("/api/employees", json=_new_employee_body(), headers=admin)
    assert created.status_code == 201, created.text
    employee_id = created.json()["id"]

    manager = sign_in(UserRole.SITE_MANAGER)
    response = employees_client.get(f"/api/employees/{employee_id}", headers=manager)

    assert response.status_code == 200
    body = response.json()
    assert body["full_name"] == "Ahmed Khalil"
    for key in ("hourly_wage", "overtime_rate", "shabbat_holiday_rate", "travel_allowance_daily", "rates"):
        assert key not in body


def test_an_admin_reading_the_same_employee_gets_the_wage_fields(employees_client, sign_in):
    """The other half of the redaction claim, so the test cannot pass by serving nobody the wage."""
    admin = sign_in(UserRole.ADMIN)
    created = employees_client.post("/api/employees", json=_new_employee_body(), headers=admin)
    employee_id = created.json()["id"]

    response = employees_client.get(f"/api/employees/{employee_id}", headers=admin)

    body = response.json()
    assert body["hourly_wage"] == "35.00"
    assert body["rates"][0]["hourly_wage"] == "35.00"


def test_a_site_manager_cannot_create_an_employee(employees_client, sign_in):
    """Writes are admin-only; a site manager reads but does not edit the personnel record."""
    manager = sign_in(UserRole.SITE_MANAGER)
    response = employees_client.post("/api/employees", json=_new_employee_body(), headers=manager)

    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


# --------------------------------------------------------------------------- status and rates round trip


def test_status_change_has_no_hard_delete_and_reports_the_new_status(employees_client, sign_in):
    admin = sign_in(UserRole.ADMIN)
    created = employees_client.post("/api/employees", json=_new_employee_body(), headers=admin)
    employee_id = created.json()["id"]

    response = employees_client.patch(
        f"/api/employees/{employee_id}/status",
        json={"status": "terminated", "reason": "contract ended"},
        headers=admin,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "terminated"
    # Still readable afterwards: the row was not deleted.
    assert employees_client.get(f"/api/employees/{employee_id}", headers=admin).status_code == 200


def test_replacing_rates_rejects_an_overlapping_chain(employees_client, sign_in):
    admin = sign_in(UserRole.ADMIN)
    created = employees_client.post("/api/employees", json=_new_employee_body(), headers=admin)
    employee_id = created.json()["id"]

    response = employees_client.put(
        f"/api/employees/{employee_id}/rates",
        json={
            "rates": [
                {
                    "hourly_wage": "30.00",
                    "overtime_rate": "0",
                    "shabbat_holiday_rate": "0",
                    "effective_from": "2025-08-01",
                    "effective_to": "2025-08-20",
                },
                {
                    "hourly_wage": "35.00",
                    "overtime_rate": "0",
                    "shabbat_holiday_rate": "0",
                    "effective_from": "2025-08-15",
                },
            ]
        },
        headers=admin,
    )

    assert response.status_code == 400
    assert _code(response) == "overlapping_rates"


def test_reading_an_unknown_employee_is_a_not_found(employees_client, sign_in):
    admin = sign_in(UserRole.ADMIN)
    response = employees_client.get(f"/api/employees/{uuid.uuid4()}", headers=admin)

    assert response.status_code == 404
    assert _code(response) == "employee_not_found"
