"""Self-service profile photo endpoints over HTTP (Requirement 3.1, with 2.7 self-scoping).

The claims worth an HTTP test are the ones about who is allowed and what is touched:

* an employee gets an upload URL for image/jpeg and for image/png;
* a PDF (a valid *document* type, not a valid photo) is refused;
* completing the upload sets `photo_key` on THEIR OWN employee and records an audit row;
* GET /api/me/photo returns a url when a photo is set and null when none;
* the endpoints act only on the caller's own employee — a second employee's photo is never touched;
* a login with no linked employee is refused cleanly.

Object storage is the in-memory fake, injected by overriding `get_object_storage`, so no request
touches a real bucket. The sign-in helper mirrors the document/scan API tests: an administrator must
have completed 2FA enrolment or every endpoint answers 403, and an employee sign-in links an
`employee_id`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date

import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.storage import get_object_storage
from app.models.change_log import ChangeLog
from app.models.employee import Employee, EmployeeStatus
from app.models.user import UserRole
from auth_support import DEFAULT_PASSWORD
from document_support import JPEG_BYTES, PDF_BYTES, PNG_BYTES, FakeObjectStorage


@pytest.fixture
def storage() -> FakeObjectStorage:
    return FakeObjectStorage()


@pytest.fixture
def photo_client(settings, session: Session, storage: FakeObjectStorage) -> Iterator:
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
def sign_in(photo_client, make_user):
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

        response = photo_client.post("/api/auth/login", json=payload)
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


def _set_photo(
    photo_client,
    headers: dict[str, str],
    storage: FakeObjectStorage,
    *,
    declared_mime: str = "image/jpeg",
    data: bytes = JPEG_BYTES,
) -> dict:
    """Run the full two-step self-service photo upload through the API and return the response body."""
    ticket = photo_client.post(
        "/api/me/photo/upload-url",
        json={"mime_type": declared_mime},
        headers=headers,
    )
    assert ticket.status_code == 200, ticket.text
    file_key = ticket.json()["file_key"]
    storage.put(file_key, data)

    completed = photo_client.post(
        "/api/me/photo",
        json={"file_key": file_key, "mime_type": declared_mime},
        headers=headers,
    )
    assert completed.status_code == 200, completed.text
    return completed.json()


# --------------------------------------------------------------------------- upload URL


@pytest.mark.parametrize(
    ("declared_mime", "data"),
    [("image/jpeg", JPEG_BYTES), ("image/png", PNG_BYTES)],
)
def test_an_employee_gets_an_upload_url_for_an_image(
    photo_client, sign_in, session, declared_mime, data
):
    """Requirement 3.1, 4.2: JPEG and PNG each get a constrained presigned URL."""
    employee = _make_employee(session)
    headers, _ = sign_in(UserRole.EMPLOYEE, employee_id=employee.id)

    response = photo_client.post(
        "/api/me/photo/upload-url", json={"mime_type": declared_mime}, headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["required_headers"]["Content-Type"] == declared_mime
    assert body["max_bytes"] == 10 * 1024 * 1024
    assert body["file_key"]


def test_a_pdf_upload_url_is_refused(photo_client, sign_in, session):
    """Requirement 3.1: a photo is images only, so a PDF is refused even though it is a valid
    document type."""
    employee = _make_employee(session)
    headers, _ = sign_in(UserRole.EMPLOYEE, employee_id=employee.id)

    response = photo_client.post(
        "/api/me/photo/upload-url", json={"mime_type": "application/pdf"}, headers=headers
    )

    assert response.status_code == 400
    assert _code(response) == "unsupported_file_type"


# --------------------------------------------------------------------------- completion


def test_completing_sets_photo_key_on_the_callers_own_employee_and_audits_it(
    photo_client, sign_in, session, storage
):
    """Requirement 3.1, 4.2, 13.2: a verified image sets photo_key and writes an audit row."""
    employee = _make_employee(session)
    headers, _ = sign_in(UserRole.EMPLOYEE, employee_id=employee.id)

    body = _set_photo(photo_client, headers, storage)

    session.expire_all()
    refreshed = session.get(Employee, employee.id)
    assert refreshed.photo_key == body["photo_key"]
    assert body["url"].startswith("https://storage.local/download/")

    audit = session.scalars(
        select(ChangeLog)
        .where(ChangeLog.entity_type == "employees")
        .where(ChangeLog.entity_id == employee.id)
        .where(ChangeLog.field == "photo_key")
        .where(ChangeLog.reason == "photo_uploaded")
    ).all()
    assert len(audit) == 1


def test_a_wrong_type_completion_is_refused_and_leaves_the_photo_unchanged(
    photo_client, sign_in, session, storage
):
    """Requirement 4.2: bytes that are not the declared image type are a 400, and nothing is set."""
    employee = _make_employee(session)
    headers, _ = sign_in(UserRole.EMPLOYEE, employee_id=employee.id)

    ticket = photo_client.post(
        "/api/me/photo/upload-url", json={"mime_type": "image/png"}, headers=headers
    )
    file_key = ticket.json()["file_key"]
    storage.put(file_key, JPEG_BYTES)  # JPEG bytes under a PNG claim

    completion = photo_client.post(
        "/api/me/photo",
        json={"file_key": file_key, "mime_type": "image/png"},
        headers=headers,
    )

    assert completion.status_code == 400
    assert _code(completion) == "upload_type_mismatch"
    session.expire_all()
    assert session.get(Employee, employee.id).photo_key is None


def test_a_pdf_completion_is_refused(photo_client, sign_in, session, storage):
    """A PDF completion is refused server-side even if a key were obtained for it."""
    employee = _make_employee(session)
    headers, _ = sign_in(UserRole.EMPLOYEE, employee_id=employee.id)

    file_key = f"employees/{employee.id}/{uuid.uuid4()}/photo.pdf"
    storage.put(file_key, PDF_BYTES)

    completion = photo_client.post(
        "/api/me/photo",
        json={"file_key": file_key, "mime_type": "application/pdf"},
        headers=headers,
    )

    assert completion.status_code == 400
    assert _code(completion) == "unsupported_file_type"


def test_replacing_a_photo_removes_the_previous_object(photo_client, sign_in, session, storage):
    """Replacing a photo sets the new key and best-effort deletes the old object."""
    employee = _make_employee(session)
    headers, _ = sign_in(UserRole.EMPLOYEE, employee_id=employee.id)

    first = _set_photo(photo_client, headers, storage, declared_mime="image/jpeg", data=JPEG_BYTES)
    second = _set_photo(photo_client, headers, storage, declared_mime="image/png", data=PNG_BYTES)

    assert first["photo_key"] != second["photo_key"]
    assert first["photo_key"] in storage.deleted_keys


# --------------------------------------------------------------------------- read


def test_get_photo_returns_a_url_when_set_and_null_when_not(photo_client, sign_in, session, storage):
    """Requirement 3.1: the caller's own photo as a short-lived URL, or null when none."""
    employee = _make_employee(session)
    headers, _ = sign_in(UserRole.EMPLOYEE, employee_id=employee.id)

    before = photo_client.get("/api/me/photo", headers=headers)
    assert before.status_code == 200
    assert before.json()["url"] is None

    _set_photo(photo_client, headers, storage)

    after = photo_client.get("/api/me/photo", headers=headers)
    assert after.status_code == 200
    assert after.json()["url"].startswith("https://storage.local/download/")


