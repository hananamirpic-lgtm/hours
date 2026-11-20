"""Staff identity read of the service ticket system (T13; Requirements 2.5, 2.7, 20.3, 20.12, 16.4).

`resolve_staff` must give the same verdict as `auth_service.resolve_access_token` while reading less of
the `users` row, so the main test runs both over the whole matrix and compares outcomes. The remaining
tests pin what the narrower read promises: the role comes from the row, the object is detached, the
secret columns are never selected, and nothing is written.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import DetachedInstanceError

from app.core.security import create_access_token, create_refresh_token
from app.models import user as user_model
from app.models.user import User, UserRole
from app.service_tickets.services import staff_identity
from app.service_tickets.services.staff_identity import (
    StaffPrincipal,
    load_staff_identity,
    resolve_staff,
)
from app.services import auth as auth_service

#: Columns that must never appear in the SELECT list: the password hash and the two encrypted TOTP
#: secrets (the attributes are `totp_secret` and `totp_pending_secret`, the columns carry `_encrypted`).
SECRET_COLUMNS = ("password_hash", "totp_secret_encrypted", "totp_pending_secret_encrypted")


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def fresh(session: Session) -> Iterator[sessionmaker[Session]]:
    """A factory of request-style sessions on the test database, each with an empty identity map."""
    yield sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture
def statements(session: Session) -> Iterator[list[str]]:
    """Every SQL statement sent to the test database while the fixture is active."""
    engine: Engine = session.get_bind()  # type: ignore[assignment]
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _token(user: User, *, version: int | None = None, role: str | None = None) -> str:
    return create_access_token(
        user_id=user.id,
        token_version=user.token_version if version is None else version,
        role=(role or user.role.value),
    )


def _outcome(call) -> str:  # noqa: ANN001
    try:
        result = call()
    except auth_service.InvalidToken:
        return "InvalidToken"
    return f"ok:{result.id if isinstance(result, User) else result.user_id}"


# --------------------------------------------------------------------------- drift against Hours

LOCK_STATES = ("unlocked", "locked", "lock_expired")


@pytest.mark.parametrize(
    ("active", "version_matches", "lock", "role"),
    list(
        itertools.product(
            (True, False),
            (True, False),
            LOCK_STATES,
            (UserRole.ADMIN, UserRole.TECHNICIAN),
        )
    ),
)
def test_resolve_staff_agrees_with_resolve_access_token(
    session: Session, fresh, make_user, active: bool, version_matches: bool, lock: str, role: UserRole
) -> None:
    now = _now()
    overrides: dict[str, object] = {"is_active": active, "role": role, "token_version": 3}
    if lock != "unlocked":
        offset = timedelta(minutes=10) if lock == "locked" else -timedelta(minutes=1)
        overrides.update(failed_login_count=auth_service.MAX_FAILED_ATTEMPTS, locked_until=now + offset)
    user = make_user(**overrides)
    token = _token(user, version=3 if version_matches else 2)

    with fresh() as hours_session:
        expected = _outcome(lambda: auth_service.resolve_access_token(hours_session, token, now=now))
    with fresh() as staff_session:
        actual = _outcome(lambda: resolve_staff(staff_session, token, now))

    assert actual == expected
    # The matrix is only meaningful if both verdicts occur: success exactly when nothing is wrong.
    should_pass = active and version_matches and lock != "locked"
    assert actual == (f"ok:{user.id}" if should_pass else "InvalidToken")


def test_resolve_staff_agrees_on_tokens_that_are_not_usable(session: Session, fresh, make_user) -> None:
    user = make_user()
    refresh_token, _ = create_refresh_token(user_id=user.id, token_version=user.token_version)
    unknown_user = create_access_token(user_id=uuid.uuid4(), token_version=1)
    for token in ("", "not-a-jwt", refresh_token, unknown_user):
        with fresh() as hours_session:
            expected = _outcome(partial(auth_service.resolve_access_token, hours_session, token))
        with fresh() as staff_session:
            actual = _outcome(partial(resolve_staff, staff_session, token))
        assert expected == actual == "InvalidToken"


def test_resolve_staff_returns_the_principal_of_a_valid_token(session: Session, fresh, make_user) -> None:
    user = make_user(role=UserRole.TECHNICIAN, username="tech1", must_change_password=True)
    with fresh() as staff_session:
        principal = resolve_staff(staff_session, _token(user), _now())
    assert principal == StaffPrincipal(
        user_id=user.id,
        username="tech1",
        role=UserRole.TECHNICIAN,
        must_change_password=True,
        is_2fa_enrolment_required=False,
    )
    assert principal.display_name == "tech1"
    with pytest.raises(AttributeError):
        principal.username = "other"  # type: ignore[misc]


def test_a_deactivated_technician_gets_invalid_token(session: Session, fresh, make_user) -> None:
    technician = make_user(role=UserRole.TECHNICIAN, is_active=True)
    token = _token(technician)
    with fresh() as staff_session:
        assert resolve_staff(staff_session, token).user_id == technician.id

    technician.is_active = False
    technician.token_version += 1
    session.commit()
    with fresh() as staff_session, pytest.raises(auth_service.InvalidToken) as raised:
        resolve_staff(staff_session, token)
    assert raised.value.code == "invalid_token"

    # Even with the version bump undone, the deactivation alone refuses the token.
    technician.token_version -= 1
    session.commit()
    with fresh() as staff_session, pytest.raises(auth_service.InvalidToken):
        resolve_staff(staff_session, token)


# --------------------------------------------------------------------------- role comes from the row


def test_a_role_change_applies_on_the_next_call_with_the_old_token(
    session: Session, fresh, make_user
) -> None:
    user = make_user(role=UserRole.ADMIN)
    token = _token(user)  # carries role "admin" for its whole life
    with fresh() as staff_session:
        assert resolve_staff(staff_session, token).role is UserRole.ADMIN

    user.role = UserRole.TECHNICIAN
    session.commit()
    with fresh() as staff_session:
        assert resolve_staff(staff_session, token).role is UserRole.TECHNICIAN

    user.role = UserRole.OPERATIONS_ADMIN
    session.commit()
    with fresh() as staff_session:
        assert resolve_staff(staff_session, token).role is UserRole.OPERATIONS_ADMIN


def test_a_forged_role_claim_is_ignored(session: Session, fresh, make_user) -> None:
    user = make_user(role=UserRole.TECHNICIAN)
    token = _token(user, role=UserRole.ADMIN.value)
    with fresh() as staff_session:
        assert resolve_staff(staff_session, token).role is UserRole.TECHNICIAN


def test_2fa_enrolment_flag_follows_the_row(session: Session, fresh, make_user, monkeypatch) -> None:
    monkeypatch.setattr(user_model, "_ENFORCE_2FA", True)
    plain_admin = make_user(role=UserRole.ADMIN)
    enrolled_admin = make_user(role=UserRole.ADMIN, is_2fa_enabled=True)
    technician = make_user(role=UserRole.TECHNICIAN)
    flags = {}
    for name, user in (("plain", plain_admin), ("enrolled", enrolled_admin), ("tech", technician)):
        with fresh() as staff_session:
            flags[name] = resolve_staff(staff_session, _token(user)).is_2fa_enrolment_required
    assert flags == {"plain": True, "enrolled": False, "tech": False}


# --------------------------------------------------------------------------- the narrow read


def test_load_staff_identity_returns_none_for_an_unknown_user(fresh) -> None:
    with fresh() as staff_session:
        assert load_staff_identity(staff_session, uuid.uuid4()) is None


def test_the_loaded_user_is_detached_and_secret_columns_stay_unloaded(
    session: Session, fresh, make_user, statements: list[str]
) -> None:
    user = make_user(is_2fa_enabled=True, totp_secret="JBSWY3DPEHPK3PXP")
    statements.clear()

    with fresh() as staff_session:
        loaded = load_staff_identity(staff_session, user.id)
        assert loaded is not None
        assert loaded not in staff_session
        state = inspect(loaded)
        assert state.detached
        assert {"password_hash", "totp_secret", "totp_pending_secret"} <= set(state.unloaded)
        assert loaded.username == user.username  # a loaded column reads fine
        before = len(statements)
        for attribute in ("password_hash", "totp_secret", "totp_pending_secret"):
            with pytest.raises(DetachedInstanceError):
                getattr(loaded, attribute)
        assert len(statements) == before  # no lazy load was attempted


def test_one_select_of_one_row_that_names_no_secret_column(
    session: Session, fresh, make_user, statements: list[str]
) -> None:
    user = make_user(role=UserRole.TECHNICIAN)
    token = _token(user)
    statements.clear()

    with fresh() as staff_session:
        resolve_staff(staff_session, token, _now())

    assert len(statements) == 1
    select = statements[0]
    assert select.lstrip().upper().startswith("SELECT")
    assert "FROM users" in select
    selected = select.split("FROM", 1)[0]
    for column in SECRET_COLUMNS:
        assert column not in selected
    for column in (
        "id",
        "username",
        "role",
        "is_active",
        "token_version",
        "must_change_password",
        "is_2fa_enabled",
        "failed_login_count",
        "locked_until",
    ):
        assert f"users.{column}" in selected
    assert "WHERE users.id" in select  # by primary key


def test_resolving_writes_nothing_and_adds_no_audit_row(
    session: Session, fresh, make_user, statements: list[str]
) -> None:
    user = make_user()
    token = _token(user)
    statements.clear()

    with fresh() as staff_session:
        resolve_staff(staff_session, token)
        assert not staff_session.new
        assert not staff_session.dirty
        assert not staff_session.deleted
        staff_session.commit()

    writes = [s for s in statements if s.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}]
    assert writes == []
    assert all("change_logs" not in s for s in statements)


def test_the_module_does_not_import_the_audit_service() -> None:
    assert not hasattr(staff_identity, "record_change")
    assert not hasattr(staff_identity, "AuditContext")
