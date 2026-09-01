"""Audit writer.

Requirement 13.2 is the whole design: an audit record is written in the same transaction as the
change it describes, so a change cannot exist without its audit record. That rules out a background
task, a message queue and a second session — every one of those can succeed while the change fails,
or fail while the change succeeds. It leaves exactly one mechanism: add the rows to the session the
caller is already using and let the caller's commit decide the fate of both.

So nothing here commits and nothing here flushes. A service calls `record_model_changes` somewhere in
the middle of its own unit of work; if that unit of work rolls back, the audit rows go with it, which
is the behaviour the test suite pins down.

The other half of the job is the diff. Writing `record_change(...)` by hand once per field is how
audit trails end up with gaps: the field nobody thought to log is invariably the one in dispute. So
a service snapshots the entity, applies its changes, and hands both to `record_model_changes`,
which compares every mapped column and emits one row per field that actually moved.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.db.types import SENSITIVE_TYPES
from app.models.change_log import REDACTED, ChangeLog


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Who is making the change, why, and under which request.

    Passed down from the API layer rather than read from a global. A thread-local or context
    variable would make the actor invisible at the call site, and "who changed this" is the question
    the whole table exists to answer — it should be impossible to write a row without stating it.
    """

    #: `None` means the system acted on its own account: a scheduled job, or a shift closed by a
    #: site transition (Requirement 11.8). It is not a fallback for "we lost track of the user".
    actor_user_id: uuid.UUID | None = None
    request_id: str | None = None
    #: Default reason for every row written under this context. A per-call `reason` overrides it.
    reason: str | None = None


def record_change(
    session: Session,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    field: str,
    old_value: Any,
    new_value: Any,
    context: AuditContext,
    reason: str | None = None,
    sensitive: bool = False,
) -> ChangeLog:
    """Add one audit row to the caller's session and return it.

    No flush and no commit: the row's lifetime is the caller's transaction. `sensitive` replaces both
    values with a redaction marker, for a field whose plaintext must not land in this table.
    """
    entry = ChangeLog(
        entity_type=entity_type,
        entity_id=entity_id,
        changed_by_user_id=context.actor_user_id,
        field=field,
        old_value=REDACTED if sensitive and old_value is not None else format_value(old_value),
        new_value=REDACTED if sensitive and new_value is not None else format_value(new_value),
        reason=reason if reason is not None else context.reason,
        request_id=context.request_id,
    )
    session.add(entry)
    return entry


def record_model_changes(
    session: Session,
    instance: Any,
    before: Mapping[str, Any],
    *,
    context: AuditContext,
    reason: str | None = None,
    fields: Iterable[str] | None = None,
) -> list[ChangeLog]:
    """Diff a mapped instance against a snapshot and emit one row per changed field.

    Usage is always the same shape, and the snapshot has to be taken before the mutation because
    afterwards the old values are gone:

        before = snapshot(employee)
        employee.position = "Foreman"
        record_model_changes(session, employee, before, context=context)

    By default every mapped column is compared, less `IGNORED_FIELDS`, and that default is the point
    — a field added to the model later is audited without anyone remembering to add it here. Passing
    `fields` narrows the comparison to exactly those names, `IGNORED_FIELDS` included, for a service
    that means to audit only part of an update.
    """
    entity_type = entity_type_of(instance)
    entity_id = getattr(instance, "id", None)
    if entity_id is None:
        raise ValueError(f"cannot audit {entity_type}: the instance has no id yet, flush it first")

    after = snapshot(instance, fields=fields)
    sensitive_fields = sensitive_field_names(instance)
    written: list[ChangeLog] = []
    for field, new_value in after.items():
        if fields is None and field in IGNORED_FIELDS:
            continue
        old_value = before.get(field)
        if values_equal(old_value, new_value):
            continue
        written.append(
            record_change(
                session,
                entity_type=entity_type,
                entity_id=entity_id,
                field=field,
                old_value=old_value,
                new_value=new_value,
                context=context,
                reason=reason,
                sensitive=field in sensitive_fields,
            )
        )
    return written


# --------------------------------------------------------------------------- inspection helpers

#: Columns that change on every write without being a business change. Auditing `updated_at` would
#: double the size of the table and tell a reader nothing they cannot see from `changed_at`.
IGNORED_FIELDS = frozenset({"id", "created_at", "updated_at"})


def entity_type_of(instance: Any) -> str:
    """Table name of a mapped instance, which is what `change_logs.entity_type` holds."""
    return str(inspect(instance).mapper.local_table.name)


def snapshot(instance: Any, *, fields: Iterable[str] | None = None) -> dict[str, Any]:
    """Current values of an instance's mapped columns.

    Taken before a mutation and handed to `record_model_changes` afterwards. Only column attributes
    are read: relationships are other entities, and they carry their own audit rows.
    """
    mapper = inspect(instance).mapper
    names = [attribute.key for attribute in mapper.column_attrs] if fields is None else list(fields)
    return {name: getattr(instance, name) for name in names}


def sensitive_field_names(instance: Any) -> frozenset[str]:
    """Attributes whose column type marks the value as sensitive.

    Read from the column types rather than a hand-maintained list, so encrypting a new column
    automatically redacts it here. A list would drift, and the direction it drifts in is the one that
    leaks.
    """
    mapper = inspect(instance).mapper
    return frozenset(
        attribute.key
        for attribute in mapper.column_attrs
        if any(isinstance(column.type, SENSITIVE_TYPES) for column in attribute.columns)
    )


# --------------------------------------------------------------------------- value rendering


def values_equal(old_value: Any, new_value: Any) -> bool:
    """Whether a field actually moved.

    Equality first, then the rendered form as a second chance. The fallback is what stops a
    no-op from producing a row when a snapshot and a freshly loaded value differ only in type — a
    `date` against its ISO string, a `Decimal` against the integer it equals. An audit table full of
    changes that did not happen is worse than no audit table, because a reader stops trusting it.
    """
    if old_value is None or new_value is None:
        return old_value is None and new_value is None
    if old_value == new_value:
        return True
    return format_value(old_value) == format_value(new_value)


def format_value(value: Any) -> str | None:
    """Render a value for the audit column.

    The audit view renders sentences from these strings, so the rendering has to be unambiguous and
    stable across Python versions: ISO 8601 for dates and times, `true`/`false` rather than
    `True`/`False`, the plain digits of a `Decimal` rather than its `repr`, and an enum's value
    rather than `ClassName.MEMBER`.
    """
    match value:
        case None:
            return None
        case bool():
            return "true" if value else "false"
        case Enum():
            return format_value(value.value)
        case datetime() | date():
            return value.isoformat()
        case Decimal():
            # `str` on a Decimal can produce exponent notation for values built from a float; the
            # normalised form is what a reader expects to see.
            return format(value, "f")
        case uuid.UUID():
            return str(value)
        case str():
            return value
        case _:
            return str(value)
