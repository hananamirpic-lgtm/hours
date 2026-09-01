"""The settings endpoints over HTTP (the settings screen).

The settings surface lets an administrator read and tune the calculation engine's knobs. The claims
worth an HTTP test are the ones the feature makes:

* **An administrator reads the seeded settings.** `GET /api/settings` returns every knob with its
  stored value and declared type.
* **An administrator tunes the working-day length, and it persists with an audit row.** A PATCH to
  `overtime_daily_threshold_minutes` changes the stored value, and a `settings` audit row attributed
  to the acting administrator records the move (Requirement 13.2) — so a change to the working-day
  length is traceable.
* **Only an administrator may read or write.** A site manager, accounting or employee caller gets a
  403 on both endpoints (Requirement 2.2).
* **A bad value is refused and changes nothing.** An out-of-range or ill-typed value is rejected with
  the domain error, and the stored value is left as it was — a batch is all-or-nothing.
* **An unknown key is refused, not created.** The endpoint tunes the seeded knobs; it does not mint
  new ones.

The sign-in helper mirrors `test_audit_api.py`: an administrator must have completed 2FA enrolment or
every endpoint answers 403 (Requirement 1.6).
"""

from __future__ import annotations

from collections.abc import Iterator

import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.change_log import ChangeLog
from app.models.setting import Setting, SettingValueType
from app.models.user import UserRole
from auth_support import DEFAULT_PASSWORD


@pytest.fixture
def settings_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(settings_client, make_user):
    def _sign_in(role: UserRole, **user_overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **user_overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **user_overrides)
        payload["username"] = user.username

        response = settings_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        header = {"Authorization": f"Bearer {response.json()['access_token']}"}
        return header, user

    return _sign_in


def _code(response) -> str:
    return response.json()["detail"]["error"]["code"]


@pytest.fixture
def seed_settings_rows(session: Session):
    """Seed the two knobs the tests act on directly into the in-memory database.

    A small, explicit subset rather than the whole `SETTINGS_DEFAULTS`: the tests only touch the
    working-day length and a time knob, and seeding exactly those keeps each test's expectations
    obvious. The rows are committed so the endpoint reads them back through its own session.
    """
    rows = [
        Setting(
            key="overtime_daily_threshold_minutes",
            value="480",
            value_type=SettingValueType.INTEGER,
            description="Minutes worked in a day before further minutes are overtime (8h).",
        ),
        Setting(
            key="no_checkout_cutoff_time",
            value="23:59",
            value_type=SettingValueType.TIME,
            description="Local time the daily no-checkout reminder is sent.",
        ),
    ]
    session.add_all(rows)
    session.commit()
    return rows


def _value_of(response, key: str) -> str:
    return next(item["value"] for item in response.json()["items"] if item["key"] == key)


# --------------------------------------------------------------------------- read


def test_admin_reads_the_seeded_settings(settings_client, sign_in, seed_settings_rows):
    """An administrator sees every seeded knob with its stored value and declared type."""
    admin, _ = sign_in(UserRole.ADMIN)

    response = settings_client.get("/api/settings", headers=admin)
    assert response.status_code == 200, response.text
    items = {item["key"]: item for item in response.json()["items"]}
    assert "overtime_daily_threshold_minutes" in items
    threshold = items["overtime_daily_threshold_minutes"]
    assert threshold["value"] == "480"
    assert threshold["value_type"] == "integer"
    assert threshold["description"]


# --------------------------------------------------------------------------- write + audit


def test_admin_updates_the_working_day_length_and_it_persists_with_an_audit_row(
    settings_client, sign_in, seed_settings_rows, session: Session
):
    """The working-day length is tuned, stored, and traceable in the audit log (Requirement 13.2).

    A PATCH moves `overtime_daily_threshold_minutes` from 480 to 540; the read-back reflects it, and a
    `settings` audit row attributed to the acting administrator records the change field by field.
    """
    admin, admin_user = sign_in(UserRole.ADMIN)

    response = settings_client.patch(
        "/api/settings",
        json={"updates": {"overtime_daily_threshold_minutes": "540"}},
        headers=admin,
    )
    assert response.status_code == 200, response.text
    assert _value_of(response, "overtime_daily_threshold_minutes") == "540"

    # It persists.
    stored = session.scalars(
        select(Setting).where(Setting.key == "overtime_daily_threshold_minutes")
    ).one()
    assert stored.value == "540"
    assert stored.updated_by_user_id == admin_user.id

    # It is traceable: a settings audit row for the value, attributed to the administrator.
    audit_rows = session.scalars(
        select(ChangeLog).where(
            ChangeLog.entity_type == "settings", ChangeLog.entity_id == stored.id
        )
    ).all()
    value_changes = [row for row in audit_rows if row.field == "value"]
    assert len(value_changes) == 1
    change = value_changes[0]
    assert change.old_value == "480"
    assert change.new_value == "540"
    assert change.changed_by_user_id == admin_user.id
    assert change.reason == "setting_updated"


