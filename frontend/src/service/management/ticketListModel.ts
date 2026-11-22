// Pure logic of the management ticket list (task 24.3; R15.1 to R15.12, R14.2, R23.12).
// No React, no DOM, no i18n instance: the page turns the keys and values below into text.
//
// Other management pages (the detail page, task 24.4) may import `allowedStatusTargets`, `statusActions` and
// `formatJerusalemMinute` from here, so the list and the detail offer the same moves and show the same times.
import type { StaffTicketPage, StaffTicketRow, TicketListQuery, TicketStatus } from '../api/staffTypes';

// --- paging ----------------------------------------------------------------------------------------

/** The Ticket_API answers 25 tickets per page (R15.1). */
export const PAGE_SIZE = 25;

export interface PageInfo {
  /** At least 1, also for an empty list. */
  readonly pages: number;
  /** 1-based number of the first ticket on the page; 0 for an empty list. */
  readonly first: number;
  /** 1-based number of the last ticket on the page; 0 for an empty list. */
  readonly last: number;
  readonly hasPrevious: boolean;
  readonly hasNext: boolean;
}

/** The range and the neighbours of `page` in a list of `total` tickets. */
export function pageInfo(page: number, total: number): PageInfo {
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (total <= 0) return { pages, first: 0, last: 0, hasPrevious: false, hasNext: false };
  return {
    pages,
    first: (page - 1) * PAGE_SIZE + 1,
    last: Math.min(total, page * PAGE_SIZE),
    hasPrevious: page > 1,
    hasNext: page < pages,
  };
}

/** The page to show when `page` lies past the end of a list of `total` tickets; `page` itself otherwise. */
export function clampPage(page: number, total: number): number {
  if (total <= 0) return 1;
  return Math.min(Math.max(1, page), pageInfo(1, total).pages);
}

// --- filters and the request -----------------------------------------------------------------------

/** The values of the search box and the filter controls. Dates are `YYYY-MM-DD`; blank means no filter. */
export interface TicketListFilters {
  readonly search: string;
  readonly statuses: readonly TicketStatus[];
  /** An id from `GET /staff/categories`; blank means every category. */
  readonly categoryId: string;
  readonly city: string;
  readonly urgentOnly: boolean;
  readonly dateFrom: string;
  readonly dateTo: string;
}

export const EMPTY_FILTERS: TicketListFilters = {
  search: '',
  statuses: [],
  categoryId: '',
  city: '',
  urgentOnly: false,
  dateFrom: '',
  dateTo: '',
};

/** The four statuses in lifecycle order. */
export const STATUS_ORDER: readonly TicketStatus[] = ['received', 'in_progress', 'closed', 'cancelled'];

/** Adds or removes `status`, keeping lifecycle order and no duplicates. */
export function toggleStatus(statuses: readonly TicketStatus[], status: TicketStatus): TicketStatus[] {
  const next = new Set(statuses);
  if (next.has(status)) next.delete(status);
  else next.add(status);
  return STATUS_ORDER.filter((candidate) => next.has(candidate));
}

/** True when any search or filter value is set (the urgent-first toggle is an order, not a filter). */
export function hasActiveFilters(filters: TicketListFilters): boolean {
  return (
    filters.search.trim() !== '' ||
    filters.statuses.length > 0 ||
    filters.categoryId !== '' ||
    filters.city.trim() !== '' ||
    filters.urgentOnly ||
    filters.dateFrom !== '' ||
    filters.dateTo !== ''
  );
}

/** What the list is showing: the filters last submitted, the order toggle and the page. */
export interface AppliedList {
  readonly filters: TicketListFilters;
  readonly urgentFirst: boolean;
  readonly page: number;
}

export const INITIAL_LIST: AppliedList = { filters: EMPTY_FILTERS, urgentFirst: false, page: 1 };

type Mutable<T> = { -readonly [K in keyof T]: T[K] };

/** The `GET /staff/tickets` query. Blank values are left out; the server trims and validates the search text. */
export function toListQuery(applied: AppliedList): TicketListQuery {
  const { filters } = applied;
  const query: Mutable<TicketListQuery> = { page: applied.page };
  if (filters.search.trim() !== '') query.search = filters.search;
  if (filters.statuses.length > 0) query.status = STATUS_ORDER.filter((s) => filters.statuses.includes(s));
  if (filters.categoryId !== '') query.categoryId = filters.categoryId;
  if (filters.city.trim() !== '') query.city = filters.city.trim();
  if (filters.urgentOnly) query.urgentOnly = true;
  if (filters.dateFrom !== '') query.dateFrom = filters.dateFrom;
  if (filters.dateTo !== '') query.dateTo = filters.dateTo;
  if (applied.urgentFirst) query.urgentFirst = true;
  return query;
}

