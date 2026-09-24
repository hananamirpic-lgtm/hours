"""Scan request and response schemas (Requirement 9, 10, 11 for the scan flow).

The whole attendance flow runs through one endpoint, `POST /api/scans`, which resolves a presented
QR token to a site, decides check-in versus check-out versus conflict from the employee's current
state, and writes the result server-side. The schemas here are deliberately spare.

**The request carries no time and no location.** The recorded time is the server's, never the
client's (Requirement 9.2), so there is no timestamp field to send. There is no coordinate field
anywhere, by requirement (9.6, 20.10), and `extra="forbid"` means a request carrying a `latitude` or
`longitude` — or any other unknown field — is rejected as a validation error rather than quietly
ignored. The only inputs are the token itself and an optional client nonce the duplicate-window check
reads (Requirement 9.3).

**The response is locale-neutral.** It states which site, which action was taken, and the server time
of the entry as an ISO 8601 timestamp; the front end formats and translates. A 409 conflict — an open
shift at a different site (Requirement 11.4) — does not come back through this model at all; it is the
error envelope, carrying the other site's name and the actions the employee may take.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class ScanRequest(BaseModel):
    """The body of `POST /api/scans`: a token and, optionally, a client nonce.

    No timestamp: the server time is authoritative (Requirement 9.2). No location field: none exists,
    and `extra="forbid"` turns one that is sent into a 422 rather than a silent drop (Requirement
    9.6). The `client_nonce` is an opaque string the client repeats when it retries a scan, so a
    double-tap or a network retry resolves to the same entry rather than a second one; it is advisory,
    and the duplicate-window check falls back to the token alone when it is absent.
    """

    model_config = ConfigDict(extra="forbid")

    qr_token: str = Field(min_length=1, max_length=2000)
    client_nonce: str | None = Field(default=None, max_length=200)


class SelfCheckInRequest(BaseModel):
    """The body of `POST /api/scans/self-check-in`: the id of the site to open a no-QR shift at.

    The employee has no QR to scan (the camera is broken, or no code is reachable), so they *pick* a
    site instead. Only the site id is accepted; like `ScanRequest` there is no timestamp — the server
    time is authoritative (Requirement 9.2) — and no location field, and `extra="forbid"` turns an
    unknown field into a 422 rather than a silent drop (Requirement 9.6). The site must be one the
    employee is assigned to, which the service enforces; the schema only shapes the input.
    """

    model_config = ConfigDict(extra="forbid")

    site_id: uuid.UUID


class ScanAction(enum.StrEnum):
    """What the server did with a scan, reported back so the client can confirm it.

    `CHECK_IN` and `CHECK_OUT` are the two outcomes that change state. `DUPLICATE_IGNORED` is a
    successful no-op: an identical scan arrived inside the duplicate window and the existing entry is
    returned unchanged (Requirement 9.3). A conflict — an open shift elsewhere — is not an action; it
    is a 409 error, so it is absent here.
    """

    CHECK_IN = "check_in"
    CHECK_OUT = "check_out"
    DUPLICATE_IGNORED = "duplicate_ignored"


class ScanResult(BaseModel):
    """The outcome of a scan: which entry, at which site, what happened, and the server time.

    `at` is the timestamp the action recorded — the check-in time for a check-in, the check-out time
    for a check-out — as a UTC ISO 8601 value the front end renders in local time (Requirement 23.5).
    `flags` surfaces any anomaly marker the entry carries, so the confirmation screen can note an
    unassigned-site check-in (Requirement 7.3) without a second request.
    """

    model_config = ConfigDict(from_attributes=True)

    time_entry_id: uuid.UUID
    site_id: uuid.UUID
    action: ScanAction
    #: The server-side timestamp the action recorded (Requirement 9.2). UTC.
    at: datetime
    #: Local date the entry is attributed to (Requirement 10.4). The check-in's local date.
    work_date: date
    #: Whether a shift is open after this scan: true after a check-in, false after a check-out.
    is_open: bool
    flags: list[str] = Field(default_factory=list)


class OpenShift(BaseModel):
    """The caller's current open shift, as `GET /api/scans/status` returns it.

    Everything the employee home screen needs to show "you are checked in at Site X since HH:MM" and
    render the primary action as check-out (Requirement 23.1, 23.2). `None` from the endpoint means no
    shift is open and the action is scan-to-check-in.
    """

    model_config = ConfigDict(from_attributes=True)

    time_entry_id: uuid.UUID
    site_id: uuid.UUID
    check_in_at: datetime
    work_date: date
    flags: list[str] = Field(default_factory=list)


class ScanStatusResponse(BaseModel):
    """The body of `GET /api/scans/status`: the open shift, or its absence.

    A wrapper rather than a bare nullable so the response is always an object and a client does not
    have to distinguish a `null` body from a missing one.
    """

    open_shift: OpenShift | None = None


class WorkHistoryDay(BaseModel):
    """One day of the caller's own recent work, as `GET /api/scans/history` returns each entry.

    Locale-neutral, like every other scan schema: `work_date` is the local calendar day as an ISO
    date and `total_minutes` is the whole minutes worked that day summed across every site
    (Requirement 11.2). The front end formats the date and renders the minutes as hours. Serialised
    straight from the service's `WorkHistoryDay`, so `from_attributes` reads its fields by name.
    """

    model_config = ConfigDict(from_attributes=True)

    work_date: date
    total_minutes: int


class WorkHistoryResponse(BaseModel):
    """The body of `GET /api/scans/history`: the caller's recent completed days, newest first.

    A wrapper around the list so the response is always an object, matching `ScanStatusResponse`. An
    empty `days` means the caller has no recent completed work — or is a login not linked to an
    employee — and the home screen shows "no recent work" (Requirement 23.3).
    """

    days: list[WorkHistoryDay] = Field(default_factory=list)


class AssignedSite(BaseModel):
    """One site the caller may open a no-QR shift at, as `GET /api/scans/my-sites` returns each.

    Deliberately spare: an id the self-check-in request sends back and a name to show in the picker.
    No billing rate, no client, no status — the employee is choosing where they are working, not
    administering the site. Serialised straight from the service's `AssignedSite`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class MySitesResponse(BaseModel):
    """The body of `GET /api/scans/my-sites`: the caller's own assigned sites for the picker.

    A wrapper around the list, matching the other scan responses. An empty `sites` means the caller
    is assigned to no active site — or is a login not linked to an employee — and the self-check-in
    picker shows that there is nowhere to check in without a QR.
    """

    sites: list[AssignedSite] = Field(default_factory=list)
