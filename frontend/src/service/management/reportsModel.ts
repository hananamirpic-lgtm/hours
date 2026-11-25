// Pure logic of the management report screen (task 24.6; R18.1, R18.7, R18.11, R18.12, R23.12).
// No React, no DOM, no i18n instance: the page turns the keys and values below into text.
//
// The list helpers (`pageInfo`, `clampPage`, `categoryName`, the status keys) come from `ticketListModel`, so
// the report table pages, names and badges the same way the ticket list does.
import type { ReportExportQueryExt, ReportQueryExt } from '../api/staffReportsExtra';
import type {
  StaffCategoryCount,
  StaffReport,
  StaffReportRow,
  StaffStatusCounts,
  TicketStatus,
} from '../api/staffTypes';
import { STATUS_ORDER, categoryName, type ListLanguage } from './ticketListModel';

// --- filters and the request (R18.1, R29.8, R30.7) -------------------------------------------------

/** The "Part replaced" values that are not a part: no restriction (the default), a part was replaced, none was. */
export const PART_FILTER_ANY = 'any';
export const PART_FILTER_YES = 'yes';
export const PART_FILTER_NO = 'no';

/** The values of the filter controls. Dates are `YYYY-MM-DD`; blank means no restriction. */
export interface ReportFilters {
  readonly dateFrom: string;
  readonly dateTo: string;
  /** The free-text settlement filter, used while no settlement list exists. */
  readonly city: string;
  /** An id from `GET /staff/categories` (active or inactive); blank means every category. */
  readonly categoryId: string;
  /** The id of one settlement, used while the settlement list is not empty; blank means every settlement. */
  readonly localityId: string;
  /** `any` (default), `yes`, `no` or the id of one part. */
  readonly partFilter: string;
}

export const EMPTY_REPORT_FILTERS: ReportFilters = {
  dateFrom: '',
  dateTo: '',
  city: '',
  categoryId: '',
  localityId: '',
  partFilter: PART_FILTER_ANY,
};

/**
 * The filters that apply for the current settlement list. While the list is non-empty only the settlement
 * select is on screen, so a city text entered earlier is dropped; while it is empty only the text input is, so
 * a settlement id is dropped. A filter that is not on screen never restricts the report invisibly.
 */
export function effectiveReportFilters(filters: ReportFilters, settlementList: boolean): ReportFilters {
  return settlementList ? { ...filters, city: '' } : { ...filters, localityId: '' };
}

/** What the report is showing: the filters last submitted and the table page. */
export interface AppliedReport {
  readonly filters: ReportFilters;
  readonly page: number;
}

export const INITIAL_REPORT: AppliedReport = { filters: EMPTY_REPORT_FILTERS, page: 1 };

type Mutable<T> = { -readonly [K in keyof T]: T[K] };

/** The filters of a request, without the page: a blank filter, and the part filter `any`, are left out. */
function filterQuery(filters: ReportFilters): Mutable<ReportExportQueryExt> {
  const query: Mutable<ReportExportQueryExt> = {};
  if (filters.dateFrom !== '') query.dateFrom = filters.dateFrom;
  if (filters.dateTo !== '') query.dateTo = filters.dateTo;
  if (filters.city.trim() !== '') query.city = filters.city.trim();
  if (filters.localityId !== '') query.localityId = filters.localityId;
  if (filters.categoryId !== '') query.categoryId = filters.categoryId;
  if (filters.partFilter !== '' && filters.partFilter !== PART_FILTER_ANY) query.partFilter = filters.partFilter;
  return query;
}

/** The `GET /staff/reports` query. A blank filter is left out, so it restricts nothing (R18.1). */
export function toReportQuery(applied: AppliedReport): ReportQueryExt {
  return { page: applied.page, ...filterQuery(applied.filters) };
}

/**
 * The `GET /staff/reports/export` query: the filters APPLIED to the report on screen (R31.1), never the draft
 * in the controls, and no page, because the file holds every matching ticket.
 */
export function toExportQuery(applied: AppliedReport): ReportExportQueryExt {
  return filterQuery(applied.filters);
}

/** True when two applied states send the same request, so a re-submit must refetch instead of changing the key. */
export function sameReportRequest(a: AppliedReport, b: AppliedReport): boolean {
  return JSON.stringify(toReportQuery(a)) === JSON.stringify(toReportQuery(b));
}

/** True when any filter has a value. */
export function hasActiveReportFilters(filters: ReportFilters): boolean {
  return (
    filters.dateFrom !== '' ||
    filters.dateTo !== '' ||
    filters.city.trim() !== '' ||
    filters.categoryId !== '' ||
    filters.localityId !== '' ||
    (filters.partFilter !== '' && filters.partFilter !== PART_FILTER_ANY)
  );
}

// --- the part filter and the parts column (R29.7, R29.8) -------------------------------------------

/** One option of the "Part replaced" select: its value and how its text is made. */
export type PartFilterOption =
  | { readonly kind: 'any' | 'yes' | 'no'; readonly value: string }
  | { readonly kind: 'part'; readonly value: string; readonly name: string; readonly inactive: boolean };

/** Any, Yes, No, then one option per part in the order given, an inactive part marked as such. */
export function partFilterOptions(
  parts: readonly { id: string; name_he: string; name_en: string; is_active: boolean }[],
  language: ListLanguage,
): readonly PartFilterOption[] {
  return [
    { kind: 'any', value: PART_FILTER_ANY },
    { kind: 'yes', value: PART_FILTER_YES },
    { kind: 'no', value: PART_FILTER_NO },
    ...parts.map(
      (part): PartFilterOption => ({
        kind: 'part',
        value: part.id,
        name: categoryName(part, language),
        inactive: !part.is_active,
      }),
    ),
  ];
}

/** The text of an empty cell. */
export const EMPTY_CELL = '\u2014';

/** The parts column of a report row: the names in `language` joined by a comma, or the dash for none. */
export function partsCellText(row: Pick<StaffReportRow, 'parts'>, language: ListLanguage): string {
  const names = (row.parts ?? []).map((part) => categoryName(part, language)).filter((name) => name !== '');
  return names.length === 0 ? EMPTY_CELL : names.join(', ');
}

/** The settlement of a row: the localized name when the row has one, else the stored city text. */
export function settlementCellText(row: Pick<StaffReportRow, 'locality' | 'city'>, language: ListLanguage): string {
  return row.locality ? categoryName(row.locality, language) : row.city;
}

// --- the Excel export (R31.1, R31.4, R31.6) --------------------------------------------------------

export type ExportState =
  | { readonly kind: 'idle' }
  | { readonly kind: 'preparing' }
  | { readonly kind: 'done' }
  | { readonly kind: 'failed'; readonly code: string };

export const EXPORT_IDLE: ExportState = { kind: 'idle' };

/** The export button is usable once a report is on screen and while no file is being prepared. */
export function canExport(report: StaffReport | null, state: ExportState): boolean {
  return report !== null && state.kind !== 'preparing';
}

/** The codes of an export the server refuses for its filters or its size. */
export const EXPORT_REJECTION_CODES: readonly string[] = ['export_too_large', 'invalid_filter', 'invalid_date_range'];

/** The translation key of the message for a failed export: the specific one, else the general one. */
export function exportErrorKey(code: string): string {
  return EXPORT_REJECTION_CODES.includes(code) ? `errors.${code}` : 'management.reports.exportError';
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
