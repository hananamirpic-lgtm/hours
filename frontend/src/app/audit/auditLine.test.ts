import { describe, expect, it } from 'vitest';

import type { AuditEntry } from '@/api/types';
import en from '@/i18n/locales/en/common.json';
import { auditFieldLabel, composeAuditLine, formatAuditValue, type Translate } from '@/app/audit/auditLine';

/**
 * A minimal translator standing in for i18next: it resolves a dotted key against the English
 * resources, honours the `[key, fallback]` array form and `defaultValue` that `auditFieldLabel` uses,
 * and interpolates `{{name}}` placeholders. Enough to prove the sentence `composeAuditLine` builds,
 * without a React tree or the full i18n runtime.
 */
const resolve = (key: string): string | undefined => {
  let node: unknown = en;
  for (const part of key.split('.')) {
    if (typeof node !== 'object' || node === null || !(part in node)) {
      return undefined;
    }
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === 'string' ? node : undefined;
};

const interpolate = (template: string, options?: Record<string, unknown>): string =>
  template.replace(/\{\{(\w+)\}\}/g, (_, name: string) => String(options?.[name] ?? ''));

function translate(key: string | string[], options?: Record<string, unknown>): string {
  const keys = Array.isArray(key) ? key : [key];
  for (const candidate of keys) {
    const found = resolve(candidate);
    if (found !== undefined) {
      return interpolate(found, options);
    }
  }
  return String(options?.defaultValue ?? keys[keys.length - 1]);
}

const t: Translate = translate;

const anEntry = (over: Partial<AuditEntry> = {}): AuditEntry => ({
  id: 'c1',
  entity_type: 'time_entries',
  entity_id: 'te-1',
  field: 'check_out_at',
  old_value: '2025-08-30T15:30:00Z',
  new_value: '2025-08-30T16:00:00Z',
  actor_name: 'Abraham',
  changed_at: '2025-08-30T18:42:00Z',
  reason: null,
  request_id: null,
  ...over,
});

describe('formatAuditValue', () => {
  it('renders an ISO instant as a wall-clock time', () => {
    expect(formatAuditValue('2025-08-30T15:30:00Z', 'en', t)).toMatch(/30|15|18/);
    // A time, not a full date: no year in the output.
    expect(formatAuditValue('2025-08-30T15:30:00Z', 'en', t)).not.toMatch(/2025/);
  });

  it('renders an ISO date as a localised date', () => {
    expect(formatAuditValue('2025-08-30', 'en', t)).toMatch(/2025/);
  });

  it('renders a boolean string as a word', () => {
    expect(formatAuditValue('true', 'en', t)).toBe('yes');
    expect(formatAuditValue('false', 'en', t)).toBe('no');
  });

  it('shows any other value verbatim, including the redaction marker', () => {
    expect(formatAuditValue('Foreman', 'en', t)).toBe('Foreman');
    expect(formatAuditValue('[redacted]', 'en', t)).toBe('[redacted]');
  });
});

describe('auditFieldLabel', () => {
  it('translates a known field', () => {
    expect(auditFieldLabel('check_out_at', t)).toBe('check-out');
  });

  it('falls back to the raw field name when untranslated', () => {
    expect(auditFieldLabel('some_new_column', t)).toBe('some_new_column');
  });
});

describe('composeAuditLine (Requirement 13.4)', () => {
  it('builds the brief example: who changed a field from one time to another', () => {
    const line = composeAuditLine(anEntry(), 'en', t);
    // "… — Abraham changed check-out from 15:30 to 16:00" (times localised, not literal).
    expect(line).toContain('Abraham');
    expect(line).toContain('changed');
    expect(line).toContain('check-out');
    expect(line).toContain('from');
    expect(line).toContain('to');
    // The timestamp is present as a date-and-time, so it carries the year.
    expect(line).toMatch(/2025/);
  });

  it('uses the "set" clause when there is no previous value', () => {
    const line = composeAuditLine(anEntry({ old_value: null }), 'en', t);
    expect(line).toContain('set');
    expect(line).not.toContain('from');
  });

  it('uses the "cleared" clause when there is no new value', () => {
    const line = composeAuditLine(anEntry({ new_value: null }), 'en', t);
    expect(line).toContain('cleared');
  });

  it('names the system when there is no actor', () => {
    const line = composeAuditLine(anEntry({ actor_name: null }), 'en', t);
    expect(line).toContain('the system');
  });

  it('appends the reason when one is present', () => {
    const line = composeAuditLine(anEntry({ reason: 'clock drift corrected' }), 'en', t);
    expect(line).toContain('clock drift corrected');
    expect(line).toMatch(/\(clock drift corrected\)$/);
  });
});
