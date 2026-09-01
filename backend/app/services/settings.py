"""Typed access to the `settings` table.

Every tuning knob the engine and the jobs read lives in one table as text plus a declared type
(see migration 0001 and `app.seed.SETTINGS_DEFAULTS`). One row shape serves every setting, so the
conversion from text back to a real value has to happen somewhere — and it happens here, once, rather
than at each call site guessing at `int(row.value)`. A caller asks for a setting by key and the type
it expects; a value stored under a different type, or a missing row, is a programming error the
caller is told about rather than a silent wrong answer.

The read side never writes. The write side is here too now — `update_settings`, used by the settings
screen — but it is deliberately kept apart from the accessors above: the accessors convert text to a
value and the update path validates a value into text, and mixing the two would let a read grow a
side effect. The document sweep and the calculation engine still share only the read side, so they
cannot disagree on what `document_expiry_warning_days` means.

A change made here alters *future* calculations only. The engine reads a setting at calculation time,
so nothing already computed is rewritten by an edit — there is no recalculation triggered here, by
design. A new working-day length changes the next calculation, not last month's payroll.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import time
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.setting import Setting, SettingValueType
from app.services.audit import AuditContext, record_model_changes, snapshot


class SettingNotFound(Exception):
    """No settings row for the requested key.

    A real error, not a `None`: every key this module is asked for is seeded on a fresh database, so
    a miss means the seed did not run or the key was mistyped — both of which a caller wants to hear
    about loudly rather than paper over with a default that then drifts from the seeded one.

    On the write path this also guards against an endpoint creating keys: `update_settings` refuses a
    key that is not already a row, so the settings surface can only tune the seeded knobs, never mint
    a new one nothing reads.
    """

    code = "setting_not_found"

    def __init__(self, key: str) -> None:
        super().__init__(f"no setting {key!r}")
        self.key = key


class InvalidSettingValue(Exception):
    """A submitted value does not fit its setting's declared type or range.

    Carries the offending `key` so the router can name it in the error envelope and the settings
    screen can mark the right field. Raised for both a shape failure (an integer knob given `"abc"`,
    a time knob given `"25:00"`) and a semantic one (the working-day length set to 0 or beyond 24
    hours), because to the caller they are the same event: the value they sent will not be stored.
    """

    code = "invalid_setting_value"

    def __init__(self, key: str) -> None:
        super().__init__(f"invalid value for setting {key!r}")
        self.key = key


def _load(session: Session, key: str) -> Setting:
    setting = session.scalars(select(Setting).where(Setting.key == key)).one_or_none()
    if setting is None:
        raise SettingNotFound(key)
    return setting


def get_int(session: Session, key: str) -> int:
    """The integer value stored under `key` (for example `document_expiry_warning_days`)."""
    return int(_load(session, key).value)


def get_int_or(session: Session, key: str, default: int) -> int:
    """As `get_int`, but returns `default` when the key is absent.

    Used by callers that must keep running on a database seeded before a key existed — a scheduled
    job should not stop sweeping because one knob has not been added yet.
    """
    try:
        return get_int(session, key)
    except SettingNotFound:
        return default


def get_time(session: Session, key: str) -> time:
    """The `HH:MM` value stored under `key`, as a `time`."""
    return time.fromisoformat(_load(session, key).value)


def get_str(session: Session, key: str) -> str:
    return _load(session, key).value


# --------------------------------------------------------------------------- write side (settings screen)


def list_settings(session: Session) -> list[Setting]:
    """Every settings row, ordered by key, for the settings screen to render.

    Ordered by `key` so the screen presents a stable list that does not reshuffle between loads. This
    is the whole table — the settings surface shows and tunes exactly the seeded knobs.
    """
    return list(session.scalars(select(Setting).order_by(Setting.key)))


#: Semantic bounds enforced on the well-known knobs, over and above the type check. A key absent here
#: is validated only for its type; a key present must also fall inside `(minimum, maximum)` inclusive.
#: The working-day length is bounded to a real day — at least one minute, at most twenty-four hours —
#: so a threshold of 0 (every minute overtime) or 99999 (never overtime) is refused rather than stored
#: and silently distorting every future calculation. `None` on a bound means that side is open.
_INTEGER_BOUNDS: dict[str, tuple[int | None, int | None]] = {
    "overtime_daily_threshold_minutes": (1, 1440),
    "implausible_shift_hours": (1, None),
    "max_open_shift_hours": (1, None),
    "duplicate_scan_window_seconds": (0, None),
    "document_expiry_warning_days": (0, None),
    "shabbat_start_weekday": (0, 6),
    "shabbat_end_weekday": (0, 6),
}


def _validate_value(key: str, value_type: SettingValueType, value: str) -> str:
    """Check `value` fits `value_type` (and any semantic bound for `key`), returning the text to store.

    The text is returned rather than the parsed value because the column stores text and the read side
    parses it back — storing the parsed form here would put the conversion in two places that could
    drift. What this guarantees is that whatever text is stored will parse cleanly on the next read,
    so a `get_int` or `get_time` downstream cannot choke on a value the screen let through.

    A failure of either kind — the wrong shape for the type, or a value outside the knob's real range —
    raises `InvalidSettingValue(key)`. The caller (the router) maps that to the error envelope.
    """
    match value_type:
        case SettingValueType.INTEGER:
            parsed = _parse_int(key, value)
            _check_integer_bounds(key, parsed)
        case SettingValueType.DECIMAL:
            _parse_decimal(key, value)
        case SettingValueType.BOOLEAN:
            if value not in ("true", "false"):
                raise InvalidSettingValue(key)
        case SettingValueType.TIME:
            _parse_time(key, value)
        case SettingValueType.JSON:
            _parse_json(key, value)
        case SettingValueType.STRING:
            pass  # Any text is a valid string.
    return value


def _parse_int(key: str, value: str) -> int:
    try:
        # `int(value)` accepts surrounding whitespace and a sign but not a decimal point or letters,
        # which is exactly the integer grammar the read side's `int(row.value)` will later accept.
        return int(value)
    except (TypeError, ValueError) as error:
        raise InvalidSettingValue(key) from error


def _check_integer_bounds(key: str, parsed: int) -> None:
    bounds = _INTEGER_BOUNDS.get(key)
    if bounds is None:
        return
    minimum, maximum = bounds
    if (minimum is not None and parsed < minimum) or (maximum is not None and parsed > maximum):
        raise InvalidSettingValue(key)


def _parse_decimal(key: str, value: str) -> None:
    try:
        Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise InvalidSettingValue(key) from error


def _parse_time(key: str, value: str) -> None:
    try:
        time.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise InvalidSettingValue(key) from error


def _parse_json(key: str, value: str) -> None:
    try:
        json.loads(value)
    except (TypeError, ValueError) as error:
        raise InvalidSettingValue(key) from error


def update_settings(
    session: Session,
    *,
    changes: Mapping[str, str],
    context: AuditContext,
) -> list[Setting]:
    """Apply a set of setting changes, validated and audited, and return the whole list afterwards.

    Each key must already be a row (`SettingNotFound` otherwise — this endpoint tunes, it does not
    create), and each new value must fit the row's declared type and any semantic bound
    (`InvalidSettingValue` otherwise). Every change is validated *before* any is written, so a batch
    with one bad value stores none of it — an administrator who fat-fingers the working-day length
    does not leave half their edits applied.

    A row is snapshotted before it moves and diffed with `record_model_changes`, so only a value that
    actually changed produces an audit row (`entity_type` `settings`), attributed to the acting
    administrator. Like the rest of the service layer this does not commit: the router owns the
    transaction, and the audit rows ride with it (Requirement 13.2).
    """
    # Load and validate everything first. A single loop that wrote as it validated could leave half a
    # batch applied before hitting a bad value; resolving the rows up front keeps the write
    # all-or-nothing — a bad value raises here, before any row has been touched.
    resolved: list[tuple[Setting, str]] = []
    for key, value in changes.items():
        setting = _load(session, key)
        resolved.append((setting, _validate_value(setting.key, setting.value_type, value)))

    for setting, new_value in resolved:
        if setting.value == new_value:
            continue  # No change; snapshot-and-diff would emit nothing, so skip the write entirely.
        before = snapshot(setting)
        setting.value = new_value
        setting.updated_by_user_id = context.actor_user_id
        session.flush()
        record_model_changes(session, setting, before, context=context, reason="setting_updated")

    return list_settings(session)


__all__ = [
    "InvalidSettingValue",
    "SettingNotFound",
    "SettingValueType",
    "get_int",
    "get_int_or",
    "get_str",
    "get_time",
    "list_settings",
    "update_settings",
]
