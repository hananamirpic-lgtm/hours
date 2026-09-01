"""Notification in-app list/read (Requirement 14.7) and email delivery with retry/backoff (14.7, 14.8, 21.6).

Two surfaces:

* **The in-app channel over HTTP.** `GET /api/notifications` returns the caller's own notifications
  with an unread count, and `POST /api/notifications/{id}/read` marks one read. Both are scoped to the
  caller: one user can neither see nor read another's notifications.

* **The email channel through the service.** `deliver` sends one notification, retrying a transient
  failure with backoff and — the heart of Requirement 14.8 — recording a final failure on the
  notification's `delivered_channels` **without ever removing the in-app row**. `render_email` produces
  the subject and body in the recipient's language (Requirement 21.6). The SMTP transport is a fake, so
  no test opens a socket, and `sleep` is injected so the backoff is driven without waiting.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator

import pyotp
import pytest
from sqlalchemy.orm import Session

from app.models.notification import Notification, NotificationSeverity
from app.models.user import AppLanguage, User, UserRole
from app.services import email as email_service
from app.services import notification as notification_service
from auth_support import DEFAULT_PASSWORD

_COUNTER = itertools.count(1)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def api_client(settings, session: Session) -> Iterator:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def sign_in(api_client, make_user):
    def _sign_in(role: UserRole, **overrides):
        payload = {"password": DEFAULT_PASSWORD}
        if role is UserRole.ADMIN:
            secret = pyotp.random_base32()
            user = make_user(role=role, is_2fa_enabled=True, totp_secret=secret, **overrides)
            payload["totp_code"] = pyotp.TOTP(secret).now()
        else:
            user = make_user(role=role, **overrides)
        payload["username"] = user.username
        response = api_client.post("/api/auth/login", json=payload)
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}, user

    return _sign_in


def _add_notification(
    session: Session,
    *,
    recipient: User,
    title_key: str = "notifications.no_checkout_reminder",
    body_params: dict | None = None,
    dedupe_key: str | None = None,
) -> Notification:
    notification, _ = notification_service.create_deduplicated(
        session,
        recipient_user_id=recipient.id,
        type="no_checkout_reminder",
        dedupe_key=dedupe_key or f"k-{next(_COUNTER)}",
        title_key=title_key,
        severity=NotificationSeverity.WARNING,
        body_params=body_params or {"site_name": "Site A", "check_in_local": "2025-08-04T08:00"},
    )
    session.commit()
    return notification


# ===================================================================== in-app list (14.7)


def test_list_returns_the_callers_notifications_with_unread_count(api_client, sign_in, session: Session):
    """Requirement 14.7: the list returns the caller's notifications, newest first, with an unread count."""
    headers, user = sign_in(UserRole.SITE_MANAGER)
    _add_notification(session, recipient=user)
    _add_notification(session, recipient=user)

    response = api_client.get("/api/notifications", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["unread"] == 2
    assert len(body["items"]) == 2
    # Language-neutral: a translation key and its parameters, not rendered text (Requirement 21.6).
    assert body["items"][0]["title_key"] == "notifications.no_checkout_reminder"
    assert "site_name" in body["items"][0]["body_params"]


def test_list_is_scoped_to_the_caller(api_client, sign_in, session: Session):
    """Requirement 14.7: a caller never sees another user's notifications."""
    mine_headers, me = sign_in(UserRole.SITE_MANAGER)
    _other_headers, other = sign_in(UserRole.SITE_MANAGER)
    _add_notification(session, recipient=me)
    _add_notification(session, recipient=other)
    _add_notification(session, recipient=other)

    body = api_client.get("/api/notifications", headers=mine_headers).json()
    assert body["total"] == 1


def test_unread_only_filters_the_list(api_client, sign_in, session: Session):
    """`unread_only` narrows the list, but the counts still cover the whole list."""
    headers, user = sign_in(UserRole.SITE_MANAGER)
    read_one = _add_notification(session, recipient=user)
    _add_notification(session, recipient=user)
    notification_service.mark_read(
        session, notification_id=read_one.id, recipient_user_id=user.id
    )
    session.commit()

    body = api_client.get("/api/notifications?unread_only=true", headers=headers).json()
    assert len(body["items"]) == 1
    assert body["total"] == 2
    assert body["unread"] == 1


# ===================================================================== read action (14.7)


def test_read_marks_the_notification_read(api_client, sign_in, session: Session):
    """Requirement 14.7: the read action marks one of the caller's notifications read."""
    headers, user = sign_in(UserRole.SITE_MANAGER)
    notification = _add_notification(session, recipient=user)

    response = api_client.post(f"/api/notifications/{notification.id}/read", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["is_read"] is True
    assert response.json()["read_at"] is not None


def test_cannot_read_another_users_notification(api_client, sign_in, session: Session):
    """Requirement 14.7: one user cannot mark another's notification read — it is a 404."""
    mine_headers, _me = sign_in(UserRole.SITE_MANAGER)
    _other_headers, other = sign_in(UserRole.SITE_MANAGER)
    theirs = _add_notification(session, recipient=other)

    response = api_client.post(f"/api/notifications/{theirs.id}/read", headers=mine_headers)
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "notification_not_found"


def test_read_is_idempotent(api_client, sign_in, session: Session):
    """Marking an already-read notification read again is a no-op that does not move read_at."""
    headers, user = sign_in(UserRole.SITE_MANAGER)
    notification = _add_notification(session, recipient=user)

    first = api_client.post(f"/api/notifications/{notification.id}/read", headers=headers).json()
    second = api_client.post(f"/api/notifications/{notification.id}/read", headers=headers).json()
    assert first["read_at"] == second["read_at"]


# ===================================================================== email rendering (21.6)


def test_render_email_uses_the_recipient_language():
    """Requirement 21.6: the same notification renders in Hebrew or English by recipient language."""
    params = {"site_name": "Site A", "check_in_local": "2025-08-04T08:00"}
    english = email_service.render_email(
        title_key="notifications.no_checkout_reminder",
        body_params=params,
        language=AppLanguage.ENGLISH,
    )
    hebrew = email_service.render_email(
        title_key="notifications.no_checkout_reminder",
        body_params=params,
        language=AppLanguage.HEBREW,
    )
    assert english.subject != hebrew.subject
    assert "Site A" in english.body
    assert "Site A" in hebrew.body


def test_render_email_falls_back_for_an_unknown_key():
    """A key with no template still renders legibly rather than failing to send."""
    rendered = email_service.render_email(
        title_key="notifications.unknown", body_params={"x": 1}, language=AppLanguage.ENGLISH
    )
    assert rendered.subject == "notifications.unknown"


# ===================================================================== email delivery (14.7, 14.8)


class _FailingTransport:
    """A transport that raises for the first `fail_times` sends, then succeeds."""

    def __init__(self, *, fail_times: int) -> None:
        self._fail_times = fail_times
        self.attempts = 0

    def send(self, *, to_address: str, subject: str, body: str) -> None:
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise ConnectionError("smtp refused")


def _emailable_user(session: Session) -> User:
    user = User(
        username=f"person{next(_COUNTER)}@hours.local",
        password_hash="x",
        role=UserRole.SITE_MANAGER,
        is_active=True,
        language=AppLanguage.ENGLISH,
    )
    session.add(user)
    session.commit()
    return user


def test_delivery_retries_then_succeeds(session: Session):
    """Requirement 14.7, 14.8: a transient failure is retried with backoff, then the email goes out."""
    user = _emailable_user(session)
    notification = _add_notification(session, recipient=user)
    transport = _FailingTransport(fail_times=1)
    waits: list[float] = []

    result = email_service.deliver(
        session, notification, transport=transport, sleep=waits.append
    )

    assert result.delivered is True
    assert transport.attempts == 2
    assert waits == [email_service.DEFAULT_BACKOFF_BASE_SECONDS]  # one backoff before the retry
    assert email_service.CHANNEL_EMAIL in notification.delivered_channels


def test_delivery_records_final_failure_without_losing_the_in_app_notification(session: Session):
    """Requirement 14.8: after every retry fails, the failure is recorded and the in-app row survives."""
    user = _emailable_user(session)
    notification = _add_notification(session, recipient=user)
    transport = _FailingTransport(fail_times=99)  # never succeeds

    result = email_service.deliver(
        session, notification, transport=transport, sleep=lambda _s: None
    )
    session.commit()

    assert result.delivered is False
    assert result.attempts == email_service.DEFAULT_MAX_ATTEMPTS
    # The failure is recorded on the channel list...
    assert email_service.CHANNEL_EMAIL_FAILED in notification.delivered_channels
    # ...and the in-app notification is still there (the record of truth).
    still_there = session.get(Notification, notification.id)
    assert still_there is not None
    assert email_service.CHANNEL_IN_APP in still_there.delivered_channels


def test_delivery_records_failure_when_recipient_has_no_address(session: Session):
    """A recipient with no address cannot be emailed; the failure is recorded and the row stands (14.8)."""
    # A username with no "@" is not an address.
    user = User(
        username="no-address-user",
        password_hash="x",
        role=UserRole.SITE_MANAGER,
        is_active=True,
    )
    session.add(user)
    session.commit()
    notification = _add_notification(session, recipient=user)
    transport = _FailingTransport(fail_times=0)

    result = email_service.deliver(
        session, notification, transport=transport, sleep=lambda _s: None
    )
    assert result.delivered is False
    assert transport.attempts == 0
    assert email_service.CHANNEL_EMAIL_FAILED in notification.delivered_channels
    assert session.get(Notification, notification.id) is not None


def test_deliver_unsent_skips_already_delivered(session: Session):
    """`deliver_unsent` sends only notifications not yet emailed, so a re-run does not re-send."""
    user = _emailable_user(session)
    _add_notification(session, recipient=user)
    transport = _FailingTransport(fail_times=0)

    first = email_service.deliver_unsent(session, transport=transport, sleep=lambda _s: None)
    second = email_service.deliver_unsent(session, transport=transport, sleep=lambda _s: None)

    assert first == 1
    assert second == 0  # already marked delivered; not re-sent
