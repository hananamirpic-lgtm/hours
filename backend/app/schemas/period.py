"""Approval-workflow and period-locking schemas (Requirement 15).

Locale-neutral like every other schema: no message text, only values the front end formats. The
approval workflow moves time entries along Draft → Review → Approved → Locked and freezes a calendar
month once accounting is satisfied, so payroll and billing cannot shift underneath them
(Requirement 15). This module carries the bodies and responses those endpoints exchange:

* `POST /api/time-entries/bulk-status` advances a set of entries to a target status, scoped to the
  caller's sites (Requirement 15.2, 15.3).
* `POST /api/periods/{year}/{month}/lock` locks a month, warning first if it holds entries that are
  not yet Approved (Requirement 15.4, 15.7).
* `POST /api/periods/{year}/{month}/unlock` reopens a locked month with a mandatory reason recorded
  in the audit log (Requirement 15.6).
* `GET /api/periods` lists the months that have been touched by the workflow and their current state.

A reason is mandatory and non-blank wherever the workflow records one — an unlock, and an
administrator's override of the status ladder (Requirement 15.6) — validated the same way a manual
entry's reason is.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.time_entry import TimeEntryStatus


def _non_blank(value: str) -> str:
    """A reason stripped of surrounding whitespace, rejected if it is blank (Requirement 15.6).

    An unlock and an administrator's reversal of the status ladder each require a reason the audit
    trail preserves. A string of spaces satisfies "present" while saying nothing, which is exactly
    the gap the requirement closes, so it is trimmed and refused here — the value the audit log
    stores is then the reason a reader will see, without padding.
    """
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("reason must not be blank")
    return trimmed


# --------------------------------------------------------------------------- bulk status change


class BulkStatusRequest(BaseModel):
    """The body of `POST /api/time-entries/bulk-status` (Requirement 15.2, 15.3).

    A set of entry ids and the status to move them to. The transition must be along the ladder
    Draft → Review → Approved → Locked, or, for an administrator only, a single step backwards; any
    other move is refused (Requirement 15.2). A site manager may move their sites' entries from Draft
    to Review and from Review to Approved (Requirement 15.3), and only for entries at sites in their
    scope (Requirement 2.3).

    `reason` is optional in the general case and is *required by the service* when the move is an
    administrator's reversal, because a step backwards is the one transition Requirement 15.6 wants
    recorded with a stated reason. `extra="forbid"` refuses an unknown field so a stray value is a
    validation error rather than a silent write.
    """

    model_config = ConfigDict(extra="forbid")

    entry_ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)
    target_status: TimeEntryStatus
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str | None) -> str | None:
        return _non_blank(value) if value is not None else None


class BulkStatusResponse(BaseModel):
    """What a bulk status change did: how many entries moved, and which.

    The front end refreshes the approval view from `updated_ids`, so it can update exactly the rows
    that changed without re-listing the whole page.
    """

    updated_count: int
    updated_ids: list[uuid.UUID]
    target_status: TimeEntryStatus


# --------------------------------------------------------------------------- period lock / unlock


class PeriodLockRequest(BaseModel):
    """The body of `POST /api/periods/{year}/{month}/lock` (Requirement 15.4, 15.7).

    Locking a month sets every Approved entry in it to Locked. When the month still holds entries that
    are not Approved, the lock is refused with a warning that lists them, unless `force` is set — the
    administrator has seen the warning and chooses to lock anyway, leaving the unapproved entries as
    they are (Requirement 15.7). `force` defaults to false so the first call always warns.
    """

    model_config = ConfigDict(extra="forbid")

    force: bool = False


class PeriodUnlockRequest(BaseModel):
    """The body of `POST /api/periods/{year}/{month}/unlock`: reopen a locked month (Requirement 15.6).

    An unlock requires a reason, recorded in the audit log, because reopening a frozen month is the
    action most in need of an explanation after the fact. The reason is mandatory and non-blank.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)

    _validate_reason = field_validator("reason")(_non_blank)


class UnapprovedEntry(BaseModel):
    """One entry standing in the way of a clean lock, for the warning list (Requirement 15.7).

    Enough for the administrator to find and fix it: which entry, which employee and site, the day it
    belongs to, and the status it is stuck at.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    site_id: uuid.UUID
    work_date: date
    status: TimeEntryStatus


class PeriodState(BaseModel):
    """One calendar month's lock state, for `GET /api/periods` and the lock/unlock responses.

    The state is derived from the timestamps, not a stored flag: a month is locked when it was locked
    and not since unlocked. The counts let the period screen show at a glance how much of a month is
    Approved and how much is still in flight.
    """

    model_config = ConfigDict(from_attributes=True)

    year: int
    month: int
    is_locked: bool
    locked_at: datetime | None = None
    locked_by_user_id: uuid.UUID | None = None
    unlocked_at: datetime | None = None
    unlocked_by_user_id: uuid.UUID | None = None
    unlock_reason: str | None = None


class PeriodListResponse(BaseModel):
    """The months the workflow has touched, most recent first."""

    items: list[PeriodState]


class PeriodLockResult(BaseModel):
    """The outcome of a lock attempt (Requirement 15.4, 15.7).

    When the lock went through, `locked` is true, `locked_count` is how many Approved entries were
    frozen, and `unapproved` lists any entries left behind because the lock was forced past them.
    When the month held unapproved entries and the call did not force, `locked` is false and
    `unapproved` is the warning list the administrator must act on or override.
    """

    locked: bool
    year: int
    month: int
    locked_count: int = 0
    unapproved: list[UnapprovedEntry] = Field(default_factory=list)
    state: PeriodState | None = None
