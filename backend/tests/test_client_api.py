"""Client endpoints over HTTP (Requirement 5, with 2 for the role guards).

The claims worth an HTTP test are the ones about requests and responses: that a malformed email is a
field-level 422, that a duplicate company number is a 409 that names the conflict, that a delete is
refused with a conflict offering archival when the client's sites carry time entries, and that a site
manager has no access to the client endpoints at all. The deeper rules are pinned in
`test_client_service.py`; here the subject is the wiring.

The sign-in helper mirrors the one in `test_employee_api.py`: an administrator must have completed
2FA enrolment or every endpoint answers 403 (Requirement 1.6).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.models.user import UserRole
from auth_support import DEFAULT_PASSWORD


@pytest.fixture
def clients_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(clients_client, make_user):
    def _sign_in(role: UserRole) -> dict[str, str]:
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role)
        payload["username"] = user.username

        response = clients_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _sign_in


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


def _new_client_body(name: str = "Acme Construction", **overrides) -> dict[str, object]:
    body: dict[str, object] = {"name": name}
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- validation


def test_a_create_with_a_blank_name_is_a_field_level_error(clients_client, sign_in):
    """Requirement 5.2. A blank name is rejected with a 422 that names the field."""
    headers = sign_in(UserRole.ADMIN)
    response = clients_client.post("/api/clients", json=_new_client_body(name="   "), headers=headers)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("name" in str(item.get("loc", "")) for item in detail)


def test_a_malformed_email_is_a_field_level_error(clients_client, sign_in):
    """Requirement 5.5: email format is validated at the boundary."""
    headers = sign_in(UserRole.ADMIN)
    response = clients_client.post(
        "/api/clients", json=_new_client_body(email="not-an-email"), headers=headers
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("email" in str(item.get("loc", "")) for item in detail)


def test_a_valid_email_and_payment_terms_round_trip(clients_client, sign_in):
    """Requirement 5.5: payment terms are days plus a note, both preserved."""
    headers = sign_in(UserRole.ADMIN)
    response = clients_client.post(
        "/api/clients",
        json=_new_client_body(
            email="billing@acme.example",
            payment_terms_days=30,
            payment_terms_notes="net on delivery",
        ),
        headers=headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["email"] == "billing@acme.example"
    assert body["payment_terms_days"] == 30
    assert body["payment_terms_notes"] == "net on delivery"
    assert body["is_archived"] is False


# --------------------------------------------------------------------------- duplicate company number


def test_a_duplicate_company_number_is_a_conflict_that_names_the_holder(clients_client, sign_in):
    """Requirement 5.2."""
    headers = sign_in(UserRole.ADMIN)
    first = clients_client.post(
        "/api/clients", json=_new_client_body(company_number="514000000"), headers=headers
    )
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]

    duplicate = clients_client.post(
        "/api/clients",
        json=_new_client_body(name="Other Co", company_number="514000000"),
        headers=headers,
    )

    assert duplicate.status_code == 409
    error = duplicate.json()["detail"]["error"]
    assert error["code"] == "duplicate_company_number"
    assert error["params"]["client_id"] == first_id


# --------------------------------------------------------------------------- delete vs archive


def _seed_client_with_time_entry(clients_client, session: Session, headers) -> str:
    """Create a client through the API, then a site and a time entry directly, and return the id."""
    from app.models.employee import Employee
    from app.models.site import Site, SiteStatus
    from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus

    created = clients_client.post("/api/clients", json=_new_client_body(), headers=headers)
    assert created.status_code == 201, created.text
    client_id = created.json()["id"]

    site = Site(
        name="Site A",
        site_number="S-1",
        client_id=uuid.UUID(client_id),
        status=SiteStatus.ACTIVE,
        qr_token=f"token-{uuid.uuid4().hex}",
    )
    session.add(site)
    employee = Employee(
        full_name="Worker",
        full_name_en="Worker",
        passport_number="P0000009",
        passport_number_hash="P0000009",
        phone="+972500000000",
        country="Israel",
        emergency_contact_name="Contact",
        emergency_contact_phone="+972500000001",
        start_date=date(2025, 1, 1),
    )
    session.add(employee)
    session.commit()
    session.add(
        TimeEntry(
            employee_id=employee.id,
            site_id=site.id,
            work_date=date(2025, 8, 1),
            check_in_at=datetime(2025, 8, 1, 5, 0, tzinfo=UTC),
            source=TimeEntrySource.QR_SCAN,
            status=TimeEntryStatus.DRAFT,
            flags=[],
        )
    )
    session.commit()
    return client_id


def test_delete_is_refused_with_an_archive_action_when_time_entries_exist(
    clients_client, sign_in, session: Session
):
    """Requirement 5.4: the delete is a 409 that offers archival."""
    headers = sign_in(UserRole.ADMIN)
    client_id = _seed_client_with_time_entry(clients_client, session, headers)

    response = clients_client.delete(f"/api/clients/{client_id}", headers=headers)

    assert response.status_code == 409
    error = response.json()["detail"]["error"]
    assert error["code"] == "client_has_time_entries"
    assert "archive" in error["actions"]


def test_archival_succeeds_for_a_client_with_time_entries(clients_client, sign_in, session: Session):
    """Requirement 5.4: archival is the alternative, and it succeeds where the delete was refused."""
    headers = sign_in(UserRole.ADMIN)
    client_id = _seed_client_with_time_entry(clients_client, session, headers)

    response = clients_client.post(f"/api/clients/{client_id}/archive", headers=headers)

    assert response.status_code == 200
    assert response.json()["is_archived"] is True
    # Still readable afterwards: the row was not deleted.
    assert clients_client.get(f"/api/clients/{client_id}", headers=headers).status_code == 200


def test_a_client_with_no_time_entries_deletes(clients_client, sign_in):
    headers = sign_in(UserRole.ADMIN)
    created = clients_client.post("/api/clients", json=_new_client_body(), headers=headers)
    client_id = created.json()["id"]

    response = clients_client.delete(f"/api/clients/{client_id}", headers=headers)

    assert response.status_code == 204
    assert clients_client.get(f"/api/clients/{client_id}", headers=headers).status_code == 404


# --------------------------------------------------------------------------- sites listing


def test_client_sites_lists_the_owned_sites(clients_client, sign_in, session: Session):
    """Requirement 5.3."""
    from app.models.site import Site, SiteStatus

    headers = sign_in(UserRole.ADMIN)
    created = clients_client.post("/api/clients", json=_new_client_body(), headers=headers)
    client_id = created.json()["id"]
    session.add(
        Site(
            name="Site A",
            site_number="S-1",
            client_id=uuid.UUID(client_id),
            status=SiteStatus.ACTIVE,
            qr_token=f"token-{uuid.uuid4().hex}",
        )
    )
    session.commit()

    response = clients_client.get(f"/api/clients/{client_id}/sites", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["site_number"] == "S-1"


# --------------------------------------------------------------------------- authorization


def test_a_site_manager_cannot_read_clients(clients_client, sign_in):
    """Requirement 2.5: a client is a billing entity, so a site manager has no client endpoint."""
    manager = sign_in(UserRole.SITE_MANAGER)
    response = clients_client.get("/api/clients", headers=manager)

    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_a_site_manager_cannot_create_a_client(clients_client, sign_in):
    manager = sign_in(UserRole.SITE_MANAGER)
    response = clients_client.post("/api/clients", json=_new_client_body(), headers=manager)

    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_accounting_can_read_but_not_write(clients_client, sign_in):
    """Reads are open to accounting; writes are administrator-only."""
    accounting = sign_in(UserRole.ACCOUNTING)
    assert clients_client.get("/api/clients", headers=accounting).status_code == 200

    response = clients_client.post("/api/clients", json=_new_client_body(), headers=accounting)
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_reading_an_unknown_client_is_a_not_found(clients_client, sign_in):
    headers = sign_in(UserRole.ADMIN)
    response = clients_client.get(f"/api/clients/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 404
    assert _code(response) == "client_not_found"
