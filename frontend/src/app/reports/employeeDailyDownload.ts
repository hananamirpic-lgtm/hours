/**
 * Pure generation for the employee-daily report's browser download (Requirement 6.1).
 *
 * Like the by-employee report, the employee-daily view lets a reader take the on-screen table away as
 * a CSV generated entirely in the browser: no export endpoint, no object storage, no new dependency.
 * This module holds the parts that are pure functions of the report so they can be tested without a
 * DOM: the human-readable cell values, the header, and the CSV string. The component wires these to a
 * real download.
 *
 * This report carries only hours — no wage, cost, billing or profit — so unlike the by-employee
 * helper there is no cost column and no redaction rule to enforce: every field is safe for every
 * console role. Three columns of minutes render as `h:mm` through `formatDuration`, matching the
 * table, so a downloaded file reads like what was on screen. There is no totals row: each row already
 * carries its own total, and the report has no per-bucket columns to sum.
 *
 * The CSV string begins with a UTF-8 byte-order mark so Excel opens Hebrew names correctly; fields are
 * quoted and embedded quotes doubled per RFC 4180, so a name containing a comma or a quote survives
 * the round trip. (The BOM is content of the generated string, not the encoding of this source file.)
 */

import type { EmployeeDailyReport, EmployeeDailyRow } from '@/api/types';
import type { Language } from '@/i18n';
import { formatDuration } from '@/lib/format';

/** A UTF-8 byte-order mark, prefixed to the CSV so Excel reads Hebrew as UTF-8 rather than ANSI. */
const BOM = '\uFEFF';

/** The column labels the caller supplies, already translated. Hours only — no cost label. */
export interface EmployeeDailyLabels {
  employee: string;
  total: string;
  approved: string;
  notApproved: string;
}

export interface EmployeeDailyRenderOptions {
  language: Language;
  labels: EmployeeDailyLabels;
}

/**
 * The reader's-language employee name, the other form in parentheses to disambiguate a shared name.
 * Unlike the by-employee row, `employee_name_en` may be null; when it is (or when it equals the
 * Hebrew name) the name is shown alone, never with an empty parenthetical.
 */
export const employeeDailyName = (
  language: Language,
  row: Pick<EmployeeDailyRow, 'employee_name' | 'employee_name_en'>,
): string => {
  const english = row.employee_name_en ?? row.employee_name;
  const primary = language === 'he' ? row.employee_name : english;
  const secondary = language === 'he' ? english : row.employee_name;
  return primary === secondary ? primary : `${primary} (${secondary})`;
};

/** The header cells, in table order. */
export const employeeDailyHeader = (options: EmployeeDailyRenderOptions): string[] => {
  const { labels } = options;
  return [labels.employee, labels.total, labels.approved, labels.notApproved];
};

/** One employee's cells, in table order: name, then total / approved / not-approved as `h:mm`. */
export const employeeDailyRowCells = (
  row: EmployeeDailyRow,
  options: EmployeeDailyRenderOptions,
): string[] => {
  const { language } = options;
  return [
    employeeDailyName(language, row),
    formatDuration(row.total_minutes),
    formatDuration(row.approved_minutes),
    formatDuration(row.not_approved_minutes),
  ];
};

/** Quote a CSV field when it holds a comma, quote or newline, doubling any embedded quote (RFC 4180). */
const escapeCsvField = (value: string): string =>
  /[",\r\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;

const toCsvLine = (cells: string[]): string => cells.map(escapeCsvField).join(',');

/**
 * The report as a CSV string: a header row and one row per employee — the same columns and values as
 * the on-screen table. Begins with a UTF-8 BOM so Excel reads Hebrew. No totals row: each row already
 * carries its own total and the report has no per-bucket columns to sum.
 */
export const toEmployeeDailyCsv = (
  report: EmployeeDailyReport,
  options: EmployeeDailyRenderOptions,
): string => {
  const lines = [
    toCsvLine(employeeDailyHeader(options)),
    ...report.rows.map((row) => toCsvLine(employeeDailyRowCells(row, options))),
  ];
  return `${BOM}${lines.join('\r\n')}`;
};

/** The download filename for a period, e.g. `employee_daily_2025_08.csv`. */
export const employeeDailyFilename = (report: EmployeeDailyReport): string =>
  `employee_daily_${report.year}_${String(report.month).padStart(2, '0')}.csv`;
