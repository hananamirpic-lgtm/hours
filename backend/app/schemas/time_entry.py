"""Time-entry read schemas for the manager hours view (Requirement 2.3, 2.4, 18.1, 22.3).

The hours view is a read of recorded shifts: a manager or accounting lists the entries at the sites
they may see, filtered by a date range, an employee, a site, a status and any anomaly flag, and the
front end lays them out as a chronological per-employee day across sites with per-site and daily
totals (Requirement 18.1). This module carries only what that read needs — the write schemas for
manual entry and correction are Task 21's, and live beside these when they land.

Locale-neutral like every other schema: no message text, only values the front end formats. Every
timestamp is a UTC ISO 8601 value the client renders in local time, `work_date` is the local date the
entry is attributed to (a shift crossing midnight keeps the date of its check-in), and `total_minutes`
is whole minutes — the front end converts to hours once, at presentation, so repeated rounding never
drifts (design "Money"/"Time").

**No wage or billing field appears here.** A time entry records *when* someone worked, not what they
are paid or what the client is billed; those live on the employee and site rate histories, which are
redacted for a site manager elsewhere. So the hours view needs no redaction — but it does need
scoping: a site manager sees only the entries at their assigned sites (Requirement 2.3), applied in
the query's `WHERE` clause, not after the fetch. The employee and site *names* are carried so the
view can label each row without a second request; a name is not a money field.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.time_entry import TimeEntrySource, TimeEntryStatus


def _non_blank(value: str) -> str:
    """A reason stripped of surrounding whitespace, rejected if it is blank (Requirement 12.3).

    A reason is mandatory on every manual creation, edit and deletion, and a string of spaces is not
    a reason — it satisfies "present" while saying nothing, which is exactly the gap the requirement
    closes. Trimming here means the value the audit trail stores is the reason a reader will see,
    without leading or trailing padding, and the database `manual_requires_reason` /
    `delete_requires_reason` checks (which `btrim` the same way) never fire on a value this validator
    let through.
    """
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("reason must not be blank")
    return trimmed


class TimeEntryListItem(BaseModel):
    """One recorded shift as the hours view reads it.

    Everything the view needs to render a row and group it: which employee (id plus both name forms,
    since the console may show either — Requirement 21.5), which site (id and name), the local work
    date it is attributed to, the check-in and check-out instants and the whole minutes worked
    (`None` while the shift is still open), how the entry came to exist, whether it was ever touched by
    hand, its approval status, and any anomaly flag it carries.

    The `is_manual` flag and the `flags` array are the two markers the view makes visible: a manual
    entry is badged wherever it appears (Requirement 12.4), and an anomalous one — an unassigned-site
    check-in or an implausible duration — is surfaced for the manager to notice rather than buried
    (Requirement 10.5, 7.3).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID

    employee_id: uuid.UUID
    #: The employee's local-language and English name forms, so the row can be labelled in either
    #: (Requirement 21.5). Names are not money fields, so they are served to every hours reader.
    employee_name: str
    employee_name_en: str

    site_id: uuid.UUID
    site_name: str

    #: Local date of check-in; the day the entry belongs to and the view groups on (Requirement 10.4).
    work_date: date
    check_in_at: datetime
    check_out_at: datetime | None
    #: Whole minutes worked, `None` while the shift is open. The view sums these into per-site and
    #: daily totals and converts to hours once, at presentation.
    total_minutes: int | None

    source: TimeEntrySource
    #: True if the entry was ever created or edited by hand; the view badges it (Requirement 12.4).
    is_manual: bool
    status: TimeEntryStatus
    #: Anomaly markers: `unassigned_site`, `implausible_duration`. The view surfaces them visibly.
    flags: list[str] = Field(default_factory=list)


class TimeEntryListResponse(BaseModel):
    """A page of time entries with the total, so the view can render pagination controls.

    Ordered by the service so the front end can lay the page out as a chronological per-employee day
    without re-sorting: by employee, then work date, then check-in time. The client groups the flat
    list into per-employee days and per-site sub-totals from that order.
    """

    items: list[TimeEntryListItem]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- write schemas
# Manual entry and correction (Requirement 12). Managers and administrators create, edit and delete
# time entries when a scan failed; every operation carries a mandatory non-blank reason (Requirement
# 12.3), and the service subjects the result to the same overlap, plausibility and period-lock rules
# a scanned entry obeys (Requirement 12.5). These are locale-neutral like the read schemas: the
# reason is free text the actor typed, not a translation key, because it is a human explanation the
# audit trail preserves verbatim.


