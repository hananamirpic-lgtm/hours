/**
 * Composing a readable audit line from a locale-neutral change row (Requirement 13.4, 21.6).
 *
 * The server stores each change as stable strings — the field, the old and new values, the actor, the
 * timestamp and the reason — and never assembles prose, because a sentence built on the server could
 * not be translated. This module is where the sentence is built, in the reader's language, from those
 * parts: "30/08 18:42 — Abraham changed check-out from 15:30 to 16:00".
 *
 * It is pure and takes the translator and language as arguments, so it can be unit-tested without a
 * React tree and reused by an export renderer later. Three shapes of change read differently and each
 * gets its own sentence: a field set for the first time (no old value), a field cleared (no new
 * value), and a field moved from one value to another. The values are formatted for display —
 * an ISO timestamp becomes a wall-clock time, an ISO date a localised date — while anything else is
 * shown as the string the server sent, including the redaction marker a sensitive field carries, so
 * the line can say a passport number changed without ever holding its value.
 */

import type { AuditEntry } from '@/api/types';
import type { Language } from '@/i18n';
import { formatDate, formatDateTime, formatTime } from '@/lib/format';

/**
 * The translator shape this module needs — the subset of i18next's `t` it calls, expressed as two
 * overloads (with and without options) so i18next's own `TFunction` is assignable to it under
 * `exactOptionalPropertyTypes`, where a single optional-argument signature is not.
 */
export interface Translate {
  (key: string | string[]): string;
  (key: string | string[], options: Record<string, unknown>): string;
}

const ISO_DATETIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * A stored value rendered for display in the reader's locale.
 *
 * An ISO 8601 instant (a check-in, a timestamp) becomes a wall-clock time, since audit lines compare
 * times of day; an ISO date becomes a localised date; a boolean string becomes a yes/no word; and
 * everything else — an enum value, a number, a name, the redaction marker — is shown verbatim, because
 * the server already rendered it to the one canonical form the trail stores. `null` has no display
 * form and is handled by the caller, which picks the "set" or "cleared" sentence instead.
 */
export const formatAuditValue = (value: string, language: Language, t: Translate): string => {
  if (ISO_DATETIME.test(value)) {
    return formatTime(language, value);
  }
  if (ISO_DATE.test(value)) {
    return formatDate(language, value);
  }
  if (value === 'true' || value === 'false') {
    return t(`audit.boolean.${value}`);
  }
  return value;
};

/**
 * The human label for a changed field, in the reader's language.
 *
 * Looked up under `audit.field.<name>` with the raw field name as the fallback, so a field without a
 * translation still reads sensibly (as the column name) rather than breaking the line, and adding a
 * label later needs no code change. The `authorization` pseudo-field — the stream of denials and auth
 * events — and similar are covered by the same table.
 */
export const auditFieldLabel = (field: string, t: Translate): string =>
  t([`audit.field.${field}`, field], { defaultValue: field });

/**
 * The full readable line for one change: when, who, and what moved (Requirement 13.4).
 *
 * The `when` is the change's timestamp as date-and-time in the reader's locale. The `who` is the
 * actor's name, or the "system" word when the change was made on the system's own account (a
 * scheduled job or a site transition — Requirement 11.8). The verb clause depends on which values are
 * present: a field set for the first time reads "set X to V", a cleared field "cleared X (was V)", and
 * an ordinary change "changed X from A to B". The reason, when present, is appended as its own clause
 * so the line answers "why" as well as "what".
 */
export const composeAuditLine = (entry: AuditEntry, language: Language, t: Translate): string => {
  const when = formatDateTime(language, entry.changed_at);
  const who = entry.actor_name ?? t('audit.system');
  const field = auditFieldLabel(entry.field, t);

  const hasOld = entry.old_value !== null;
  const hasNew = entry.new_value !== null;

  let clause: string;
  if (hasOld && hasNew) {
    clause = t('audit.clause.changed', {
      who,
      field,
      from: formatAuditValue(entry.old_value as string, language, t),
      to: formatAuditValue(entry.new_value as string, language, t),
    });
  } else if (hasNew) {
    clause = t('audit.clause.set', {
      who,
      field,
      to: formatAuditValue(entry.new_value as string, language, t),
    });
  } else if (hasOld) {
    clause = t('audit.clause.cleared', {
      who,
      field,
      from: formatAuditValue(entry.old_value as string, language, t),
    });
  } else {
    // Neither value present — a bare event with no before/after, e.g. an authentication or
    // authorization record. State that the actor touched the field, without inventing values.
    clause = t('audit.clause.noted', { who, field });
  }

  const line = t('audit.line', { when, clause });
  if (entry.reason) {
    return t('audit.lineWithReason', { line, reason: entry.reason });
  }
  return line;
};
