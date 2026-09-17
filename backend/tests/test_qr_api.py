"""QR download and rotation over HTTP (Requirement 8.5, 8.6, 8.7).

The HTTP claims: a site's QR downloads as a PNG and a PDF; a separate-mode site needs its action
named; regeneration bumps the stored version and mints a new stored token; and a token minted before
a regeneration no longer verifies against the site afterwards, which is the scan-side revocation of a
printed code (Requirement 8.6, 8.7). The token cryptography itself is pinned in `test_qr_token.py`
and the rendering in `test_qr_render.py`; here the subject is the wiring and the state change on the
row.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.core import qr_token
from app.models.client import Client
from app.models.setting import Setting, SettingValueType
from app.models.site import Site
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
        return {"Authorization": f"Bearer {response.json()['access_token']}"}, user

    return _sign_in


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


def _make_client_row(session: Session) -> uuid.UUID:
    client = Client(name="Acme")
    session.add(client)
    session.commit()
    return client.id


def _create_site(sites_client, headers, client_id: uuid.UUID, *, number="S-1", **overrides) -> str:
    body = {
        "name": f"Site {number}",
        "site_number": number,
        "client_id": str(client_id),
        **overrides,
    }
    created = sites_client.post("/api/sites", json=body, headers=headers)
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _load_site(session: Session, site_id: str) -> Site:
    return session.get(Site, uuid.UUID(site_id))


def _seed_public_app_url(session: Session, value: str = "https://hours.example.com") -> None:
    """Seed the `public_app_url` setting the QR download reads.

    The in-memory schema is built from `Base.metadata` and carries no seed rows, so the row migration
    0008 seeds on a real database has to be created here for the download path to find a configured
    base URL (Requirement 1.x, 8.5).
    """
    session.add(Setting(key="public_app_url", value=value, value_type=SettingValueType.STRING))
    session.commit()


# --------------------------------------------------------------------------- download


def test_a_site_qr_downloads_as_png_by_default(sites_client, sign_in, session: Session):
    """Requirement 8.5: the QR downloads as a printable PNG."""
    headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    _seed_public_app_url(session)
    site_id = _create_site(sites_client, headers, client_id)

    response = sites_client.get(f"/api/sites/{site_id}/qr", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert "attachment" in response.headers["content-disposition"]


def test_a_site_qr_downloads_as_pdf(sites_client, sign_in, session: Session):
    """Requirement 8.5: the QR downloads as a printable PDF.

    The site name and number appear on the sheet as a rendered image label (so a Hebrew name renders),
    not as literal PDF-text bytes; that the label is drawn is asserted in `test_qr_render.py` on the
    composed image. Here the subject is the HTTP contract: a real PDF comes back.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    _seed_public_app_url(session)
    site_id = _create_site(sites_client, headers, client_id, number="S-77")

    response = sites_client.get(f"/api/sites/{site_id}/qr?format=pdf", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")


def test_a_separate_mode_site_needs_an_action(sites_client, sign_in, session: Session):
    """Requirement 8.3: a separate site has two codes, so the action must be named."""
    headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    _seed_public_app_url(session)
    site_id = _create_site(sites_client, headers, client_id, qr_mode="separate")

    missing = sites_client.get(f"/api/sites/{site_id}/qr", headers=headers)
    assert missing.status_code == 400
    assert _code(missing) == "qr_action_required"

    picked = sites_client.get(f"/api/sites/{site_id}/qr?action=check_in", headers=headers)
    assert picked.status_code == 200, picked.text
    assert "check_in" in picked.headers["content-disposition"]


def test_downloading_an_unknown_site_is_a_not_found(sites_client, sign_in):
    headers, _ = sign_in(UserRole.ADMIN)
    response = sites_client.get(f"/api/sites/{uuid.uuid4()}/qr", headers=headers)
    assert response.status_code == 404
    assert _code(response) == "site_not_found"


# --------------------------------------------------------------------------- regeneration


def test_creating_a_site_stores_a_verifiable_current_token(sites_client, sign_in, session: Session):
    """Requirement 8.4: the stored token is a signed one that verifies at the site's version."""
    headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    site_id = _create_site(sites_client, headers, client_id)

    site = _load_site(session, site_id)
    assert site.qr_token_version == 1
    parsed = qr_token.verify(site.qr_token, expected_version=1)
    assert parsed.site_id == site.id


def test_regeneration_bumps_the_version_and_replaces_the_token(sites_client, sign_in, session: Session):
    """Requirement 8.6: regenerating bumps the version and mints a new stored token."""
    headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    site_id = _create_site(sites_client, headers, client_id)
    before = _load_site(session, site_id).qr_token

    response = sites_client.post(f"/api/sites/{site_id}/qr/regenerate", headers=headers)
    assert response.status_code == 200, response.text

    session.expire_all()
    after = _load_site(session, site_id)
    assert after.qr_token_version == 2
    assert after.qr_token != before


def test_regeneration_invalidates_the_previous_token(sites_client, sign_in, session: Session):
    """Requirement 8.6, 8.7: the token printed before a regeneration no longer verifies afterwards.

    This is the scan-side revocation: the old token is well-formed and signed, but it names version 1
    while the site is now at version 2, so verifying it against the site's current version fails.
    """
    headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    site_id = _create_site(sites_client, headers, client_id)
    old_token = _load_site(session, site_id).qr_token

    sites_client.post(f"/api/sites/{site_id}/qr/regenerate", headers=headers)
    session.expire_all()
    current_version = _load_site(session, site_id).qr_token_version

    with pytest.raises(qr_token.InvalidToken):
        qr_token.verify(old_token, expected_version=current_version)


def test_a_site_manager_cannot_regenerate(sites_client, sign_in, session: Session):
    """Regeneration invalidates codes in the field, so it is administrator-only."""
    admin_headers, _ = sign_in(UserRole.ADMIN)
    client_id = _make_client_row(session)
    site_id = _create_site(sites_client, admin_headers, client_id, number="S-MGR")

    manager_headers, _ = sign_in(UserRole.SITE_MANAGER)
    response = sites_client.post(f"/api/sites/{site_id}/qr/regenerate", headers=manager_headers)
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"
