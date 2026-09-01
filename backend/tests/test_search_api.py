"""Global search over HTTP and through the service (Requirement 22.1, 22.2, 22.4, 22.5, 21.5).

The system provides a global search across employees, sites and clients (Requirement 22.1) matching a
partial name in either language (Requirement 21.5, 22.4), a site number, a client phone and a
passport number, case- and accent-insensitive (Requirement 22.4), scoped to what the caller may see
(Requirement 22.2) and paged with a stable order (Requirement 22.5).

The matching rules live in the search service so a few tests call it directly to exercise folding and
scoping, and the rest go over HTTP to exercise the route, the scope and the grouped response shape.
The sign-in and make-* helpers mirror `test_missing_reports_api.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.core.authz import SiteScope
from app.models.client import Client
from app.models.employee import Employee
from app.models.site import EmployeeSite, Site
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.models.user import UserRole
from app.models.user_site import UserSite
from app.services import search as search_service
from auth_support import DEFAULT_PASSWORD

# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def search_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(search_client, make_user):
    def _sign_in(role: UserRole, **user_overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **user_overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **user_overrides)
        payload["username"] = user.username

        response = search_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        header = {"Authorization": f"Bearer {response.json()['access_token']}"}
        return header, user

    return _sign_in


_PASSPORT = iter(f"K{n:07d}" for n in range(1, 1_000_000))


def _make_employee(
    session: Session,
    *,
    full_name: str = "עובד",
    full_name_en: str = "Worker",
    passport: str | None = None,
    phone: str = "+972500000000",
) -> Employee:
    number = passport or next(_PASSPORT)
    employee = Employee(
        full_name=full_name,
        full_name_en=full_name_en,
        passport_number=number,
        # Plaintext assigned to both: the DeterministicHash type hashes it on the way to the column.
        passport_number_hash=number,
        phone=phone,
        country="Israel",
        emergency_contact_name="Contact",
        emergency_contact_phone="+972500000001",
        start_date=date(2025, 1, 1),
    )
    session.add(employee)
    session.commit()
    return employee


def _make_client(
    session: Session,
    *,
    name: str = "Acme",
    phone: str | None = None,
    company: str | None = None,
) -> Client:
    client = Client(name=name, phone=phone, company=company)
    session.add(client)
    session.commit()
    return client


def _make_site(session: Session, *, client: Client, number: str, name: str | None = None) -> Site:
    site = Site(
        name=name or f"Site {number}",
        site_number=number,
        client_id=client.id,
        qr_token=f"placeholder-{uuid.uuid4().hex}",
        qr_token_version=1,
    )
    session.add(site)
    session.commit()
    return site


def _assign(session: Session, *, employee: Employee, site: Site) -> None:
    session.add(
        EmployeeSite(employee_id=employee.id, site_id=site.id, assigned_from=date(2025, 1, 1))
    )
    session.commit()


def _worked(session: Session, *, employee: Employee, site: Site, day: int = 4) -> TimeEntry:
    local_in = datetime(2025, 8, day, 8, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
    check_in = local_in.astimezone(UTC)
    entry = TimeEntry(
        employee_id=employee.id,
        site_id=site.id,
        work_date=date(2025, 8, day),
        check_in_at=check_in,
        check_out_at=check_in + timedelta(minutes=480),
        total_minutes=480,
        source=TimeEntrySource.QR_SCAN,
        is_manual=False,
        status=TimeEntryStatus.APPROVED,
        flags=[],
    )
    session.add(entry)
    session.commit()
    return entry


def _assign_manager(session: Session, user, site: Site) -> None:
    session.add(UserSite(user_id=user.id, site_id=site.id))
    session.commit()


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


# ===================================================================== name forms (21.5, 22.4)


def test_hebrew_name_form_matches(search_client, sign_in, session: Session):
    """Requirement 21.5, 22.4: the Hebrew name form matches a Hebrew search term."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session, full_name="אברהם כהן", full_name_en="Abraham Cohen")

    response = search_client.get("/api/search?q=אברהם", headers=headers)
    assert response.status_code == 200, response.text
    ids = {hit["id"] for hit in response.json()["employees"]["items"]}
    assert str(employee.id) in ids


