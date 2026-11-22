// Form error helpers (R23.13 focus on the first failing field, R8.12 text for every code with a generic
// fallback). Pure: no React, no DOM, no i18n; the caller translates the keys.

import { isErrorCode, type ErrorCode } from '../api/errorCodes';

/** The `fields` map of the API error envelope: field name to error code. */
export type FieldErrors = Readonly<Record<string, string>>;

export type ErrorTextKey = `errors.${ErrorCode}` | 'errors.generic';

export interface FieldMessage {
  readonly field: string;
  readonly textKey: ErrorTextKey;
}

function hasField(fields: FieldErrors, name: string): boolean {
  return Object.prototype.hasOwnProperty.call(fields, name);
}

/**
 * The first name in `domOrder` (reading order) that has an entry in `fields`, or null when none has,
 * when `fields` is empty, or when `fields` is null or undefined. Fields that are not in `domOrder`
 * cannot take focus and are ignored here; `fieldsToMessages` still lists them.
 */
export function firstFailingField(
  fields: FieldErrors | null | undefined,
  domOrder: readonly string[],
): string | null {
  if (!fields) return null;
  for (const name of domOrder) {
    if (hasField(fields, name)) return name;
  }
  return null;
}

/** `errors.<code>` for a code the API defines, `errors.generic` for anything else (R8.12). */
export function errorTextKey(code: string | null | undefined): ErrorTextKey {
  return isErrorCode(code) ? `errors.${code}` : 'errors.generic';
}

/**
 * One `{field, textKey}` pair per entry of `fields`: fields in `domOrder` first, in that order (a name
 * repeated in `domOrder` counts once), then fields not in `domOrder` in the order the server sent them.
 * Empty for null, undefined or empty `fields`.
 */
export function fieldsToMessages(
  fields: FieldErrors | null | undefined,
  domOrder: readonly string[],
): FieldMessage[] {
  if (!fields) return [];
  const ordered: string[] = [];
  const seen = new Set<string>();
  for (const name of domOrder) {
    if (hasField(fields, name) && !seen.has(name)) {
      seen.add(name);
      ordered.push(name);
    }
  }
  for (const name of Object.keys(fields)) {
    if (!seen.has(name)) ordered.push(name);
  }
  return ordered.map((field) => ({ field, textKey: errorTextKey(fields[field]) }));
}
