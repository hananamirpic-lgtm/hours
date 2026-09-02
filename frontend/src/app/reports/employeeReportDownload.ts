/**
 * Pure generation for the by-employee report's browser downloads (Requirement 18.4, 18.7).
 *
 * The by-employee view lets a reader take the on-screen table away as a file — a CSV for a
 * spreadsheet, a PDF for print — generated entirely in the browser: no export endpoint, no object
 * storage, and no new dependency. This module holds the parts that are pure functions of the report
 * so they can be tested without a DOM: the column model, the human-readable cell values, and the CSV
 * string. The component wires these to a real download and to `window.print`.
 *
 * The one security-relevant rule lives here too. `cost` and `total_cost` are a decimal string for a
 * finance reader and `null` for a site manager, whose payload the server strips of wage. When the
 * cost is absent the caller passes `includeCost: false`, and neither the column header nor any cell —
 * including the totals row — carries a cost. The manager view is wage-free by construction: there is
 * no code path that formats a null cost as a zero or an empty cell.
 *
 * Values are human-readable, matching the table: minutes render as `h:mm` through `formatDuration`,
 * money through `formatCurrency`, so a downloaded file reads like what was on screen. The CSV string
 * begins with a UTF-8 byte-order mark so Excel opens Hebrew names correctly; fields are quoted and
 * embedded quotes doubled per RFC 4180, so a name containing a comma or a quote survives the round
 * trip. (The BOM is content of the generated string, not the encoding of this source file.)
 */

import type { EmployeeReport, EmployeeReportRow } from '@/api/types';
import type { Language } from '@/i18n';
import { formatCurrency, formatDuration } from '@/lib/format';
import { parseAmount } from '@/app/dashboard/dashboardView';

/** A UTF-8 byte-order mark, prefixed to the CSV so Excel reads Hebrew as UTF-8 rather than ANSI. */
const BOM = '\uFEFF';

/** The column labels the caller supplies, already translated. The cost label is used only when shown. */
export interface EmployeeReportLabels {
  employee: string;
  regular: string;
  overtime: string;
  shabbat: string;
  holiday: string;
  total: string;
  cost: string;
  totalsRow: string;
}

export interface EmployeeReportRenderOptions {
  /** Whether the cost column is present — false for a site manager, whose payload carries no wage. */
  includeCost: boolean;
  language: Language;
  labels: EmployeeReportLabels;
}

/** The reader's-language employee name, the other form in parentheses to disambiguate a shared name. */
export const employeeReportName = (
  language: Language,
  row: Pick<EmployeeReportRow, 'employee_name' | 'employee_name_en'>,
): string => {
  const primary = language === 'he' ? row.employee_name : row.employee_name_en;
  const secondary = language === 'he' ? row.employee_name_en : row.employee_name;
  return primary === secondary ? primary : `${primary} (${secondary})`;
};

/** The header cells, in table order; the cost column is appended only when shown. */
export const employeeReportHeader = (options: EmployeeReportRenderOptions): string[] => {
  const { labels, includeCost } = options;
  const header = [
    labels.employee,
    labels.regular,
    labels.overtime,
    labels.shabbat,
    labels.holiday,
    labels.total,
  ];
  if (includeCost) {
    header.push(labels.cost);
  }
  return header;
};

/** One employee's cells, in table order; the cost cell is appended only when shown. */
export const employeeReportRowCells = (
  row: EmployeeReportRow,
  options: EmployeeReportRenderOptions,
): string[] => {
  const { language, includeCost } = options;
  const cells = [
    employeeReportName(language, row),
    formatDuration(row.regular_minutes),
    formatDuration(row.overtime_minutes),
    formatDuration(row.shabbat_minutes),
    formatDuration(row.holiday_minutes),
    formatDuration(row.total_minutes),
  ];
  if (includeCost) {
    cells.push(formatCurrency(language, parseAmount(row.cost)));
  }
  return cells;
};

/**
 * The totals row: the totals label under the employee column, an empty cell under each per-bucket
 * column, the summed minutes under Total, and — only when cost is shown — the summed cost. The
 * per-bucket totals are left blank because the report totals hours, not each bucket.
 */
export const employeeReportTotalsCells = (
  report: EmployeeReport,
  options: EmployeeReportRenderOptions,
): string[] => {
  const { language, includeCost, labels } = options;
  const cells = [
    labels.totalsRow,
    '',
    '',
    '',
    '',
    formatDuration(report.total_minutes),
  ];
  if (includeCost) {
    cells.push(formatCurrency(language, parseAmount(report.total_cost)));
  }
  return cells;
};

/** Quote a CSV field when it holds a comma, quote or newline, doubling any embedded quote (RFC 4180). */
const escapeCsvField = (value: string): string =>
  /[",\r\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;

const toCsvLine = (cells: string[]): string => cells.map(escapeCsvField).join(',');

/**
 * The report as a CSV string: a header row, one row per employee, then a totals row — the same
 * columns and values as the on-screen table. Begins with a UTF-8 BOM so Excel reads Hebrew. The cost
 * column appears only when `includeCost`, so a site manager's file is wage-free.
 */
export const toEmployeeReportCsv = (
  report: EmployeeReport,
  options: EmployeeReportRenderOptions,
): string => {
  const lines = [
    toCsvLine(employeeReportHeader(options)),
    ...report.rows.map((row) => toCsvLine(employeeReportRowCells(row, options))),
    toCsvLine(employeeReportTotalsCells(report, options)),
  ];
  return `${BOM}${lines.join('\r\n')}`;
};

/** The download filename for a period, e.g. `employees_2025_08.csv`. */
export const employeeReportFilename = (report: EmployeeReport, extension: string): string =>
  `employees_${report.year}_${String(report.month).padStart(2, '0')}.${extension}`;