/** True when two applied states send the same request, so a re-submit must refetch instead of changing the key. */
export function sameRequest(a: AppliedList, b: AppliedList): boolean {
  return JSON.stringify(toListQuery(a)) === JSON.stringify(toListQuery(b));
}

// --- list errors (R15.4, R15.6, R15.12) ------------------------------------------------------------

/** The codes of a rejected search, date range or filter. The previously displayed list stays (R15.4, R15.6). */
export const LIST_REJECTION_CODES: readonly string[] = [
  'invalid_ticket_number',
  'invalid_date_range',
  'invalid_filter',
  'invalid_page',
];

/** The translation key of the message for a failed list request: the specific one, else the general one. */
export function listErrorKey(code: string): string {
  return LIST_REJECTION_CODES.includes(code) ? `errors.${code}` : 'management.tickets.loadError';
}

/** The control a list error belongs to, so the page can associate the message with it. */
export type ListErrorField = 'search' | 'dates' | 'filters';

export function listErrorField(code: string): ListErrorField | null {
  if (code === 'invalid_ticket_number') return 'search';
  if (code === 'invalid_date_range') return 'dates';
  if (code === 'invalid_filter') return 'filters';
  return null;
}

/**
 * The rows to show: the loaded page when there is one, otherwise the last page that loaded. A failed request
 * therefore leaves the rows already displayed in place (R15.4, R15.6, R15.12).
 */
export function displayedPage(
  loaded: StaffTicketPage | undefined,
  lastLoaded: StaffTicketPage | null,
): StaffTicketPage | null {
  return loaded ?? lastLoaded;
}

// --- status lifecycle (R14.2, R15.9, R15.11) -------------------------------------------------------

/** The moves Ticket_Staff may make (Requirement 14). Cancelled is never a staff target and has no way out. */
const STAFF_TARGETS: Readonly<Record<TicketStatus, readonly TicketStatus[]>> = {
  received: ['in_progress'],
  in_progress: ['closed'],
  closed: ['in_progress'],
  cancelled: [],
};

/** The statuses Ticket_Staff may move a ticket to from `status`. */
export function allowedStatusTargets(status: TicketStatus): readonly TicketStatus[] {
  return STAFF_TARGETS[status];
}

/** One button of the status column: the status it sends and the `service` key of its label. */
export interface StatusAction {
  readonly target: TicketStatus;
  readonly labelKey: string;
}

/** The key of the label of the move `from` to `target`; null for a move that is not allowed. */
export function statusActionKey(from: TicketStatus, target: TicketStatus): string | null {
  if (!STAFF_TARGETS[from].includes(target)) return null;
  if (from === 'received') return 'management.tickets.actions.toInProgress';
  if (from === 'in_progress') return 'management.tickets.actions.close';
  return 'management.tickets.actions.reopen';
}

/** The buttons to offer for a ticket in `status`: one per allowed target, none for a cancelled ticket. */
export function statusActions(status: TicketStatus): readonly StatusAction[] {
  return STAFF_TARGETS[status].map((target) => ({
    target,
    labelKey: statusActionKey(status, target) ?? 'management.tickets.actions.none',
  }));
}

/** The translation key of a status name. */
export function statusTextKey(status: TicketStatus): string {
  return `status.${status}`;
}

/** An Open_Ticket is received or in progress. */
export function isOpenStatus(status: TicketStatus): boolean {
  return status === 'received' || status === 'in_progress';
}

/** True for a ticket that is urgent and still open: its row gets its own background (R15.7). */
export function isUrgentOpen(row: Pick<StaffTicketRow, 'urgent' | 'status'>): boolean {
  return row.urgent && isOpenStatus(row.status);
}

/** The class names of a row. Only an urgent open ticket gets the highlight class. */
export function rowClassName(row: Pick<StaffTicketRow, 'urgent' | 'status'>): string {
  return isUrgentOpen(row) ? 'ticket-list__row ticket-list__row--urgent-open' : 'ticket-list__row';
}

