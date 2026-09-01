/**
 * Pure view helpers for the payroll screen (Requirement 16.6, 16.8).
 *
 * The API is locale-neutral: money arrives as raw decimal strings and minutes as integers. These
 * functions turn that payload into the small, testable shapes the screen renders — the bucket
 * breakdown a summary shows, and the total minutes an allocation represents — without touching React
 * or i18n, so the arithmetic can be tested on its own and the component stays a thin renderer.
 *
 * Money stays a string end to end and is parsed to a number only at the edge, where `formatCurrency`
 * needs one; the parse is centralised here so every screen reads a decimal string the same way. The
 * figures themselves are the backend's — computed with `Decimal` and `ROUND_HALF_UP` (Requirement
 * 16.7) — so this never re-derives an amount, it only presents what was calculated.
 */

import type { PayrollRecord, SiteAllocation } from '@/api/types';

/**
 * Parse a locale-neutral decimal string (e.g. "7420.00") to a number for formatting. An empty,
 * missing or unparseable value reads as zero, so a partial payload renders as ₪0.00 rather than NaN.
 */
export const parseAmount = (value: string | null | undefined): number => {
  if (value === null || value === undefined || value === '') {
    return 0;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
};

/** One pay bucket as the summary lists it: its minutes and its pay, ready to format. */
export interface PayBucket {
  /** The translation key under `payroll.bucket` for this bucket's label. */
  key: 'regular' | 'overtime' | 'shabbat' | 'holiday';
  minutes: number;
  pay: number;
}

/**
 * The four pay buckets of a record in display order (Requirement 16.6). Each carries its minutes and
 * its pay so the summary shows both the hours and the amount without re-deriving one from the other.
 */
export const payBuckets = (record: PayrollRecord): PayBucket[] => [
  { key: 'regular', minutes: record.regular_minutes, pay: parseAmount(record.regular_pay) },
  { key: 'overtime', minutes: record.overtime_minutes, pay: parseAmount(record.overtime_pay) },
  { key: 'shabbat', minutes: record.shabbat_minutes, pay: parseAmount(record.shabbat_pay) },
  { key: 'holiday', minutes: record.holiday_minutes, pay: parseAmount(record.holiday_pay) },
];

/** The total minutes worked across all four buckets, for a record's list-row summary. */
export const totalMinutes = (record: PayrollRecord): number =>
  record.regular_minutes + record.overtime_minutes + record.shabbat_minutes + record.holiday_minutes;

/** The total minutes worked at one site across its four buckets (Requirement 16.8). */
export const allocationMinutes = (allocation: SiteAllocation): number =>
  allocation.regular_minutes +
  allocation.overtime_minutes +
  allocation.shabbat_minutes +
  allocation.holiday_minutes;

/**
 * The sum of a record's per-site allocation costs (Requirement 16.8). The backend corrects these with
 * a largest-remainder pass so they equal the worked pay (the four bucket pays) to the agora; this
 * lets the drill-down show that the parts add up to the whole.
 */
export const allocationsTotal = (record: PayrollRecord): number =>
  record.allocations.reduce((sum, allocation) => sum + parseAmount(allocation.cost), 0);