class TimeEntryCreate(BaseModel):
    """The body of `POST /api/time-entries`: a manually recorded shift (Requirement 12.1).

    A manager or administrator states the employee, the site, and the check-in and check-out
    instants; the entry is marked manual and its source recorded as `manual` (Requirement 12.4). Both
    times are required — a manual entry records a shift that already happened, so it is complete, not
    an open shift someone will scan out of later. `check_out_at` must be after `check_in_at`, matching
    the database check and the scanned check-out rule (Requirement 10.3). `work_date` is optional: when
    omitted the service attributes the entry to the local date of its check-in, exactly as a scan does
    (Requirement 10.4), so a caller normally leaves it unset and only supplies it to override an
    edge case. The `reason` is mandatory and non-blank (Requirement 12.3).

    `extra="forbid"` refuses an unknown field, so a stray `latitude` or a client-supplied `is_manual`
    is a validation error rather than a silent write — the source and manual marking are the server's
    to set, never the client's.
    """

    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    site_id: uuid.UUID
    check_in_at: datetime
    check_out_at: datetime
    work_date: date | None = None
    reason: str = Field(min_length=1, max_length=1000)
    #: Set by an administrator to write into a *locked* month (Requirement 15.5). Ignored when the
    #: month is open; when the month is locked, only an administrator may set it and the write and the
    #: override are both recorded in the audit log under `reason` (Requirement 15.6). Any other role
    #: setting it is refused by the router before the write is attempted. Defaults to false so a
    #: normal write is never an accidental override.
    override: bool = False

    _validate_reason = field_validator("reason")(_non_blank)


class TimeEntryUpdate(BaseModel):
    """The body of `PATCH /api/time-entries/{id}`: a correction to an entry's times (Requirement 12.2).

    A manager or administrator edits the check-in or the check-out time of an existing entry; either
    may be sent alone or both together, and an omitted field is left unchanged. Editing an entry marks
    it manual wherever it appears (Requirement 12.4) even if it was originally scanned, because a hand
    correction is exactly what the marking exists to disclose. The `reason` is mandatory and non-blank
    on every edit (Requirement 12.3).

    An omitted time field is left unchanged; only the fields present in the body are applied, and the
    service recomputes `total_minutes` from whatever the times become. The rule that at least one time
    field is present lives in the service, since it is a cross-field constraint the endpoint reports
    as a domain refusal rather than a per-field validation error.
    """

    model_config = ConfigDict(extra="forbid")

    check_in_at: datetime | None = None
    check_out_at: datetime | None = None
    reason: str = Field(min_length=1, max_length=1000)
    #: An administrator's override to correct an entry in a locked month (Requirement 15.5). See
    #: `TimeEntryCreate.override`.
    override: bool = False

    _validate_reason = field_validator("reason")(_non_blank)


class TimeEntryDelete(BaseModel):
    """The body of `DELETE /api/time-entries/{id}`: a soft delete with a mandatory reason (Req 12.7).

    A deletion is never a hard delete — the row is retained for audit — so it carries a reason the
    same way a creation and an edit do, and the service sets `deleted_at` and `delete_reason` rather
    than removing the row. Sent as a body on a DELETE so the reason travels with the request; the
    `reason` is mandatory and non-blank (Requirement 12.3, 12.7).
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)
    #: An administrator's override to delete an entry in a locked month (Requirement 15.5). See
    #: `TimeEntryCreate.override`.
    override: bool = False

    _validate_reason = field_validator("reason")(_non_blank)


class TimeEntryResponse(BaseModel):
    """One entry as a write endpoint returns it, so the console can render the row it just changed.

    The same shape a list row carries minus the joined labels: which employee and site (by id), the
    times and total, how the entry came to exist, whether it was touched by hand, its status, its
    flags, and — where set — the soft-delete marker. A write endpoint returns the entry it created,
    edited or deleted so the front end can update the row in place without re-listing.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    site_id: uuid.UUID
    work_date: date
    check_in_at: datetime
    check_out_at: datetime | None
    total_minutes: int | None
    source: TimeEntrySource
    is_manual: bool
    manual_reason: str | None
    status: TimeEntryStatus
    flags: list[str] = Field(default_factory=list)
    deleted_at: datetime | None = None