/** The modifier of the status badge. Colour is only a hint; the badge always carries the status name. */
export function statusBadgeClass(status: TicketStatus): string {
  const modifier: Readonly<Record<TicketStatus, string>> = {
    received: 'pill',
    in_progress: 'pill pill--warn',
    closed: 'pill pill--active',
    cancelled: 'pill pill--muted',
  };
  return modifier[status];
}

// --- a failed status change (R15.10) ---------------------------------------------------------------

/** The failed change a page shows an error for. */
export interface StatusFailure {
  readonly ticketNumber: number;
  readonly from: TicketStatus;
  readonly target: TicketStatus;
  readonly code: string;
}

/** Codes of a rejected change that have a message of their own. */
const STATUS_ERROR_CODES: readonly string[] = [
  'invalid_transition',
  'ticket_status_changed',
  'ticket_not_found',
  'invalid_expected_status',
  'invalid_target_status',
  'insufficient_role',
];

/** The key of the reason of a failed status change; the general error for a code with no message. */
export function statusFailureReasonKey(code: string): string {
  return STATUS_ERROR_CODES.includes(code) ? `errors.${code}` : 'errors.generic';
}

// --- rows ------------------------------------------------------------------------------------------

/** The row fields of a ticket (a `StaffTicketDetail` has more). */
export function toRow(ticket: StaffTicketRow): StaffTicketRow {
  return {
    ticket_number: ticket.ticket_number,
    created_at: ticket.created_at,
    customer_name: ticket.customer_name,
    city: ticket.city,
    apartment: ticket.apartment,
    category: ticket.category,
    status: ticket.status,
    assigned_technician_name: ticket.assigned_technician_name,
    urgent: ticket.urgent,
  };
}

/** `page` with the row of `updated`'s ticket replaced. The same object when the page has no such row. */
export function replaceRow(page: StaffTicketPage, updated: StaffTicketRow): StaffTicketPage {
  if (!page.items.some((row) => row.ticket_number === updated.ticket_number)) return page;
  const row = toRow(updated);
  return { ...page, items: page.items.map((item) => (item.ticket_number === row.ticket_number ? row : item)) };
}

export type ListLanguage = 'he' | 'en';

/** `he` for a Hebrew language tag, `en` for anything else. */
export function listLanguage(tag: string | undefined): ListLanguage {
  return tag !== undefined && tag.toLowerCase().startsWith('he') ? 'he' : 'en';
}

/** The name of a category in `language`; the other language when that name is blank. */
export function categoryName(category: { name_he: string; name_en: string }, language: ListLanguage): string {
  const preferred = language === 'he' ? category.name_he : category.name_en;
  const other = language === 'he' ? category.name_en : category.name_he;
  return preferred.trim() !== '' ? preferred : other;
}

/** The path of a ticket's detail page (R15.2 row link). */
export function ticketDetailPath(ticketNumber: number): string {
  return `/tickets/list/${ticketNumber}`;
}

// --- time (R15.2, R16.1) ---------------------------------------------------------------------------

export const STAFF_TIME_ZONE = 'Asia/Jerusalem';

let formatter: Intl.DateTimeFormat | null = null;

function jerusalemFormatter(): Intl.DateTimeFormat {
  formatter ??= new Intl.DateTimeFormat('en-GB', {
    timeZone: STAFF_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  });
  return formatter;
}

const HAS_ZONE = /(Z|[+-]\d{2}(:?\d{2})?)$/i;

/** An ISO instant as a `Date`. A value with no zone designator is read as UTC, which is what the server stores. */
export function parseInstant(iso: string): Date {
  const text = iso.trim();
  const withZone = text.includes('T') && !HAS_ZONE.test(text) ? `${text}Z` : text;
  return new Date(withZone);
}

/**
 * `DD/MM/YYYY HH:mm` of an instant in the Asia/Jerusalem time zone, to the minute, by `Intl.DateTimeFormat`
 * (which applies daylight saving). The same layout in both languages; the page isolates it from the
 * surrounding text. An unreadable value gives an empty string.
 */
export function formatJerusalemMinute(iso: string): string {
  const date = parseInstant(iso);
  if (Number.isNaN(date.getTime())) return '';
  const parts: Record<string, string> = {};
  for (const part of jerusalemFormatter().formatToParts(date)) {
    if (part.type !== 'literal') parts[part.type] = part.value;
  }
  return `${parts.day}/${parts.month}/${parts.year} ${parts.hour}:${parts.minute}`;
}
