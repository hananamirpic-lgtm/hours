import { describe, expect, it } from 'vitest';

import type { DashboardAttention } from '@/api/types';
import {
  attentionItems,
  currentPeriod,
  hasAttention,
  monthEnd,
  monthStart,
  parseAmount,
  parseMonthValue,
  toMonthValue,
} from '@/app/dashboard/dashboardView';

const anAttention = (over: Partial<DashboardAttention> = {}): DashboardAttention => ({
  missing_checkout: 0,
  missing_checkin: 0,
  missing_reports: 0,
  ...over,
});

describe('parseAmount — a decimal string to a number for formatting', () => {
  it('parses a plain decimal', () => {
    expect(parseAmount('645.00')).toBe(645);
    expect(parseAmount('312.50')).toBe(312.5);
  });

  it('reads a missing, null or unparseable value as zero, never NaN', () => {
    expect(parseAmount(null)).toBe(0);
    expect(parseAmount(undefined)).toBe(0);
    expect(parseAmount('')).toBe(0);
    expect(parseAmount('not-a-number')).toBe(0);
  });
});

describe('currentPeriod — the calendar month a date falls in', () => {
  it('reads the year and 1-based month from a date', () => {
    expect(currentPeriod(new Date(2025, 7, 15))).toEqual({ year: 2025, month: 8 });
    expect(currentPeriod(new Date(2025, 0, 1))).toEqual({ year: 2025, month: 1 });
    expect(currentPeriod(new Date(2025, 11, 31))).toEqual({ year: 2025, month: 12 });
  });
});

describe('month-input value round-trips', () => {
  it('formats a period as a zero-padded YYYY-MM', () => {
    expect(toMonthValue({ year: 2025, month: 8 })).toBe('2025-08');
    expect(toMonthValue({ year: 2025, month: 12 })).toBe('2025-12');
  });

  it('parses a YYYY-MM value back to a period', () => {
    expect(parseMonthValue('2025-08')).toEqual({ year: 2025, month: 8 });
  });

  it('returns null for a value that is not a month', () => {
    expect(parseMonthValue('')).toBeNull();
    expect(parseMonthValue('2025')).toBeNull();
    expect(parseMonthValue('2025-8')).toBeNull();
  });
});

describe('month range — the calendar span a period covers (Requirement 18.6)', () => {
  it('starts on the first of the month', () => {
    expect(monthStart({ year: 2025, month: 8 })).toBe('2025-08-01');
    expect(monthStart({ year: 2025, month: 2 })).toBe('2025-02-01');
  });

  it('ends on the last day of the month', () => {
    expect(monthEnd({ year: 2025, month: 8 })).toBe('2025-08-31'); // 31-day month
    expect(monthEnd({ year: 2025, month: 4 })).toBe('2025-04-30'); // 30-day month
    expect(monthEnd({ year: 2025, month: 2 })).toBe('2025-02-28'); // non-leap February
    expect(monthEnd({ year: 2024, month: 2 })).toBe('2024-02-29'); // leap February
  });

  it('rolls December to the 31st, not into the next year', () => {
    expect(monthEnd({ year: 2025, month: 12 })).toBe('2025-12-31');
  });
});

describe('attentionItems — the three attention items (Requirement 18.6)', () => {
  it('maps the counts onto the three kinds in display order', () => {
    const items = attentionItems(
      anAttention({ missing_checkout: 2, missing_checkin: 1, missing_reports: 3 }),
    );
    expect(items.map((item) => item.kind)).toEqual([
      'missing_checkout',
      'missing_checkin',
      'both_missing',
    ]);
    expect(items.map((item) => item.count)).toEqual([2, 1, 3]);
  });
});

describe('hasAttention — whether anything needs attention', () => {
  it('is true when any count is non-zero', () => {
    expect(hasAttention(anAttention({ missing_checkout: 1 }))).toBe(true);
    expect(hasAttention(anAttention({ missing_checkin: 1 }))).toBe(true);
    expect(hasAttention(anAttention({ missing_reports: 1 }))).toBe(true);
  });

  it('is false when every count is zero', () => {
    expect(hasAttention(anAttention())).toBe(false);
  });
});