def test_english_name_form_matches(search_client, sign_in, session: Session):
    """Requirement 21.5, 22.4: the English name form matches an English search term for the same
    record, so a bilingual name is found either way."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session, full_name="אברהם כהן", full_name_en="Abraham Cohen")

    response = search_client.get("/api/search?q=abraham", headers=headers)
    assert response.status_code == 200, response.text
    ids = {hit["id"] for hit in response.json()["employees"]["items"]}
    assert str(employee.id) in ids


def test_partial_name_matches(search_client, sign_in, session: Session):
    """Requirement 22.1: a partial name matches, not only a whole one."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session, full_name="דוד לוי", full_name_en="David Levi")

    response = search_client.get("/api/search?q=lev", headers=headers)
    assert response.status_code == 200, response.text
    ids = {hit["id"] for hit in response.json()["employees"]["items"]}
    assert str(employee.id) in ids


def test_case_insensitive(search_client, sign_in, session: Session):
    """Requirement 22.4: an upper-case term matches a mixed-case name."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session, full_name_en="Sarah Klein")

    response = search_client.get("/api/search?q=SARAH", headers=headers)
    assert response.status_code == 200, response.text
    ids = {hit["id"] for hit in response.json()["employees"]["items"]}
    assert str(employee.id) in ids


def test_accent_insensitive(search_client, sign_in, session: Session):
    """Requirement 22.4: an unaccented term matches an accented name and vice versa."""
    headers, _ = sign_in(UserRole.ADMIN)
    accented = _make_employee(session, full_name_en="José García")
    plain = _make_employee(session, full_name_en="Jose Garcia")

    # Unaccented query reaches the accented row.
    unaccented = search_client.get("/api/search?q=jose", headers=headers).json()
    ids = {hit["id"] for hit in unaccented["employees"]["items"]}
    assert str(accented.id) in ids
    assert str(plain.id) in ids

    # Accented query reaches the unaccented row.
    with_accent = search_client.get("/api/search?q=josé", headers=headers).json()
    ids = {hit["id"] for hit in with_accent["employees"]["items"]}
    assert str(accented.id) in ids
    assert str(plain.id) in ids


# ===================================================================== passport, site number, phone (22.1)


def test_passport_exact_match(search_client, sign_in, session: Session):
    """Requirement 22.1: a passport number matches its holder (exactly, via the deterministic hash)."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session, passport="X1234567")

    response = search_client.get("/api/search?q=X1234567", headers=headers)
    assert response.status_code == 200, response.text
    ids = {hit["id"] for hit in response.json()["employees"]["items"]}
    assert str(employee.id) in ids


def test_passport_case_insensitive(search_client, sign_in, session: Session):
    """Requirement 22.4: passport matching is case-insensitive, matching the uniqueness hash's
    normalisation (the hash upper-cases before digesting)."""
    headers, _ = sign_in(UserRole.ADMIN)
    employee = _make_employee(session, passport="AB99887766")

    response = search_client.get("/api/search?q=ab99887766", headers=headers)
    assert response.status_code == 200, response.text
    ids = {hit["id"] for hit in response.json()["employees"]["items"]}
    assert str(employee.id) in ids


def test_site_number_matches(search_client, sign_in, session: Session):
    """Requirement 22.1: a site number matches its site."""
    headers, _ = sign_in(UserRole.ADMIN)
    site = _make_site(session, client=_make_client(session), number="SITE-042", name="North Tower")

    response = search_client.get("/api/search?q=042", headers=headers)
    assert response.status_code == 200, response.text
    ids = {hit["id"] for hit in response.json()["sites"]["items"]}
    assert str(site.id) in ids


def test_site_name_matches(search_client, sign_in, session: Session):
    """Requirement 22.1: a site name matches its site."""
    headers, _ = sign_in(UserRole.ADMIN)
    site = _make_site(session, client=_make_client(session), number="S1", name="Rosh Tzurim")

    response = search_client.get("/api/search?q=tzurim", headers=headers)
    assert response.status_code == 200, response.text
    ids = {hit["id"] for hit in response.json()["sites"]["items"]}
    assert str(site.id) in ids


