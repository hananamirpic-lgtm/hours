"""Notification schemas (Requirement 14.7, 21.6).

Locale-neutral like every other schema: a notification is a translation *key* plus its parameters,
never a rendered sentence, so the front end renders it in the reader's language when it draws the
list (Requirement 21.6). Freezing the text at write time would make the recipient's language a
property of when the job ran rather than of who is reading, which is exactly the mistake the
`title_key` + `body_params` shape avoids.

Two endpoints:

* `GET /api/notifications` → `NotificationListResponse` — the caller's own in-app notifications,
  newest first, with an unread count so a badge does not need a second request.
* `POST /api/notifications/{id}/read` → `NotificationResponse` — mark one of the caller's own
  notifications read, returning the updated row.

Nothing here carries the `dedupe_key`: it is an internal idempotency mechanism, not something a
client has any use for, so it stays out of the response.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.notification import NotificationSeverity


class NotificationResponse(BaseModel):
    """One notification, rendered into the reader's language by the front end (Requirement 21.6).

    `title_key` names the translation and `body_params` supplies its parameters — the employee, the
    site, the date, what is missing — so the same stored row reads in Hebrew or English depending on
    who opens it. `type` and `severity` let the front end pick an icon and a colour; `related_entity_*`
    lets it link straight to the completion form or the document (Requirement 14.4).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    severity: NotificationSeverity
    title_key: str
    body_params: dict
    related_entity_type: str | None = None
    related_entity_id: uuid.UUID | None = None
    is_read: bool
    read_at: datetime | None = None
    created_at: datetime


class NotificationListResponse(BaseModel):
    """The caller's notifications, newest first, with the unread count for a badge (Requirement 14.7).

    `unread` is the count of the caller's unread notifications across the whole list, not just the
    page, so a badge is correct even when the list is paged. `total` is the number of rows returned.
    """

    items: list[NotificationResponse] = Field(default_factory=list)
    total: int = 0
    unread: int = 0
    limit: int = 50
    offset: int = 0
