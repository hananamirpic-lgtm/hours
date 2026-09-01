/**
 * Pure date math and grouping for the employee "My hours" screen (Requirement 23.3, 11.2).
 *
 * Everything here is a pure function over plain values — no React, no fetching, no clock reads except
 * the `today` a caller passes in — so the preset windows and the weekly grouping can be unit-tested
 * directly. The screen composes these into queries and rendered groups.
 *
 * **Week definition.** The business week is Sunday..Saturday, the Israeli convention: a week's first
 * day is the Sunday on or before a date, its last day the following Saturday. "This week" is the
 * Sun..Sat window containing today; the month view's weekly subtotals group each day into the Sun..Sat
 * week it falls in. This is deliberately the plain calendar week, not a rolling 7-day window, so two
 * days in the same calendar week always share a group and the grouping is predictable.
 *
 * **Local time.** Windows are computed from the caller's `today`, a `Date` in the browser's local
 * time, and every boundary is rendered as a canonical `YYYY-MM-DD` through `toCanonicalDate`, the same
 * form the date inputs and the API use. No UTC conversion happens here: an employee picking "this
 * month" means the month on their own calendar.
 */

import { toCanonicalDate } from '@/lib/format';

/** An inclusive from..to window as canonical dates, ready to hand to `getWorkHistory`. */
export interface DateRange {
  from: string;
  to: string;
}

/** The quick presets the filter offers. `custom` means the two date inputs drive the range. */
export type HoursPreset = 'week' | 'month' | 'day' | 'custom';

/** Days per week, Sunday = 0 (JavaScript's `Date.getDay` convention), Saturday = 6. */
const DAYS_IN_WEEK = 7;

/** A new `Date` at local midnight of `value`, so arithmetic never drifts on the time of day. */
const atLocalMidnight = (value: Date): Date =>
  new Date(value.getFullYear(), value.getMonth(), value.getDate());

/** The Sunday on or before `value` (its own day if it is a Sunday), at local midnight. */
export const startOfWeek = (value: Date): Date => {
  const start = atLocalMidnight(value);
  start.setDate(start.getDate() - start.getDay());
  return start;
};

/** The Saturday on or after `value` (its own day if it is a Saturday), at local midnight. */
export const endOfWeek = (value: Date): Date => {
  const end = startOfWeek(value);
  end.setDate(end.getDate() + (DAYS_IN_WEEK - 1));
  return end;
};

/**
 * The inclusive range for a preset, computed from `today` (local time). `day` is the single date,
 * both bounds equal; `week` is the Sun..Sat window containing `today`; `month` is the first to the
 * last day of `today`'s calendar month. `custom` has no canonical range — the caller supplies the
 * two inputs — so this returns the current month as a sensible starting window for it.
 */
export const rangeForPreset = (preset: HoursPreset, today: Date, day: Date = today): DateRange => {
  switch (preset) {
    case 'day': {
      const at = toCanonicalDate(atLocalMidnight(day));
      return { from: at, to: at };
    }
    case 'week':
      return { from: toCanonicalDate(startOfWeek(today)), to: toCanonicalDate(endOfWeek(today)) };
    case 'month':
    case 'custom':
    default: {
      const first = new Date(today.getFullYear(), today.getMonth(), 1);
      const last = new Date(today.getFullYear(), today.getMonth() + 1, 0);
      return { from: toCanonicalDate(first), to: toCanonicalDate(last) };
    }
  }
};

/** One day of work as the page reads it — the same shape the API returns. */
export interface HoursDay {
  work_date: string;
  total_minutes: number;
}

/** A Sun..Sat week of days with its own subtotal, for the month view's grouped rendering. */
export interface WeekGroup {
  /** The week's Sunday, canonical `YYYY-MM-DD`, both the group key and the "Week of" label date. */
  weekStart: string;
  /** Whole minutes worked across every day in the group. */
  subtotalMinutes: number;
  /** The days in the group, in the order they arrived (the API's newest-first). */
  days: HoursDay[];
}

/**
 * The sum of every day's minutes — the range total shown prominently at the top of the screen.
 * Pure over the list, so an empty list is zero.
 */
export const totalMinutes = (days: HoursDay[]): number =>
  days.reduce((sum, day) => sum + day.total_minutes, 0);

/**
 * Group days into Sun..Sat weeks with per-week subtotals, for the month view (Requirement 11.2's
 * daily totals rolled up a level). A day is placed in the week of the Sunday on or before its date;
 * the group's `weekStart` is that Sunday. Input order is preserved within each group, and the groups
 * come out ordered by `weekStart` descending (newest week first), matching the newest-first day list
 * the API returns so the screen reads top-to-bottom from the most recent week.
 *
 * Pure and total: an empty list yields no groups; a `work_date` is read as a local date so its week
 * is the employee's calendar week, consistent with the preset math above.
 */
export const groupByWeek = (days: HoursDay[]): WeekGroup[] => {
  const byWeek = new Map<string, WeekGroup>();
  for (const day of days) {
    const weekStart = toCanonicalDate(startOfWeek(new Date(`${day.work_date}T00:00:00`)));
    const group = byWeek.get(weekStart);
    if (group) {
      group.days.push(day);
      group.subtotalMinutes += day.total_minutes;
    } else {
      byWeek.set(weekStart, { weekStart, subtotalMinutes: day.total_minutes, days: [day] });
    }
  }
  return [...byWeek.values()].sort((a, b) => b.weekStart.localeCompare(a.weekStart));
};
