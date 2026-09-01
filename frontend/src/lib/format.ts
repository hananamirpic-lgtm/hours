/**
 * Locale-aware formatting for dates, times, numbers and currency (Requirement 21.4).
 *
 * The API is locale-neutral: it returns ISO 8601 timestamps and raw decimals, and every bit of
 * formatting happens here. That keeps one implementation of "how a shekel amount looks" rather than
 * one per screen, and it is the seam the export renderers reuse so a PDF and the web agree.
 *
 * `Intl` does the heavy lifting, so Hebrew and English are equally first-class: the same call formats
 * both, differing only in the locale tag. Currency is fixed to ILS because the system bills and pays
 * in shekels; the day it does not, this is the one place that changes.
 */

import type { Language } from '@/i18n';

/** BCP 47 tags for the two supported languages. `he-IL` and `en-IL` both format money as ₪. */
const LOCALES: Record<Language, string> = {
  he: 'he-IL',
  en: 'en-IL',
};

const CURRENCY = 'ILS';

const localeOf = (language: Language): string => LOCALES[language];

/**
 * The one date format data entry uses, in every language (Requirement 21.4: "a single canonical date
 * format for data entry"). Display is localised; entry is not, because a form that parses dd/mm in
 * one language and mm/dd in another is a form that silently swaps 03/08 for 08/03.
 */
export const CANONICAL_DATE_FORMAT = 'YYYY-MM-DD';
const CANONICAL_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

/** Whether a string is a canonical entry date. The form validates against this before it submits. */
export const isCanonicalDate = (value: string): boolean => {
  if (!CANONICAL_DATE_PATTERN.test(value)) {
    return false;
  }
  const parsed = new Date(`${value}T00:00:00`);
  return !Number.isNaN(parsed.getTime()) && toCanonicalDate(parsed) === value;
};

/** Render a date as the canonical entry string, e.g. for pre-filling a date input. */
export const toCanonicalDate = (value: Date): string => {
  const year = value.getFullYear().toString().padStart(4, '0');
  const month = (value.getMonth() + 1).toString().padStart(2, '0');
  const day = value.getDate().toString().padStart(2, '0');
  return `${year}-${month}-${day}`;
};

const toDate = (value: Date | string): Date => (value instanceof Date ? value : new Date(value));

/** A calendar date in the reader's locale: 30 באוג׳ 2025 / Aug 30, 2025. */
export const formatDate = (language: Language, value: Date | string): string =>
  new Intl.DateTimeFormat(localeOf(language), { dateStyle: 'medium' }).format(toDate(value));

/** A wall-clock time in the reader's locale, without seconds: 18:42. */
export const formatTime = (language: Language, value: Date | string): string =>
  new Intl.DateTimeFormat(localeOf(language), { hour: '2-digit', minute: '2-digit' }).format(toDate(value));

/** Date and time together: 30 באוג׳ 2025, 18:42. Used wherever an audit line names when something happened. */
export const formatDateTime = (language: Language, value: Date | string): string =>
  new Intl.DateTimeFormat(localeOf(language), { dateStyle: 'medium', timeStyle: 'short' }).format(
    toDate(value),
  );

/** A decimal number in the reader's locale, with sensible defaults for hours (up to two places). */
export const formatNumber = (
  language: Language,
  value: number,
  options: Intl.NumberFormatOptions = {},
): string =>
  new Intl.NumberFormat(localeOf(language), { maximumFractionDigits: 2, ...options }).format(value);

/** A shekel amount, always two places: ₪7,420.00 / 7,420.00 ₪. */
export const formatCurrency = (language: Language, value: number): string =>
  new Intl.NumberFormat(localeOf(language), {
    style: 'currency',
    currency: CURRENCY,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);

/**
 * Whole minutes as hours and minutes: 90 → "1:30". Attendance is counted in minutes, and this is how
 * a duration reads on the hours view. Digits only, so the caller isolates it against surrounding
 * Hebrew text (see the `.numeric` rule in the stylesheet).
 */
export const formatDuration = (totalMinutes: number): string => {
  const sign = totalMinutes < 0 ? '-' : '';
  const absolute = Math.abs(Math.trunc(totalMinutes));
  const hours = Math.floor(absolute / 60);
  const minutes = absolute % 60;
  return `${sign}${hours}:${minutes.toString().padStart(2, '0')}`;
};
