// Pure logic of the management report screen (task 24.6; R18.1, R18.7, R18.11, R18.12, R23.12).
// No React, no DOM, no i18n instance: the page turns the keys and values below into text.
//
// The list helpers (`pageInfo`, `clampPage`, `categoryName`, the status keys) come from `ticketListModel`, so
// the report table pages, names and badges the same way the ticket list does.
import type {
  ReportQuery,
  StaffCategoryCount,
  StaffReport,
  StaffStatusCounts,
  TicketStatus,
} from '../api/staffTypes';
import { STATUS_ORDER, categoryName, type ListLanguage } from './ticketListModel';

// --- filters and the request (R18.1) ---------------------------------------------------------------

/** The values of the filter controls. Dates are `YYYY-MM-DD`; blank means no restriction. */
export interface ReportFilters {
  readonly dateFrom: string;
  readonly dateTo: string;
  readonly city: string;
  /** An id from `GET /staff/categories` (active or inactive); blank means every category. */
  readonly categoryId: string;
}

export const EMPTY_REPORT_FILTERS: ReportFilters = { dateFrom: '', dateTo: '', city: '', categoryId: '' };

/** What the report is showing: the filters last submitted and the table page. */
export interface AppliedReport {
  readonly filters: ReportFilters;
  readonly page: number;
}

export const INITIAL_REPORT: AppliedReport = { filters: EMPTY_REPORT_FILTERS, page: 1 };

type Mutable<T> = { -readonly [K in keyof T]: T[K] };

/** The `GET /staff/reports` query. A blank filter is left out, so it restricts nothing (R18.1). */
export function toReportQuery(applied: AppliedReport): ReportQuery {
  const { filters } = applied;
  const query: Mutable<ReportQuery> = { page: applied.page };
  if (filters.dateFrom !== '') query.dateFrom = filters.dateFrom;
  if (filters.dateTo !== '') query.dateTo = filters.dateTo;
  if (filters.city.trim() !== '') query.city = filters.city.trim();
  if (filters.categoryId !== '') query.categoryId = filters.categoryId;
  return query;
}

/** True when two applied states send the same request, so a re-submit must refetch instead of changing the key. */
export function sameReportRequest(a: AppliedReport, b: AppliedReport): boolean {
  return JSON.stringify(toReportQuery(a)) === JSON.stringify(toReportQuery(b));
}

/** True when any filter has a value. */
export function hasActiveReportFilters(filters: ReportFilters): boolean {
  return (
    filters.dateFrom !== '' || filters.dateTo !== '' || filters.city.trim() !== '' || filters.categoryId !== ''
  );
}

// --- errors (R18.12) -------------------------------------------------------------------------------

/** The codes of a report the server refuses for its filters or page (R18.4, R18.6). */
export const REPORT_REJECTION_CODES: readonly string[] = ['invalid_date_range', 'invalid_filter', 'invalid_page'];

/** The translation key of the message for a failed report request: the specific one, else the general one. */
export function reportErrorKey(code: string): string {
  return REPORT_REJECTION_CODES.includes(code) ? `errors.${code}` : 'management.reports.loadError';
}

/** The control group a report error belongs to, so the page can associate the message with it. */
export type ReportErrorField = 'dates' | 'filters';

export function reportErrorField(code: string): ReportErrorField | null {
  if (code === 'invalid_date_range') return 'dates';
  if (code === 'invalid_filter') return 'filters';
  return null;
}

/**
 * The report to show. A failed request shows no figures: they would describe other filters than the ones in the
 * form, and the error message and the entered filters are what R18.12 requires.
 */
export function shownReport(report: StaffReport | undefined, failed: boolean): StaffReport | null {
  return failed || report === undefined ? null : report;
}

// --- figures (R18.7, R18.9, R18.11) ----------------------------------------------------------------

/** One status count of the report. */
export interface StatusCountEntry {
  readonly status: TicketStatus;
  readonly count: number;
}

/** A count read from the answer: a missing, negative or non-integer value reads as 0, never as blank. */
function safeCount(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

/** All four statuses in lifecycle order, each with its count, 0 included (R18.7, R18.11). */
export function statusCountEntries(byStatus: Partial<StaffStatusCounts> | undefined): readonly StatusCountEntry[] {
  return STATUS_ORDER.map((status) => ({ status, count: safeCount(byStatus?.[status]) }));
}

/** One category line of the report. */
export interface CategoryCountEntry {
  readonly id: string;
  readonly name: string;
  readonly count: number;
}

/**
 * The per-category counts to show: every category with at least one matching ticket, active or inactive, in the
 * order the server gave (R18.7). A category with a count of 0 is left out.
 */
export function categoryCountEntries(
  byCategory: readonly StaffCategoryCount[] | undefined,
  language: ListLanguage,
): readonly CategoryCountEntry[] {
  return (byCategory ?? [])
    .map((item) => ({ id: item.category.id, name: categoryName(item.category, language), count: safeCount(item.count) }))
    .filter((entry) => entry.count > 0);
}

/** True when no ticket matched: the table is replaced by the no-match message (R18.11). */
export function isEmptyReport(report: Pick<StaffReport, 'total'>): boolean {
  return report.total === 0;
}