def test_client_name_matches(search_client, sign_in, session: Session):
    """Requirement 22.1: a client name matches its client."""
    headers, _ = sign_in(UserRole.ADMIN)
    client = _make_client(session, name="Globex Corporation")

    response = search_client.get("/api/search?q=globex", headers=headers)
    assert response.status_code == 200, response.text
    ids = {hit["id"] for hit in response.json()["clients"]["items"]}
    assert str(client.id) in ids


def test_client_phone_matches(search_client, sign_in, session: Session):
    """Requirement 22.1: a client's (plaintext) phone matches partially."""
    headers, _ = sign_in(UserRole.ADMIN)
    client = _make_client(session, name="Initech", phone="+97235551234")

    response = search_client.get("/api/search?q=5551234", headers=headers)
    assert response.status_code == 200, response.text
    ids = {hit["id"] for hit in response.json()["clients"]["items"]}
    assert str(client.id) in ids


# ===================================================================== scope by role (22.2, 2.3)


def test_site_manager_scoped_to_their_sites(search_client, sign_in, session: Session):
    """Requirement 22.2, 2.3: a site manager finds only the sites, employees and clients attached to
    their assigned sites."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)

    client_a = _make_client(session, name="Match Alpha")
    client_b = _make_client(session, name="Match Beta")
    site_a = _make_site(session, client=client_a, number="MA", name="Match Site A")
    site_b = _make_site(session, client=client_b, number="MB", name="Match Site B")
    emp_a = _make_employee(session, full_name_en="Match Ann")
    emp_b = _make_employee(session, full_name_en="Match Ben")
    _assign(session, employee=emp_a, site=site_a)
    _assign(session, employee=emp_b, site=site_b)
    _assign_manager(session, manager, site_a)

    body = search_client.get("/api/search?q=match", headers=manager_headers).json()
    assert {h["id"] for h in body["sites"]["items"]} == {str(site_a.id)}
    assert {h["id"] for h in body["employees"]["items"]} == {str(emp_a.id)}
    assert {h["id"] for h in body["clients"]["items"]} == {str(client_a.id)}

    admin_body = search_client.get("/api/search?q=match", headers=admin_headers).json()
    assert {h["id"] for h in admin_body["sites"]["items"]} == {str(site_a.id), str(site_b.id)}
    assert {h["id"] for h in admin_body["employees"]["items"]} == {str(emp_a.id), str(emp_b.id)}
    assert {h["id"] for h in admin_body["clients"]["items"]} == {str(client_a.id), str(client_b.id)}


def test_site_manager_finds_employee_who_worked_their_site(search_client, sign_in, session: Session):
    """Requirement 2.4: a manager can find an employee who recorded time at their site even without a
    standing assignment."""
    manager_headers, manager = sign_in(UserRole.SITE_MANAGER)
    site = _make_site(session, client=_make_client(session), number="W1", name="Worked Site")
    employee = _make_employee(session, full_name_en="Transient Tom")
    _worked(session, employee=employee, site=site)
    _assign_manager(session, manager, site)

    body = search_client.get("/api/search?q=transient", headers=manager_headers).json()
    assert {h["id"] for h in body["employees"]["items"]} == {str(employee.id)}


def test_manager_with_no_assignment_finds_nothing(search_client, sign_in, session: Session):
    """An empty scope means nothing, not everything (SiteScope invariant)."""
    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)
    _make_employee(session, full_name_en="Lonely Larry")
    _make_site(session, client=_make_client(session, name="Lonely Client"), number="L1", name="Lonely")

    body = search_client.get("/api/search?q=lonely", headers=manager_headers).json()
    assert body["employees"]["total"] == 0
    assert body["sites"]["total"] == 0
    assert body["clients"]["total"] == 0


def test_accounting_searches_the_whole_business(search_client, sign_in, session: Session):
    """Requirement 2.6: accounting is unrestricted, so it finds every matching record."""
    headers, _ = sign_in(UserRole.ACCOUNTING)
    employee = _make_employee(session, full_name_en="Wide Walter")
    wide_client = _make_client(session, name="Wide Client")
    site = _make_site(session, client=wide_client, number="WD", name="Wide Site")

    body = search_client.get("/api/search?q=wide", headers=headers).json()
    assert str(employee.id) in {h["id"] for h in body["employees"]["items"]}
    assert str(site.id) in {h["id"] for h in body["sites"]["items"]}


def test_employee_role_finds_nothing(search_client, sign_in, session: Session):
    """Requirement 2.7: an employee's scope is their own record, not a site, so global search returns
    nothing rather than the business."""
    employee_headers, _ = sign_in(UserRole.EMPLOYEE)
    _make_employee(session, full_name_en="Someone Else")

    body = search_client.get("/api/search?q=someone", headers=employee_headers).json()
    assert body["employees"]["total"] == 0


# ===================================================================== envelope, paging, blank (22.5)


def test_response_is_grouped_and_echoes_query(search_client, sign_in, session: Session):
    """The response groups hits by kind and echoes the term (Requirement 22.1)."""
    headers, _ = sign_in(UserRole.ADMIN)
    body = search_client.get("/api/search?q=nothingmatches", headers=headers).json()
    assert body["query"] == "nothingmatches"
    assert set(body) >= {"employees", "sites", "clients"}
    for group in ("employees", "sites", "clients"):
        assert body[group]["items"] == []
        assert body[group]["total"] == 0


def test_blank_term_returns_empty(search_client, sign_in, session: Session):
    """A blank term is not a request for the whole database (Requirement 22.5 guardrail)."""
    headers, _ = sign_in(UserRole.ADMIN)
    _make_employee(session, full_name_en="Present Person")

    body = search_client.get("/api/search?q=", headers=headers).json()
    assert body["employees"]["total"] == 0


def test_pagination_is_stable(search_client, sign_in, session: Session):
    """Requirement 22.5: results page with a stable order, so consecutive pages do not overlap."""
    headers, _ = sign_in(UserRole.ADMIN)
    for index in range(5):
        _make_employee(session, full_name_en=f"Pager {index:02d}")

    first = search_client.get("/api/search?q=pager&limit=2&offset=0", headers=headers).json()
    second = search_client.get("/api/search?q=pager&limit=2&offset=2", headers=headers).json()

    assert first["employees"]["total"] == 5
    assert len(first["employees"]["items"]) == 2
    assert len(second["employees"]["items"]) == 2
    first_ids = [h["id"] for h in first["employees"]["items"]]
    second_ids = [h["id"] for h in second["employees"]["items"]]
    assert set(first_ids).isdisjoint(second_ids)


def test_requires_authentication(search_client, session: Session):
    """The endpoint is authenticated like every other non-health route."""
    response = search_client.get("/api/search?q=anything")
    assert response.status_code == 401


# ===================================================================== service directly


def test_service_folds_accents_and_case():
    """The fold used for matching lower-cases and strips accents (Requirement 22.4)."""
    assert search_service.fold("José") == "jose"
    assert search_service.fold("  ÀBÇ ") == "abc"
    assert search_service.fold("אברהם") == "אברהם"


def test_service_scopes_employees(session: Session):
    """Through the service: a restricted scope narrows employees to the scoped sites (Requirement 22.2)."""
    client = _make_client(session, name="Svc Client")
    site_in = _make_site(session, client=client, number="IN", name="Svc In")
    site_out = _make_site(session, client=client, number="OUT", name="Svc Out")
    emp_in = _make_employee(session, full_name_en="Svc Insider")
    emp_out = _make_employee(session, full_name_en="Svc Outsider")
    _assign(session, employee=emp_in, site=site_in)
    _assign(session, employee=emp_out, site=site_out)

    results = search_service.search(
        session, term="svc", scope=SiteScope.limited_to([site_in.id])
    )
    ids = {hit.id for hit in results.employees.items}
    assert ids == {emp_in.id}
