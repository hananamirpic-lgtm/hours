import { describe, expect, it } from 'vitest';

import {
  CANONICAL_DATE_FORMAT,
  formatCurrency,
  formatDate,
  formatDuration,
  formatNumber,
  formatTime,
  isCanonicalDate,
  toCanonicalDate,
} from '@/lib/format';

describe('canonical entry date', () => {
  it('is ISO year-month-day', () => {
    expect(CANONICAL_DATE_FORMAT).toBe('YYYY-MM-DD');
  });

  it('accepts a well-formed real date', () => {
    expect(isCanonicalDate('2025-08-30')).toBe(true);
  });

  it.each(['30-08-2025', '2025/08/30', '2025-8-30', '2025-13-01', '2025-02-30', 'not-a-date', ''])(
    'rejects %s',
    (value) => {
      expect(isCanonicalDate(value)).toBe(false);
    },
  );

  it('round-trips a Date to the canonical string', () => {
    expect(toCanonicalDate(new Date('2025-08-30T12:00:00'))).toBe('2025-08-30');
  });
});

describe('duration', () => {
  it.each([
    [90, '1:30'],
    [0, '0:00'],
    [59, '0:59'],
    [60, '1:00'],
    [570, '9:30'],
    [-90, '-1:30'],
  ])('formats %i minutes as %s', (minutes, expected) => {
    expect(formatDuration(minutes)).toBe(expected);
  });
});

describe('locale-aware output', () => {
  it('formats currency as two-place shekels in both languages', () => {
    // The amount and the currency mark are present in both; the arrangement differs by locale, which
    // is exactly what Intl is here to get right.
    for (const language of ['he', 'en'] as const) {
      const formatted = formatCurrency(language, 7420);
      expect(formatted).toContain('7,420.00');
      expect(formatted).toMatch(/₪|ILS/);
    }
  });

  it('formats a number without forcing decimals', () => {
    expect(formatNumber('en', 8)).toBe('8');
    expect(formatNumber('en', 1.5)).toBe('1.5');
  });

  it('formats a date and a time from an ISO string', () => {
    const iso = '2025-08-30T18:42:00';
    expect(formatDate('en', iso)).toMatch(/2025/);
    expect(formatTime('en', iso)).toMatch(/42/);
  });
});
