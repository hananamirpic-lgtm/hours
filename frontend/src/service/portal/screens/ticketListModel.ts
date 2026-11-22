// View model of the customer ticket list (R12.1, R12.5, R12.8, R12.9, R12.10, R23.5, R23.15).
// Pure: no React, no DOM, no i18n instance. The screen turns the keys and values below into text.

import { keepPreviousData } from '@tanstack/react-query';
import { isPortalApiError } from '../../api/portalFetch';
import type { TicketCategory, TicketPage, TicketRow } from '../../api/ticketTypes';
import type { TicketStatus, TicketStatusEntry } from '../../types';
import { diffStatuses, type StatusAnnouncement } from '../announcements';
import type { PortalLanguage } from '../portalI18n';

// --- polling ---------------------------------------------------------------------------------------

/** The list reloads this long after the previous load completes (R12.5). */
export const POLL_INTERVAL_MS = 30_000;

/**
 * Query options of the list (design "Polling"). `keepPreviousData` keeps the rows of the page on screen
 * while another page loads, so there is no loading flash and no remount.
 */
export const TICKET_LIST_QUERY_OPTIONS = {
  refetchInterval: POLL_INTERVAL_MS,
  refetchIntervalInBackground: false,
  refetchOnWindowFocus: 'always',
  placeholderData: keepPreviousData,
} as const;

/**
 * The interval to hand to the query for a given number of completed loads.
 *
 * TanStack Query 5 starts `refetchInterval` as a fixed `setInterval` and restarts it only when the
 * interval value changes. R12.5 and R12.10 want the next reload 30 seconds after the previous load
 * COMPLETED, also after a manual refresh, a retry and a focus reload. Alternating the value by one
 * millisecond after every completed load restarts the timer at that moment and changes nothing else.
 */
export function pollInterval(completedLoads: number): number {
  return POLL_INTERVAL_MS + (Math.abs(completedLoads) % 2);
}

// --- page number in the URL (R12.1, R12.9) ---------------------------------------------------------

export const PAGE_SIZE = 50;

export interface ParsedPage {
  /** The page to load: the parameter when it is valid, otherwise 1. */
  readonly page: number;
  /** False when the parameter is present but is not a positive integer, so the screen says it used page 1. */
  readonly valid: boolean;
}

const POSITIVE_INTEGER = /^[1-9][0-9]*$/;

/**
 * The page from the `page` search parameter. A missing parameter is page 1 and valid. Any present value
 * that is not a plain positive integer in decimal (`''`, `'0'`, `'-1'`, `'x'`, `'2.5'`, `'01'`, `' 2'`,
 * `'1e2'`, a number above the safe integer range) is page 1 and not valid.
 */
export function parsePageParam(raw: string | null | undefined): ParsedPage {
  if (raw === null || raw === undefined) return { page: 1, valid: true };
  if (!POSITIVE_INTEGER.test(raw)) return { page: 1, valid: false };
  const page = Number(raw);
  if (!Number.isSafeInteger(page)) return { page: 1, valid: false };
  return { page, valid: true };
}

/** The search string of a page: nothing for page 1 so the first page has the plain address. */
export function pageSearch(page: number): string {
  return page <= 1 ? '' : `?page=${page}`;
}

/** The path (relative to the `/service` basename) of one ticket's detail. */
export function ticketPath(ticketNumber: number): string {
  return `/tickets/${ticketNumber}`;
}

// --- row fields (R12.1, R12.6) ---------------------------------------------------------------------

const ISO_DATE = /^(\d{4})-(\d{1,2})-(\d{1,2})$/;

/**
 * `DD/MM/YYYY` from the server's `YYYY-MM-DD`, by rearranging the digits. No `Date` parsing and no time
 * zone, so the day never shifts. A value that is not a real calendar date is returned unchanged.
 */
export function formatCreatedDate(isoDate: string): string {
  const match = ISO_DATE.exec(isoDate);
  if (match === null) return isoDate;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) return isoDate;
  return `${String(day).padStart(2, '0')}/${String(month).padStart(2, '0')}/${match[1]}`;
}

const MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31] as const;

