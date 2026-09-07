/**
 * The pure shaping behind the manager hours view (Requirement 18.1).
 *
 * The API returns a flat, stable-sorted page of time entries — ordered by employee, then work date,
 * then check-in time. This module folds that flat list into the structure the view renders: a
 * chronological per-employee day, its entries grouped by site with a per-site subtotal, and a total
 * for the day (Requirement 11.2: the daily total is the sum of the per-site entries, never a single
 * site's). Keeping the shaping pure means it is tested against fixed inputs here rather than through
 * the rendered table, and the component stays a thin presentation over the result.
 *
 * Two rules the requirement pins live here.
 *
 * **A day's total is the sum of its entries' minutes, across sites.** An open shift contributes no
 * minutes (its `total_minutes` is null until check-out), so a day with an open shift still totals the
 * completed work and marks that the day is not yet closed. Attributing the whole day to one site — the
 * bug Requirement 11.2 forbids — is impossible here because the total is only ever a sum of entries.
 *
 * **The order the API returned is preserved.** The list arrives already ordered, so the groups come
 * out in that order and the entries within a site-group keep their chronological sequence; nothing is
 * re-sorted, which is what lets the flat page and the grouped view agree.
 */

import type { TimeEntryListItem } from '@/api/types';

/** One site's slice of an employee's day: the entries at that site and their summed minutes. */
export interface SiteGroup {
  siteId: string;
  siteName: string;
  entries: TimeEntryListItem[];
  /** Minutes worked at this site that day, summed over completed entries (open entries add none). */
  subtotalMinutes: number;
}

/** One employee's day: the site groups in order, the day total, and whether any shift is still open. */
export interface EmployeeDay {
  key: string;
  employeeId: string;
  employeeName: string;
  employeeNameEn: string;
  employeeNumber: string | null;
  workDate: string;
  siteGroups: SiteGroup[];
  /** The day's total minutes across every site (Requirement 11.2). */
  totalMinutes: number;
  /** How many sites the day spans, so the view can note a multi-site day. */
  siteCount: number;
  /** True when at least one entry that day has no check-out yet. */
  hasOpenShift: boolean;
  /** True when at least one entry that day is manual (Requirement 12.4). */
  hasManual: boolean;
  /** True when at least one entry that day carries an anomaly flag (Requirement 7.3, 10.5). */
  hasAnomaly: boolean;
}

/** The minutes an entry contributes: its total, or zero while the shift is still open. */
const minutesOf = (entry: TimeEntryListItem): number => entry.total_minutes ?? 0;

/**
 * Group a flat, ordered page of entries into per-employee days with per-site subtotals and daily
 * totals (Requirement 18.1). The input is assumed to be in the API's stable order (employee, work
 * date, check-in); the grouping preserves it, so the first time an (employee, date) pair or a site
 * within it is seen fixes its position. Entries that share an (employee, date) but sit apart in the
 * list — which the API's order does not produce, but a caller could — are still folded into the same
 * day rather than splitting it.
 */
export const groupEntries = (entries: TimeEntryListItem[]): EmployeeDay[] => {
  const days = new Map<string, EmployeeDay>();

  for (const entry of entries) {
    const dayKey = `${entry.employee_id}::${entry.work_date}`;
    let day = days.get(dayKey);
    if (day === undefined) {
      day = {
        key: dayKey,
        employeeId: entry.employee_id,
        employeeName: entry.employee_name,
        employeeNameEn: entry.employee_name_en,
        employeeNumber: entry.employee_number,
        workDate: entry.work_date,
        siteGroups: [],
        totalMinutes: 0,
        siteCount: 0,
        hasOpenShift: false,
        hasManual: false,
        hasAnomaly: false,
      };
      days.set(dayKey, day);
    }

    let group = day.siteGroups.find((candidate) => candidate.siteId === entry.site_id);
    if (group === undefined) {
      group = { siteId: entry.site_id, siteName: entry.site_name, entries: [], subtotalMinutes: 0 };
      day.siteGroups.push(group);
    }
    group.entries.push(entry);
    group.subtotalMinutes += minutesOf(entry);

    day.totalMinutes += minutesOf(entry);
    day.siteCount = day.siteGroups.length;
    if (entry.check_out_at === null) {
      day.hasOpenShift = true;
    }
    if (entry.is_manual) {
      day.hasManual = true;
    }
    if (entry.flags.length > 0) {
      day.hasAnomaly = true;
    }
  }

  return Array.from(days.values());
};
