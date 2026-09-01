"""User-service rules (Requirement 1, 2.1, 2.3, 20.8).

The service owns the rules the HTTP tests only observe the effect of: that deactivation bumps the
token version (which is *how* Requirement 20.8's immediacy is achieved), that it is idempotent, that a
password reset carries the same bump, and that a site scope is refused for a role that has none. These
run against the in-memory session, not over HTTP.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import verify_password
from app.models.user import User, UserRole
from app.models.user_site import UserSite
from app.schemas.user import UserCreate, UserUpdate
from app.services import user as user_service
from app.services.audit import AuditContext


def _context() -> AuditContext:
    return AuditContext(actor_user_id=uuid.uuid4(), request_id="test")


def _create(session: Session, **overrides) -> User:
    payload = UserCreate(
        username=overrides.pop("username", "manager"),
        password=overrides.pop("password", "correct-horse-battery"),
        role=overrides.pop("role", UserRole.SITE_MANAGER),
        **overrides,
    )
    user = user_service.create_user(session, payload, context=_context())
    session.commit()
    return user


# --------------------------------------------------------------------------- create


def test_create_hashes_the_password_and_never_stores_plaintext(session: Session):
    user = _create(session, password="a-real-password")
    assert user.password_hash != "a-real-password"
    assert verify_password("a-real-password", user.password_hash)


def test_create_with_sites_for_a_manager_writes_the_scope(session: Session):
    site_a, site_b = uuid.uuid4(), uuid.uuid4()
    user = _create(session, role=UserRole.SITE_MANAGER, site_ids=[site_a, site_b])
    assigned = set(user_service.assigned_site_ids(session, user.id))
    assert assigned == {site_a, site_b}


def test_create_with_sites_for_a_non_manager_is_refused(session: Session):
    with pytest.raises(user_service.SiteScopeNotApplicable):
        _create(session, role=UserRole.ACCOUNTING, site_ids=[uuid.uuid4()])


def test_a_duplicate_username_is_refused_and_names_the_holder(session: Session):
    first = _create(session, username="taken")
    with pytest.raises(user_service.DuplicateUsername) as excinfo:
        _create(session, username="taken")
    assert excinfo.value.conflicting.id == first.id


# --------------------------------------------------------------------------- deactivation (Requirement 20.8)


def test_deactivation_bumps_the_token_version(session: Session):
    """The mechanism behind Requirement 20.8: every issued token carries a version, and moving it
    forward is what makes `resolve_access_token` reject them all."""
    user = _create(session)
    before = user.token_version

    user_service.deactivate(session, user.id, context=_context())
    session.commit()

    assert user.is_active is False
    assert user.token_version == before + 1


def test_deactivation_is_idempotent(session: Session):
    """A second deactivation of an already-inactive user does nothing — it does not needlessly bump
    the version and invalidate a fresh admin session for the same account."""
    user = _create(session)
    user_service.deactivate(session, user.id, context=_context())
    session.commit()
    version_after_first = user.token_version

    user_service.deactivate(session, user.id, context=_context())
    session.commit()

    assert user.token_version == version_after_first


def test_reactivation_restores_active_without_touching_the_version(session: Session):
    user = _create(session)
    user_service.deactivate(session, user.id, context=_context())
    session.commit()
    version = user.token_version

    user_service.reactivate(session, user.id, context=_context())
    session.commit()

    assert user.is_active is True
    # No token exists to revive, so nothing is invalidated.
    assert user.token_version == version


# --------------------------------------------------------------------------- update


def test_a_password_reset_rehashes_and_bumps_the_version(session: Session):
    user = _create(session, password="old-password")
    before = user.token_version

    user_service.update_user(
        session, user.id, UserUpdate(password="new-password"), context=_context()
    )
    session.commit()

    assert verify_password("new-password", user.password_hash)
    assert not verify_password("old-password", user.password_hash)
    assert user.token_version == before + 1


def test_an_update_without_a_password_leaves_the_version_alone(session: Session):
    user = _create(session)
    before = user.token_version

    user_service.update_user(
        session, user.id, UserUpdate(language="en"), context=_context()
    )
    session.commit()

    assert user.language.value == "en"
    assert user.token_version == before


# --------------------------------------------------------------------------- site scope (Requirement 2.3)


def test_replace_user_sites_reconciles_the_scope(session: Session):
    user = _create(session, role=UserRole.SITE_MANAGER)
    site_a, site_b, site_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    user_service.replace_user_sites(session, user.id, [site_a, site_b], context=_context())
    session.commit()
    assert set(user_service.assigned_site_ids(session, user.id)) == {site_a, site_b}

    # Replacing the set removes the old rows rather than appending.
    user_service.replace_user_sites(session, user.id, [site_c], context=_context())
    session.commit()
    assert set(user_service.assigned_site_ids(session, user.id)) == {site_c}
    rows = session.scalars(select(UserSite).where(UserSite.user_id == user.id)).all()
    assert len(rows) == 1


def test_replace_user_sites_refused_for_a_non_manager(session: Session):
    user = _create(session, username="admin", role=UserRole.ADMIN)
    with pytest.raises(user_service.SiteScopeNotApplicable):
        user_service.replace_user_sites(session, user.id, [uuid.uuid4()], context=_context())
