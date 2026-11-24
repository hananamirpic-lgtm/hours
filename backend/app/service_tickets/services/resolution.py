"""The resolution of a ticket: service history and replaced parts (Requirement 29.1 to 29.3, 29.7).

A staff status change to `closed` (displayed as Resolved) carries a resolution:

    entry_text        the History_Entry text, 1 to 2000 characters after trimming
    technician_name   the name on the entry, 1 to 100 characters after trimming
    parts_replaced    a real boolean (never `"true"`, `1` or a missing value)
    part_ids          the replaced parts; 1 to 20 distinct active parts when `parts_replaced` is true,
                      empty when it is false

`lifecycle.change_status` is the only caller and stays the single place that changes a status. It calls
`validate` after its own checks (ticket, expected status, transition, in that order) and before the guarded
`UPDATE`, and `store` after that `UPDATE` matched a row, so a concurrent status change
(`ticket_status_changed`) never leaves a history entry or a part behind. Neither function commits; the
router owns the transaction, so a failure anywhere rolls back the status, the entry and the parts together.

Validation reports every failing field at once, in form order (R29.1), as `ResolutionValidationError.fields`:

    entry_text        `invalid_history_text`        (the history validators and codes, R16.3)
    technician_name   `invalid_technician_name`
    parts_replaced    `invalid_request`             not a boolean (the package's generic malformed-input
                                                    code, as for the strict booleans of the settings body);
                                                    the parts are not looked at then
    part_ids          `part_required`               parts_replaced true and no id
                      `invalid_part`                an id that is malformed, unknown, inactive or repeated,
                                                    more than 20 ids, or any id when parts_replaced is false

The field keys are the bare names above, not `resolution.<name>`; the envelope of the response is the
usual `{"detail": {"error": {"code": <first failing field's code>, "fields": {...}}}}`.

`store` replaces the rows of `svc_ticket_parts` for the ticket (delete, then insert). A resolution with
`parts_replaced` false therefore removes the parts an earlier resolution recorded (R29.7); a reopen never
calls this module, so reopening keeps them. The parts are validated with one query, which takes a shared
row lock on the matching parts (`FOR SHARE` on PostgreSQL) so that a part cannot be deactivated or deleted
by a concurrent request between the check and the commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.service_tickets.error_codes import (
    CODE_INVALID_PART,
    CODE_PART_REQUIRED,
    GENERIC_INVALID_REQUEST,
)
from app.service_tickets.grammars import parse_uuid_path
from app.service_tickets.models import Part, Ticket, TicketPart
from app.service_tickets.services import history
from app.service_tickets.services.history import HistoryValidationError

#: Most parts one resolution may record (R29.3).
PART_IDS_MAX = 20

__all__ = [
    "PART_IDS_MAX",
    "Resolution",
    "ResolutionValidationError",
    "ValidResolution",
    "store",
    "validate",
]


@dataclass(frozen=True, slots=True)
class Resolution:
    """The resolution as the caller supplied it. Every value is unchecked on purpose (`object`), so that
    a bad value reaches `validate` and gets its code together with the other bad fields."""

    entry_text: object = None
    technician_name: object = None
    parts_replaced: object = None
    part_ids: object = None


@dataclass(frozen=True, slots=True)
class ValidResolution:
    """A checked resolution: the trimmed text and name, and the distinct active part ids (empty for none)."""

    entry_text: str
    technician_name: str
    part_ids: tuple[uuid.UUID, ...]


class ResolutionValidationError(HistoryValidationError):
    """One or more fields of the resolution are invalid (HTTP 422).

    Same shape as `HistoryValidationError`, so the router maps it the same way: `fields` maps each failing
    field to its code in form order and `code` is the code of the first one.
    """


def _check_parts(
    session: Session, parts_replaced: object, raw_ids: object
) -> tuple[tuple[uuid.UUID, ...], dict[str, str]]:
    """The checked part ids and the failing field, if any (at most one entry)."""
    if not isinstance(parts_replaced, bool):
        return (), {"parts_replaced": GENERIC_INVALID_REQUEST}
    ids = [] if raw_ids is None else raw_ids
    if not isinstance(ids, list | tuple):
        return (), {"part_ids": CODE_INVALID_PART}
    if not parts_replaced:
        return (), ({"part_ids": CODE_INVALID_PART} if ids else {})
    if not ids:
        return (), {"part_ids": CODE_PART_REQUIRED}
    if len(ids) > PART_IDS_MAX:
        return (), {"part_ids": CODE_INVALID_PART}

    parsed: list[uuid.UUID] = []
    for raw in ids:
        if isinstance(raw, uuid.UUID):
            part_id: uuid.UUID | None = raw
        else:
            part_id = parse_uuid_path(raw) if isinstance(raw, str) else None
        if part_id is None:
            return (), {"part_ids": CODE_INVALID_PART}
        parsed.append(part_id)
    if len(set(parsed)) != len(parsed):
        return (), {"part_ids": CODE_INVALID_PART}

    active = session.execute(
        select(Part.id).where(Part.id.in_(parsed), Part.is_active.is_(True)).with_for_update(read=True)
    ).scalars()
    if len(set(active)) != len(parsed):  # an unknown or an inactive part
        return (), {"part_ids": CODE_INVALID_PART}
    return tuple(parsed), {}


def validate(session: Session, resolution: Resolution) -> ValidResolution:
    """The checked resolution, or `ResolutionValidationError` naming every failing field. Writes nothing."""
    fields: dict[str, str] = {}
    text = name = ""
    try:
        text, name = history.validate_entry(resolution.entry_text, resolution.technician_name)
    except HistoryValidationError as error:
        fields.update(error.fields)  # entry_text first, then technician_name
    part_ids, part_fields = _check_parts(session, resolution.parts_replaced, resolution.part_ids)
    fields.update(part_fields)
    if fields:
        raise ResolutionValidationError(fields)
    return ValidResolution(entry_text=text, technician_name=name, part_ids=part_ids)


def store(session: Session, ticket: Ticket, valid: ValidResolution, *, now: datetime | None = None) -> None:
    """Add the history entry and replace the ticket's parts with `valid.part_ids`. Never commits.

    The entry goes through the history service. The parts are replaced by one delete and, when there are
    any, one insert; the ticket's earlier parts are gone whatever the new list is (R29.7).
    """
    history.add_entry(session, ticket.ticket_number, valid.entry_text, valid.technician_name, now=now)
    session.execute(
        delete(TicketPart)
        .where(TicketPart.ticket_id == ticket.id)
        .execution_options(synchronize_session=False)
    )
    if valid.part_ids:
        session.execute(
            insert(TicketPart), [{"ticket_id": ticket.id, "part_id": part_id} for part_id in valid.part_ids]
        )
