/**
 * Pure view helpers for the dashboards (Requirement 18.4, 18.5, 18.6).
 *
 * The API is locale-neutral: money arrives as raw decimal strings, minutes as integers and dates as
 * ISO strings. These functions turn that into the small, testable shapes the screens render — a
 * current-month period, the calendar range that month spans, and the attention items with their
 * counts and the missing-report kind each links to — without touching React or i18n, so the
 * arithmetic can be tested on its own and the components stay thin renderers.
 *
 * Money stays a string end to end and is parsed to a number only where `formatCurrency` needs one;
 * the parse is centralised here so every screen reads a decimal string the same way. The figures
 * themselves are the backend's — computed with `Decimal` and `ROUND_HALF_UP` — so this never
 * re-derives an amount, it only presents what was calculated.
 */

import type { DashboardAttention, MissingReportKind } from '@/api/types';

/**
 * Parse a locale-neutral decimal string (e.g. "645.00") to a number for formatting. An empty, missing
 * or unparseable value reads as zero, so a partial payload renders as ₪0.00 rather than NaN.
 */
export const parseAmount = (value: string | null | undefined): number => {
  if (value === null || value === undefined || value === '') {
    return 0;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
};

export interface Period {
  year: number;
  month: number;
}

/** The current calendar year and month, the period the dashboard and profitability filter default to. */
export const currentPeriod = (today: Date = new Date()): Period => ({
  year: today.getFullYear(),
  month: today.getMonth() + 1,
});

/** A `YYYY-MM` value for a month input, zero-padding the month. */
export const toMonthValue = (period: Period): string =>
  `${period.year}-${String(period.month).padStart(2, '0')}`;

/** Parse a `YYYY-MM` month-input value back to a period, or null when it is not one. */
export const parseMonthValue = (value: string): Period | null => {
  const match = /^(\d{4})-(\d{2})$/.exec(value);
  if (!match) {
    return null;
  }
  return { year: Number(match[1]), month: Number(match[2]) };
};

/** The first calendar day of a period, as a canonical `YYYY-MM-DD` date string. */
export const monthStart = (period: Period): string =>
  `${period.year}-${String(period.month).padStart(2, '0')}-01`;

/**
 * The last calendar day of a period, as a canonical `YYYY-MM-DD` date string. December rolls to the
 * 31st; every other month is the day before the first of the next month. Kept here so the dashboard's
 * attention links carry the same month range the server counted over.
 */
export const monthEnd = (period: Period): string => {
  const nextMonthFirst =
    period.month === 12 ? new Date(period.year + 1, 0, 1) : new Date(period.year, period.month, 1);
  const last = new Date(nextMonthFirst.getTime() - 24 * 60 * 60 * 1000);
  const year = last.getFullYear().toString().padStart(4, '0');
  const month = (last.getMonth() + 1).toString().padStart(2, '0');
  const day = last.getDate().toString().padStart(2, '0');
  return `${year}-${month}-${day}`;
};

export interface AttentionItem {
  /** Which missing-report kind this item counts and links to (Requirement 18.6). */
  kind: MissingReportKind;
  /** The translation key for the item's label. */
  labelKey: string;
  count: number;
}

/**
 * The three attention items in display order, from the dashboard's attention counts (Requirement
 * 18.6). Each carries the kind it links to so the screen can build a link to the missing-report list
 * filtered to that kind; the order — missing check-out, missing check-in, both missing — is the same
 * as the missing-report list's kinds so the two read alike.
 */
export const attentionItems = (attention: DashboardAttention): AttentionItem[] => [
  {
    kind: 'missing_checkout',
    labelKey: 'dashboard.attention.missingCheckout',
    count: attention.missing_checkout,
  },
  {
    kind: 'missing_checkin',
    labelKey: 'dashboard.attention.missingCheckin',
    count: attention.missing_checkin,
  },
  {
    kind: 'both_missing',
    labelKey: 'dashboard.attention.missingReports',
    count: attention.missing_reports,
  },
];

/** Whether any attention item carries a non-zero count — otherwise the section says "all clear". */
export const hasAttention = (attention: DashboardAttention): boolean =>
  attention.missing_checkout > 0 ||
  attention.missing_checkin > 0 ||
  attention.missing_reports > 0;
