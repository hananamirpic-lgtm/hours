/**
 * Pure presentation helpers for the settings screen.
 *
 * The screen shows every tuning knob the calculation engine reads, but two decisions do not belong in
 * the component: which input a value type gets, and how the working-day length is presented. Both are
 * pure functions of the setting, so they live here where they can be unit-tested without a React tree.
 *
 * The working-day length (`overtime_daily_threshold_minutes`) is stored in minutes, because that is
 * what the engine measures, but an administrator thinks in hours — an eight-hour day, not a
 * 480-minute one. So this knob is presented in hours: the stored minutes are shown as hours on the
 * way in, and the entered hours are converted back to whole minutes on the way out, rounded to the
 * nearest minute so a fractional hour (7.5) stores cleanly (450) rather than a floating-point tail.
 */

import type { SettingItem } from '@/api/types';

/** The setting whose stored minutes are presented to the administrator as hours. */
export const WORKING_DAY_KEY = 'overtime_daily_threshold_minutes';

/** The kind of input the screen renders for a setting, derived from its declared value type. */
export type SettingInputKind = 'number' | 'time' | 'checkbox' | 'text';

/**
 * Which input a setting gets. The working-day length is a number (of hours); otherwise a decimal or
 * integer is a number box, a time is a time picker, a boolean a checkbox, and anything else a text box.
 */
export const inputKindFor = (setting: SettingItem): SettingInputKind => {
  switch (setting.value_type) {
    case 'integer':
    case 'decimal':
      return 'number';
    case 'time':
      return 'time';
    case 'boolean':
      return 'checkbox';
    default:
      return 'text';
  }
};

/** Whether a setting is presented in hours rather than its stored minutes. */
export const isWorkingDayLength = (key: string): boolean => key === WORKING_DAY_KEY;

/**
 * The value to show in the input for a setting: the stored text as-is, except the working-day length,
 * whose stored minutes become hours (480 → "8", 450 → "7.5"). A stored value that does not parse as a
 * number falls back to the raw text, so a malformed row is still shown rather than blanked.
 */
export const toDisplayValue = (setting: SettingItem): string => {
  if (!isWorkingDayLength(setting.key)) {
    return setting.value;
  }
  const minutes = Number(setting.value);
  if (!Number.isFinite(minutes)) {
    return setting.value;
  }
  return String(minutes / 60);
};

/**
 * The text to send for a setting given what the administrator typed. For the working-day length the
 * entered hours are converted to whole minutes, rounded to the nearest minute (7.5 h → "450"); a
 * non-numeric entry is passed through unchanged so the server's validation, not this helper, is what
 * reports it. Every other setting sends exactly what was typed.
 */
export const toStoredValue = (key: string, entered: string): string => {
  if (!isWorkingDayLength(key)) {
    return entered;
  }
  const hours = Number(entered);
  if (entered.trim() === '' || !Number.isFinite(hours)) {
    return entered;
  }
  return String(Math.round(hours * 60));
};

/**
 * The i18n label key for a setting: `settings.label.<key>`, with the raw key as the array fallback so
 * an unknown knob still resolves (the page then prefers the server `description`). Kept here so the
 * page and any future export agree on the key shape.
 */
export const labelKeyFor = (key: string): string[] => [`settings.label.${key}`, key];

/** The i18n helptext key for a setting: `settings.help.<key>`. The page falls back to `description`. */
export const helpKeyFor = (key: string): string => `settings.help.${key}`;
