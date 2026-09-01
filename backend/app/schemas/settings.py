"""Settings request and response schemas (the settings screen).

The settings surface reads and tunes the calculation engine's knobs — the working-day length, the
Shabbat window, the anomaly thresholds — that live in the `settings` table as text plus a declared
type. This module is the shape of that read and that write.

Locale-neutral like every other schema. A setting is served with its `key`, its stored text `value`,
its `value_type` (so the screen can render the right input — a number box, a time picker, a checkbox)
and the server's `description` as a fallback label; no message text is composed here, because the
screen labels each known key from its own resource files and falls back to the description for any
key it does not yet name.

`extra="forbid"` on the write model, the same posture the rest of the write surface takes: a request
carrying a field the schema does not name is rejected rather than silently dropped.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.models.setting import SettingValueType


class SettingItem(BaseModel):
    """One tuning knob as the settings screen reads it.

    `value` is the stored text, untyped, exactly as the column holds it; the screen parses it
    according to `value_type`. `description` is the seeded, locale-neutral note on the knob — the
    screen prefers its own translated label for a known key and uses this only as the fallback for a
    key it does not recognise, so a knob added later is still shown sensibly without a code change.
    """

    model_config = ConfigDict(from_attributes=True)

    key: str
    value: str
    value_type: SettingValueType
    description: str | None


class SettingsListResponse(BaseModel):
    """Every settings row, ordered by key, as served by `GET /api/settings`.

    The whole table rather than a page: the settings screen shows all the knobs at once, and there is
    a small, fixed number of them, so there is nothing to paginate.
    """

    items: list[SettingItem]


class SettingsUpdate(BaseModel):
    """The body of `PATCH /api/settings`: the knobs to change, keyed by name (the settings screen).

    `updates` maps a setting key to its new text value, the same untyped form the row stores; the
    service validates each value against its row's declared type and any semantic bound before writing
    anything. Only the keys present are touched, and a key that is not already a settings row is
    refused — this endpoint tunes the seeded knobs, it does not create new ones.
    """

    model_config = ConfigDict(extra="forbid")

    updates: dict[str, str] = Field(min_length=1)