# --------------------------------------------------------------------------- self-scoping


def test_a_second_employees_photo_is_never_touched(photo_client, sign_in, session, storage):
    """Requirement 2.7: the endpoints act only on the caller's own record.

    One employee sets their photo; a second employee has none and their row is untouched no matter
    what the first did. There is no field in any request that could name the other person."""
    owner = _make_employee(session)
    other = _make_employee(session)

    owner_headers, _ = sign_in(UserRole.EMPLOYEE, employee_id=owner.id)
    _set_photo(photo_client, owner_headers, storage)

    session.expire_all()
    assert session.get(Employee, owner.id).photo_key is not None
    assert session.get(Employee, other.id).photo_key is None

    # The second employee reads their own photo and sees none — never the owner's.
    other_headers, _ = sign_in(UserRole.EMPLOYEE, employee_id=other.id)
    response = photo_client.get("/api/me/photo", headers=other_headers)
    assert response.status_code == 200
    assert response.json()["url"] is None


# --------------------------------------------------------------------------- not linked


def test_a_login_with_no_linked_employee_is_refused_cleanly(photo_client, sign_in):
    """A login admitted by the guard but not linked to an employee has no photo of its own.

    The guard admits the employee role and the administrator (Requirement 2.2). An administrator
    carries no `employee_id`, so it is the login that reaches the endpoint yet has no personal record
    — and every endpoint refuses it with a clean `no_employee_for_caller` 403 rather than acting on
    nobody. (A site manager is not admitted at all; that is a separate `insufficient_role` refusal.)"""
    headers, _ = sign_in(UserRole.ADMIN)

    get_response = photo_client.get("/api/me/photo", headers=headers)
    assert get_response.status_code == 403, get_response.text
    assert _code(get_response) == "no_employee_for_caller"

    for path, body in (
        ("/api/me/photo/upload-url", {"mime_type": "image/png"}),
        ("/api/me/photo", {"file_key": "k", "mime_type": "image/png"}),
    ):
        response = photo_client.post(path, json=body, headers=headers)
        assert response.status_code == 403, (path, response.text)
        assert _code(response) == "no_employee_for_caller"


def test_a_role_that_is_not_an_employee_is_not_admitted(photo_client, sign_in):
    """A site manager is not admitted to the employee-self endpoints at all (insufficient_role)."""
    headers, _ = sign_in(UserRole.SITE_MANAGER)

    response = photo_client.get("/api/me/photo", headers=headers)
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"