def test_an_unchanged_value_writes_no_audit_row(
    settings_client, sign_in, seed_settings_rows, session: Session
):
    """Re-saving the same value is a no-op: only a value that actually moved is audited."""
    admin, _ = sign_in(UserRole.ADMIN)

    response = settings_client.patch(
        "/api/settings",
        json={"updates": {"overtime_daily_threshold_minutes": "480"}},
        headers=admin,
    )
    assert response.status_code == 200, response.text

    stored = session.scalars(
        select(Setting).where(Setting.key == "overtime_daily_threshold_minutes")
    ).one()
    audit_rows = session.scalars(
        select(ChangeLog).where(ChangeLog.entity_type == "settings", ChangeLog.entity_id == stored.id)
    ).all()
    assert audit_rows == []


# --------------------------------------------------------------------------- authorization


@pytest.mark.parametrize("role", [UserRole.SITE_MANAGER, UserRole.ACCOUNTING])
def test_a_non_admin_cannot_read_settings(settings_client, sign_in, seed_settings_rows, role):
    """Tuning the engine is administrator-only: other console roles are refused the read."""
    headers, _ = sign_in(role)
    response = settings_client.get("/api/settings", headers=headers)
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


def test_an_employee_cannot_read_settings(settings_client, sign_in, seed_settings_rows, make_user):
    """The employee role has no settings access."""
    headers, _ = sign_in(UserRole.EMPLOYEE)
    response = settings_client.get("/api/settings", headers=headers)
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"


@pytest.mark.parametrize("role", [UserRole.SITE_MANAGER, UserRole.ACCOUNTING, UserRole.EMPLOYEE])
def test_a_non_admin_cannot_update_settings(
    settings_client, sign_in, seed_settings_rows, session: Session, role
):
    """A non-administrator is refused the write, and the stored value is untouched."""
    headers, _ = sign_in(role)
    response = settings_client.patch(
        "/api/settings",
        json={"updates": {"overtime_daily_threshold_minutes": "540"}},
        headers=headers,
    )
    assert response.status_code == 403
    assert _code(response) == "insufficient_role"

    stored = session.scalars(
        select(Setting).where(Setting.key == "overtime_daily_threshold_minutes")
    ).one()
    assert stored.value == "480"


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize("bad_value", ["0", "99999", "abc", "8.5"])
def test_an_invalid_threshold_is_rejected_and_changes_nothing(
    settings_client, sign_in, seed_settings_rows, session: Session, bad_value
):
    """A working-day length of 0, 99999, or a non-integer is refused; the stored value is unchanged.

    0 is below the one-minute floor, 99999 is beyond the 24-hour ceiling, `"abc"` and `"8.5"` are not
    whole integers — all four are the same event to the caller: the value will not be stored.
    """
    admin, _ = sign_in(UserRole.ADMIN)

    response = settings_client.patch(
        "/api/settings",
        json={"updates": {"overtime_daily_threshold_minutes": bad_value}},
        headers=admin,
    )
    assert response.status_code == 422
    assert _code(response) == "invalid_setting_value"
    assert response.json()["detail"]["error"]["params"]["key"] == "overtime_daily_threshold_minutes"

    stored = session.scalars(
        select(Setting).where(Setting.key == "overtime_daily_threshold_minutes")
    ).one()
    assert stored.value == "480"


def test_an_invalid_time_is_rejected(settings_client, sign_in, seed_settings_rows, session: Session):
    """A time knob given a value that is not `HH:MM` is refused, and the stored value is unchanged."""
    admin, _ = sign_in(UserRole.ADMIN)

    response = settings_client.patch(
        "/api/settings",
        json={"updates": {"no_checkout_cutoff_time": "25:00"}},
        headers=admin,
    )
    assert response.status_code == 422
    assert _code(response) == "invalid_setting_value"

    stored = session.scalars(
        select(Setting).where(Setting.key == "no_checkout_cutoff_time")
    ).one()
    assert stored.value == "23:59"


def test_a_batch_with_one_bad_value_stores_none_of_it(
    settings_client, sign_in, seed_settings_rows, session: Session
):
    """A batch is all-or-nothing: a good change alongside a bad one leaves both unchanged."""
    admin, _ = sign_in(UserRole.ADMIN)

    response = settings_client.patch(
        "/api/settings",
        json={
            "updates": {
                "overtime_daily_threshold_minutes": "540",  # valid
                "no_checkout_cutoff_time": "nope",  # invalid
            }
        },
        headers=admin,
    )
    assert response.status_code == 422

    threshold = session.scalars(
        select(Setting).where(Setting.key == "overtime_daily_threshold_minutes")
    ).one()
    assert threshold.value == "480", "the valid change must not have been applied"


def test_an_unknown_key_is_rejected_not_created(
    settings_client, sign_in, seed_settings_rows, session: Session
):
    """A key that is not already a setting is refused; the endpoint tunes, it does not create."""
    admin, _ = sign_in(UserRole.ADMIN)

    response = settings_client.patch(
        "/api/settings",
        json={"updates": {"made_up_key": "1"}},
        headers=admin,
    )
    assert response.status_code == 400
    assert _code(response) == "setting_not_found"

    created = session.scalars(select(Setting).where(Setting.key == "made_up_key")).one_or_none()
    assert created is None