function isLeapYear(year: number): boolean {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

function daysInMonth(year: number, month: number): number {
  return month === 2 && isLeapYear(year) ? 29 : (MONTH_DAYS[month - 1] ?? 0);
}

/** The category name in the current language; the other language when that name is empty. */
export function categoryName(category: Pick<TicketCategory, 'name_he' | 'name_en'>, language: PortalLanguage): string {
  const [first, second] =
    language === 'he' ? [category.name_he, category.name_en] : [category.name_en, category.name_he];
  return first !== '' ? first : second;
}

export type StatusTextKey = `status.${TicketStatus}`;

/** The locale key of a status's text (R12.6). The status is always printed, never shown by colour alone. */
export function statusTextKey(status: TicketStatus): StatusTextKey {
  return `status.${status}`;
}

export const URGENT_MARKER_KEY = 'portal.urgent.marker';

/** The locale key of the urgent marker text when the ticket is urgent, in any status; null otherwise. */
export function urgentMarkerKey(row: Pick<TicketRow, 'urgent'>): typeof URGENT_MARKER_KEY | null {
  return row.urgent ? URGENT_MARKER_KEY : null;
}

// --- pagination (R12.1) ----------------------------------------------------------------------------

export interface PaginationState {
  /** The page being shown. */
  readonly page: number;
  /** The number of pages: at least 1, also when the customer owns no ticket. */
  readonly pages: number;
  readonly total: number;
  readonly hasPrevious: boolean;
  readonly hasNext: boolean;
  readonly previousPage: number | null;
  readonly nextPage: number | null;
  /** Page controls are shown whenever the customer owns more than 50 tickets. */
  readonly showControls: boolean;
}

/** The pagination state for the shown `page` and the `total` of tickets the customer owns. */
export function paginationState(page: number, total: number): PaginationState {
  const safeTotal = Number.isFinite(total) && total > 0 ? Math.floor(total) : 0;
  const pages = Math.max(1, Math.ceil(safeTotal / PAGE_SIZE));
  const hasPrevious = page > 1;
  const hasNext = page < pages;
  return {
    page,
    pages,
    total: safeTotal,
    hasPrevious,
    hasNext,
    previousPage: hasPrevious ? Math.min(page - 1, pages) : null,
    nextPage: hasNext ? page + 1 : null,
    showControls: safeTotal > PAGE_SIZE,
  };
}

/**
 * Where to go when the shown page is past the last one (an address typed by hand): the last page, or
 * page 1 when the customer owns no ticket. Null while the page exists.
 */
export function pastTheEndPage(page: number, total: number): number | null {
  const { pages } = paginationState(page, total);
  return page > pages ? pages : null;
}

/**
 * True when a failed list request means "this page is not available" and the screen should show page 1:
 * 422 `invalid_page` or any 404, while a later page was asked for. On page 1 there is no page to fall
 * back to, so the error panel shows instead.
 */
export function shouldFallBackToFirstPage(error: unknown, page: number): boolean {
  if (page <= 1 || !isPortalApiError(error)) return false;
  return error.status === 404 || (error.status === 422 && error.code === 'invalid_page');
}

// --- what the screen shows (R12.7, R12.8) ----------------------------------------------------------

/**
 * `loading`  nothing loaded yet and no failure: the loading text.
 * `none`     nothing loaded and the request failed: only the error panel.
 * `rows`     the table.
 * `empty`    the customer owns no ticket: the empty state.
 */
export type ListContent = 'loading' | 'none' | 'rows' | 'empty';

export interface ListViewState {
  readonly content: ListContent;
  readonly rows: readonly TicketRow[];
  /** The error panel with the retry control. It sits beside the rows and never replaces them. */
  readonly showError: boolean;
}

/**
 * Rows already loaded stay on screen through a failed reload (R12.8), so a failure only adds the error
 * panel. The panel is gone as soon as a later request succeeds (`isError` is false again).
 */
export function listViewState(input: { data: TicketPage | undefined; isError: boolean }): ListViewState {
  const { data, isError } = input;
  if (data === undefined) {
    return { content: isError ? 'none' : 'loading', rows: [], showError: isError };
  }
  if (data.items.length > 0) return { content: 'rows', rows: data.items, showError: isError };
  // No rows on a page past the end is not "no tickets": the screen is about to move to the last page.
  return { content: data.total > 0 ? 'loading' : 'empty', rows: [], showError: isError };
}

// --- status-change announcements (R23.15, design Property 34) --------------------------------------

/** The statuses of one page as the last load showed them. */
export interface StatusSnapshot {
  readonly page: number;
  readonly entries: readonly TicketStatusEntry[];
}

export function toStatusEntries(rows: readonly TicketRow[]): TicketStatusEntry[] {
  return rows.map((row) => ({ ticketNumber: row.ticket_number, status: row.status }));
}

/**
 * Compares a load with the snapshot before it. Returns what to announce and the snapshot to keep.
 *
 *  - No snapshot (first load) or a snapshot of another page (the customer changed page): nothing is
 *    announced and the new load becomes the snapshot.
 *  - Same page: one announcement per ticket whose status differs (`diffStatuses`), none when the data
 *    is identical.
 */
export function pollAnnouncements(
  previous: StatusSnapshot | null,
  page: number,
  rows: readonly TicketRow[],
): { announcements: StatusAnnouncement[]; snapshot: StatusSnapshot } {
  const entries = toStatusEntries(rows);
  const snapshot: StatusSnapshot = { page, entries };
  if (previous === null || previous.page !== page) return { announcements: [], snapshot };
  return { announcements: diffStatuses(previous.entries, entries), snapshot };
}

/** A translator: `t(key, values)`. */
export type Translate = (key: string, values?: Record<string, string | number>) => string;

/** One live-region text per changed status: `a11y.live.statusChanged` with the number and the status text. */
export function announcementTexts(announcements: readonly StatusAnnouncement[], translate: Translate): string[] {
  return announcements.map((announcement) =>
    translate('a11y.live.statusChanged', {
      number: announcement.ticketNumber,
      status: translate(statusTextKey(announcement.status)),
    }),
  );
}

/** Pause between two messages, long enough for a screen reader to finish one before the next replaces it. */
export const ANNOUNCEMENT_GAP_MS = 2_000;

export interface ScheduledAnnouncement {
  readonly text: string;
  readonly delayMs: number;
}

/**
 * The live region holds one message at a time (each write replaces the last), so several changed
 * statuses are written one after the other, in ticket-number order, `gapMs` apart.
 */
export function scheduleAnnouncements(
  texts: readonly string[],
  gapMs: number = ANNOUNCEMENT_GAP_MS,
): ScheduledAnnouncement[] {
  return texts.map((text, index) => ({ text, delayMs: index * gapMs }));
}
