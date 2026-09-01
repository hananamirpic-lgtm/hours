"""Notifications router (Requirement 14.7, 21.6).

HTTP only: resolve the caller, call the service, shape the response. The service owns the rules and
never commits, so the read action commits after marking a notification read.

Two endpoints, both scoped to the caller's own notifications:

* `GET /api/notifications` — the caller's in-app notifications, newest first, with an unread count
  for a badge. Optional `unread_only` narrows the list to what still needs attention.
* `POST /api/notifications/{id}/read` — mark one of the caller's own notifications read.

There is no role guard here beyond authentication: a notification belongs to one recipient, and the
recipient is *every* role — an employee gets the no-checkout reminder, a manager gets the missing-
report alerts, an administrator gets the document-expiry escalations. So the guard is simply "the
caller reads their own", enforced by scoping every query to `recipient_user_id = caller.id`. A
notification belonging to someone else does not resolve, and the read action on it is a 404, so one
user can neither see nor touch another's notifications.

The response is locale-neutral: a `title_key` and its `body_params`, never a rendered sentence, so
the front end draws the row in the reader's language (Requirement 21.6).
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession, EnrolledUser, api_error
from app.schemas.notification import (
    NotificationListResponse,
    NotificationResponse,
)
from app.services import notification as notification_service

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get(
    "",
    response_model=NotificationListResponse,
    summary="The caller's in-app notifications, newest first",
    description=(
        "The caller's own in-app notifications, newest first, with the count of unread ones for a "
        "badge (Requirement 14.7). Scoped to the caller: a notification belonging to another user is "
        "not returned. Each row carries a translation key and its parameters rather than rendered "
        "text, so the front end draws it in the reader's language (Requirement 21.6). `unread_only` "
        "narrows the list to notifications that have not been read."
    ),
)
def list_notifications(
    user: EnrolledUser,
    session: DbSession,
    unread_only: Annotated[bool, Query(description="Return only unread notifications.")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> NotificationListResponse:
    page = notification_service.list_for_recipient(
        session,
        recipient_user_id=user.id,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )
    return NotificationListResponse(
        items=[NotificationResponse.model_validate(row) for row in page.rows],
        total=page.total,
        unread=page.unread,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{notification_id}/read",
    response_model=NotificationResponse,
    summary="Mark one of the caller's notifications read",
    description=(
        "Mark one of the caller's own notifications read and return the updated row (Requirement "
        "14.7). Scoped to the caller: a notification that is not the caller's does not resolve and the "
        "action is a 404, so one user can never mark another's notification read. Idempotent — "
        "marking an already-read notification read again is a no-op."
    ),
    responses={404: {"description": "No such notification belongs to the caller"}},
)
def mark_notification_read(
    user: EnrolledUser,
    session: DbSession,
    notification_id: uuid.UUID,
) -> NotificationResponse:
    try:
        notification = notification_service.mark_read(
            session, notification_id=notification_id, recipient_user_id=user.id
        )
    except notification_service.NotificationNotFound as error:
        raise api_error(HTTPStatus.NOT_FOUND, error.code) from error
    session.commit()
    return NotificationResponse.model_validate(notification)
