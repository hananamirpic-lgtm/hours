/**
 * Settings endpoints — the tuning knobs the calculation engine reads (the settings screen).
 *
 * `GET /api/settings` returns every knob with its stored value and declared type; `PATCH /api/settings`
 * applies a set of changes keyed by name and returns the whole list afterwards. Both are
 * administrator-only on the server — tuning the engine is the administrator's job — so a non-admin
 * caller gets a 403, which the settings page surfaces as an admin-only note rather than a load error.
 *
 * Locale-neutral: the server returns the key, the stored text value and the declared type, and the
 * screen composes the labels and picks the input in the reader's language. A change alters future
 * calculations only; nothing already computed is rewritten, and this module triggers no recalculation.
 */

import type { SettingsListResponse, SettingsUpdate } from './types';

import { apiFetch } from './client';

export const settingsKeys = {
  all: ['settings'] as const,
  list: () => ['settings', 'list'] as const,
};

/** Every tuning knob with its stored value and declared type, ordered by key (GET /api/settings). */
export const listSettings = (): Promise<SettingsListResponse> =>
  apiFetch<SettingsListResponse>('/settings');

/**
 * Apply a set of setting changes and return the whole list afterwards (PATCH /api/settings).
 *
 * `updates` maps a setting key to its new text value. The server validates each value against its
 * type and range and refuses an unknown key, so a rejected change leaves every setting untouched and
 * comes back as an `ApiError` the page translates.
 */
export const updateSettings = (updates: Record<string, string>): Promise<SettingsListResponse> =>
  apiFetch<SettingsListResponse>('/settings', {
    method: 'PATCH',
    body: JSON.stringify({ updates } satisfies SettingsUpdate),
  });
