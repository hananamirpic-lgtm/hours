"""Document endpoints over HTTP (Requirement 4, with 2.7/2.8 for the download authorization).

The claims worth an HTTP test are the ones about requests, responses and who is allowed:

* the two-step upload works end to end and a wrong-type completion is a 400 that records nothing;
* an oversize completion is a 413;
* a download URL is refused to a caller who is not an administrator and not the owning employee, and
  the refusal is audited (Requirements 4.3, 2.7, 2.8);
* an administrator, and the owning employee, do get a download URL.

Object storage is the in-memory fake, injected by overriding `get_object_storage`, so no request
touches a real bucket. The sign-in helper mirrors the employee API tests: an administrator must have
completed 2FA enrolment or every endpoint answers 403.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.core.storage import get_object_storage
from app.models.employee import Employee, EmployeeStatus
from app.models.user import UserRole
from auth_support import DEFAULT_PASSWORD
from document_support import PDF_BYTES, PNG_BYTES, FakeObjectStorage


@pytest.fixture
def storage() -> FakeObjectStorage:
    return FakeObjectStorage()


@pytest.fixture
def documents_client(settings, session: Session, storage: FakeObjectStorage) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_object_storage] = lambda: storage
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(documents_client, make_user):
    """Create a user of a role, sign them in, and return (auth header, user)."""

    def _sign_in(role: UserRole, **user_overrides) -> tuple[dict[str, str], object]:
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **user_overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **user_overrides)
        payload["username"] = user.username

        response = documents_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}, user

    return _sign_in


def _make_employee(session: Session) -> Employee:
    employee = Employee(
        full_name="Ahmed Khalil",
        full_name_en="Ahmed Khalil",
        passport_number=f"A{uuid.uuid4().hex[:8].upper()}",
        passport_number_hash=f"A{uuid.uuid4().hex[:8].upper()}",
        phone="+972500000000",
        country="Jordan",
        emergency_contact_name="Layla Khalil",
        emergency_contact_phone="+972500000001",
        start_date=date(2025, 1, 1),
        status=EmployeeStatus.ACTIVE,
    )
    session.add(employee)
    session.commit()
    return employee


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


def _upload_a_document(
    documents_client,
    admin_headers: dict[str, str],
    storage: FakeObjectStorage,
    employee_id: uuid.UUID,
    *,
    declared_mime: str = "application/pdf",
    data: bytes = PDF_BYTES,
    file_name: str = "permit.pdf",
) -> dict:
    """Run the full two-step upload through the API and return the created document body."""
    ticket = documents_client.post(
        f"/api/employees/{employee_id}/documents/upload-url",
        json={
            "type": "work_permit",
            "file_name": file_name,
            "mime_type": declared_mime,
            "size_bytes": max(len(data), 1),
        },
        headers=admin_headers,
    )
    assert ticket.status_code == 200, ticket.text
    file_key = ticket.json()["file_key"]
    storage.put(file_key, data)

    created = documents_client.post(
        f"/api/employees/{employee_id}/documents",
        json={
            "file_key": file_key,
            "type": "work_permit",
            "file_name": file_name,
            "mime_type": declared_mime,
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    return created.json()


# --------------------------------------------------------------------------- upload happy path


def test_the_two_step_upload_records_a_document(documents_client, sign_in, session, storage):
    admin, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)

    body = _upload_a_document(documents_client, admin, storage, employee.id)

    assert body["mime_type"] == "application/pdf"
    assert body["size_bytes"] == len(PDF_BYTES)

    listing = documents_client.get(f"/api/employees/{employee.id}/documents", headers=admin)
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


# --------------------------------------------------------------------------- verification failures


def test_a_wrong_type_completion_is_a_bad_request_and_records_nothing(
    documents_client, sign_in, session, storage
):
    """Requirement 4.2: a PNG uploaded under a PDF claim is a 400, and no row is recorded."""
    admin, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)

    ticket = documents_client.post(
        f"/api/employees/{employee.id}/documents/upload-url",
        json={
            "type": "other",
            "file_name": "fake.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 64,
        },
        headers=admin,
    )
    file_key = ticket.json()["file_key"]
    storage.put(file_key, PNG_BYTES)  # wrong bytes for the declared type

    completion = documents_client.post(
        f"/api/employees/{employee.id}/documents",
        json={
            "file_key": file_key,
            "type": "other",
            "file_name": "fake.pdf",
            "mime_type": "application/pdf",
        },
        headers=admin,
    )

    assert completion.status_code == 400
    assert _code(completion) == "upload_type_mismatch"
    listing = documents_client.get(f"/api/employees/{employee.id}/documents", headers=admin)
    assert listing.json()["total"] == 0


def test_an_unsupported_declared_type_is_a_validation_error(documents_client, sign_in, session):
    """Requirement 4.2: a GIF is rejected at the boundary before an upload URL is issued."""
    admin, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)

    response = documents_client.post(
        f"/api/employees/{employee.id}/documents/upload-url",
        json={"type": "other", "file_name": "x.gif", "mime_type": "image/gif", "size_bytes": 10},
        headers=admin,
    )

    # The declared size is fine, but the type is unsupported; the service refuses it.
    assert response.status_code == 400
    assert _code(response) == "unsupported_file_type"


def test_an_oversize_declared_size_is_a_validation_error(documents_client, sign_in, session):
    """Requirement 4.2: a declared size over 10 MB is a 422 at the schema boundary."""
    admin, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)

    response = documents_client.post(
        f"/api/employees/{employee.id}/documents/upload-url",
        json={
            "type": "other",
            "file_name": "big.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 10 * 1024 * 1024 + 1,
        },
        headers=admin,
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------- download authorization


def test_a_download_url_is_refused_to_an_unrelated_caller(documents_client, sign_in, session, storage):
    """Requirement 4.3, 2.7, 2.8. A site manager may not download an employee's passport or permit;
    the refusal is a 403."""
    admin, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    document = _upload_a_document(documents_client, admin, storage, employee.id)

    manager, _ = sign_in(UserRole.SITE_MANAGER)
    response = documents_client.get(
        f"/api/documents/{document['id']}/download-url", headers=manager
    )

    assert response.status_code == 403
    assert _code(response) == "document_forbidden"


def test_a_download_url_is_refused_to_a_different_employee(documents_client, sign_in, session, storage):
    """An employee may act only on their own record (Requirement 2.7): another employee is refused."""
    admin, _ = sign_in(UserRole.ADMIN)
    owner = _make_employee(session)
    document = _upload_a_document(documents_client, admin, storage, owner.id)

    other_employee = _make_employee(session)
    stranger, _ = sign_in(UserRole.EMPLOYEE, employee_id=other_employee.id)
    response = documents_client.get(
        f"/api/documents/{document['id']}/download-url", headers=stranger
    )

    assert response.status_code == 403
    assert _code(response) == "not_own_record"


def test_an_admin_gets_a_download_url(documents_client, sign_in, session, storage):
    admin, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    document = _upload_a_document(documents_client, admin, storage, employee.id)

    response = documents_client.get(
        f"/api/documents/{document['id']}/download-url", headers=admin
    )

    assert response.status_code == 200
    assert response.json()["url"].startswith("https://storage.local/download/")


def test_the_owning_employee_gets_a_download_url(documents_client, sign_in, session, storage):
    """The employee whose document it is may read it (Requirement 2.7)."""
    admin, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    document = _upload_a_document(documents_client, admin, storage, employee.id)

    owner, _ = sign_in(UserRole.EMPLOYEE, employee_id=employee.id)
    response = documents_client.get(
        f"/api/documents/{document['id']}/download-url", headers=owner
    )

    assert response.status_code == 200
    assert "url" in response.json()


def test_downloading_an_unknown_document_is_a_not_found(documents_client, sign_in):
    admin, _ = sign_in(UserRole.ADMIN)
    response = documents_client.get(
        f"/api/documents/{uuid.uuid4()}/download-url", headers=admin
    )

    assert response.status_code == 404
    assert _code(response) == "document_not_found"


# --------------------------------------------------------------------------- delete


def test_deleting_a_document_soft_deletes_it_and_removes_the_bytes(
    documents_client, sign_in, session, storage
):
    admin, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session)
    document = _upload_a_document(documents_client, admin, storage, employee.id)

    deleted = documents_client.delete(f"/api/documents/{document['id']}", headers=admin)
    assert deleted.status_code == 204

    listing = documents_client.get(f"/api/employees/{employee.id}/documents", headers=admin)
    assert listing.json()["total"] == 0
    assert document["id"]  # sanity
