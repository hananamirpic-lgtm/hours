// The pure model of the management ticket detail page (task 24.4; R15.9, R15.10, R16.1 to R16.7, R16.9,
// R9.13, R23.12). No React, no DOM, no i18n instance: components translate the keys this module returns, so
// the decisions are testable without a renderer.
import { isErrorCode } from '../api/errorCodes';
import { GENERIC_CODE } from '../api/portalFetch';
import { isStaffApiError } from '../api/staff';
import type {
  StaffCategoryRef,
  StaffHistoryEntry,
  StaffTicketDetail,
  StatusChangeBody,
  TicketStatus,
} from '../api/staffTypes';

// --- the ticket number in the address --------------------------------------------------------------

export const MIN_TICKET_NUMBER = 10000;
export const MAX_TICKET_NUMBER = 99999;

/**
 * The ticket number of the `:ticketNumber` route parameter, or null when it is not 5 digits from 10000 to
 * 99999. The server answers such a value with `ticket_not_found`, so the page shows the not-found panel
 * without a request.
 */
export function parseTicketNumber(raw: string | null | undefined): number | null {
  if (raw === null || raw === undefined || !/^[0-9]{5}$/.test(raw)) return null;
  const value = Number(raw);
  return value >= MIN_TICKET_NUMBER && value <= MAX_TICKET_NUMBER ? value : null;
}

/** The number inside a sentence, wrapped in left-to-right isolates so right-to-left text does not reorder it. */
export function ltrIsolate(text: string): string {
  return `\u2066${text}\u2069`;
}

// --- language, dates and names ---------------------------------------------------------------------

export type DetailLanguage = 'he' | 'en';

/** `en` only for a language that starts with `en`; Hebrew otherwise (the default of the staff app). */
export function languageOf(raw: string | null | undefined): DetailLanguage {
  return typeof raw === 'string' && raw.toLowerCase().startsWith('en') ? 'en' : 'he';
}

/** The category name in the current language; the other language when that name is empty. */
export function categoryName(category: Pick<StaffCategoryRef, 'name_he' | 'name_en'>, language: DetailLanguage): string {
  const own = (language === 'he' ? category.name_he : category.name_en).trim();
  if (own !== '') return own;
  return (language === 'he' ? category.name_en : category.name_he).trim();
}

/** An ISO instant as milliseconds. A string with no zone is read as UTC, the zone the server stores. NaN when unreadable. */
export function instantMs(value: string): number {
  const text = value.trim();
  const zoned = /(?:Z|[+-]\d{2}(?::?\d{2})?)$/i.test(text);
  return Date.parse(zoned ? text : `${text}Z`);
}

const JERUSALEM = 'Asia/Jerusalem';

let minuteFormat: Intl.DateTimeFormat | null = null;

/**
 * An instant as `DD/MM/YYYY HH:mm` in the Asia/Jerusalem time zone, to the minute (R16.1), in any browser
 * language and time zone. Text that is not an instant is returned unchanged.
 */
export function formatJerusalemMinute(value: string): string {
  const ms = instantMs(value);
  if (Number.isNaN(ms)) return value;
  minuteFormat ??= new Intl.DateTimeFormat('en-GB', {
    timeZone: JERUSALEM,
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  });
  const parts: Record<string, string> = {};
  for (const part of minuteFormat.formatToParts(new Date(ms))) parts[part.type] = part.value;
  return `${parts.day}/${parts.month}/${parts.year} ${parts.hour}:${parts.minute}`;
}

// --- the rows of the detail ------------------------------------------------------------------------

export type DetailRowKind =
  /** Text read left to right inside `<bdi dir="ltr">`: the ticket number, the phone, the date. */
  | 'ltr'
  | 'text'
  /** Free text kept with its line breaks. Rendered as text, never as markup (R9.13). */
  | 'description'
  /** The status name, translated by the component. */
  | 'status'
  /** The SOS indicator: icon plus the text "SOS". */
  | 'sos';

export type DetailRowKey =
  | 'number'
  | 'status'
  | 'sos'
  | 'customer'
  | 'phone'
  | 'city'
  | 'apartment'
  | 'category'
  | 'description'
  | 'created';

export interface DetailRow {
  readonly key: DetailRowKey;
  /** A `service` namespace key. */
  readonly labelKey: string;
  readonly kind: DetailRowKind;
  /** The value. For `status` it is the status; for `sos` it is empty; for `description` it may be empty. */
  readonly text: string;
}

/** The glyph in front of the SOS text. Decorative: the text carries the meaning (R23.12). */
export const SOS_ICON = '\u26A0\uFE0E';

/**
 * Every field of R16.1 in reading order: number, status, the SOS indicator (only when the urgent flag is
 * set), customer name, phone, city, apartment, category, description, creation time. The assigned
 * technician and the history are separate sections of the page.
 */
export function buildDetailRows(ticket: StaffTicketDetail, language: DetailLanguage): DetailRow[] {
  const rows: DetailRow[] = [
    { key: 'number', labelKey: 'management.ticket.number', kind: 'ltr', text: String(ticket.ticket_number) },
    { key: 'status', labelKey: 'management.ticket.status', kind: 'status', text: ticket.status },
  ];
  if (ticket.urgent) {
    rows.push({ key: 'sos', labelKey: 'management.tickets.columns.sos', kind: 'sos', text: '' });
  }
  rows.push(
    { key: 'customer', labelKey: 'management.ticket.customer', kind: 'text', text: ticket.customer_name },
    { key: 'phone', labelKey: 'management.ticket.phone', kind: 'ltr', text: ticket.phone },
    { key: 'city', labelKey: 'management.ticket.city', kind: 'text', text: ticket.city },
    { key: 'apartment', labelKey: 'management.ticket.apartment', kind: 'text', text: ticket.apartment },
    { key: 'category', labelKey: 'management.ticket.category', kind: 'text', text: categoryName(ticket.category, language) },
    { key: 'description', labelKey: 'management.ticket.description', kind: 'description', text: ticket.description },
    { key: 'created', labelKey: 'management.ticket.created', kind: 'ltr', text: formatJerusalemMinute(ticket.created_at) },
  );
  return rows;
}

// --- status actions --------------------------------------------------------------------------------

/**
 * The targets Requirement 14 allows Ticket_Staff from `status` (R15.9, R15.11): Received to In Progress,
 * In Progress to Closed, Closed to In Progress. A Cancelled ticket has none.
 */
export function allowedTargets(status: TicketStatus): readonly TicketStatus[] {
  switch (status) {
    case 'received':
      return ['in_progress'];
    case 'in_progress':
      return ['closed'];
    case 'closed':
      return ['in_progress'];
    case 'cancelled':
      return [];
  }
}

/** The locale key of the action that moves a ticket from `from` to `to`, or null when that move is not allowed. */
export function actionKey(from: TicketStatus, to: TicketStatus): string | null {
  if (!allowedTargets(from).includes(to)) return null;
  if (from === 'received') return 'management.tickets.actions.toInProgress';
  if (from === 'in_progress') return 'management.tickets.actions.close';
  return 'management.tickets.actions.reopen';
}

/** True when the move is the approval step, the only one that may carry an assigned technician name (R14.6). */
export function isApproval(from: TicketStatus, to: TicketStatus): boolean {
  return from === 'received' && to === 'in_progress';
}

/**
 * The request body of a status change. `expected_status` is the status the page displays (R14.8, R15.9).
 * The technician name goes along only on the approval step, and only when it has text after trimming; an
 * absent name lets the server keep or default it (R14.6).
 */
export function statusChangeBody(from: TicketStatus, to: TicketStatus, technicianName: string): StatusChangeBody {
  const name = technicianName.trim();
  if (isApproval(from, to) && name !== '') {
    return { expected_status: from, target_status: to, assigned_technician_name: name };
  }
  return { expected_status: from, target_status: to };
}

// --- failures --------------------------------------------------------------------------------------

/** The error code of a thrown value: the API's code, or `generic` for anything else (including a network failure). */
export function codeOf(error: unknown): string {
  return isStaffApiError(error) ? error.code : GENERIC_CODE;
}

/** `errors.<code>` for a code the Ticket_API defines (the catalogue has text for each), `errors.generic` otherwise. */
export function errorKeyOf(code: string): string {
  return isErrorCode(code) ? `errors.${code}` : 'errors.generic';
}

export interface StatusFailure {
  /** The locale key of the action that failed, fixed when it was asked for: the reload that follows can change the status (R15.10). */
  readonly actionKey: string | null;
  readonly code: string;
}

/** The result of a failed status change from `from` to `to`. The page always reloads the ticket afterwards (R15.10). */
export function statusFailureOf(from: TicketStatus, to: TicketStatus, error: unknown): StatusFailure {
  return { actionKey: actionKey(from, to), code: codeOf(error) };
}

// --- the history -----------------------------------------------------------------------------------

/** The two request fields of a history entry, which are also the keys of a 422 `fields` map (R16.6). */
export const ENTRY_TEXT_FIELD = 'entry_text';
export const ENTRY_TECHNICIAN_FIELD = 'technician_name';
export type EntryField = typeof ENTRY_TEXT_FIELD | typeof ENTRY_TECHNICIAN_FIELD;
/** The order of the fields in the form, which is the order of the messages and of the focus. */
export const ENTRY_FIELDS: readonly EntryField[] = [ENTRY_TEXT_FIELD, ENTRY_TECHNICIAN_FIELD];

export interface EntryValues {
  readonly entry_text: string;
  readonly technician_name: string;
}

/** The add form: empty text, the technician name prefilled with the acting username (R16.4). */
export function addFormValues(username: string | null | undefined): EntryValues {
  return { entry_text: '', technician_name: username ?? '' };
}

/** The edit form: the stored text and technician name. */
export function editFormValues(entry: Pick<StaffHistoryEntry, 'entry_text' | 'technician_name'>): EntryValues {
  return { entry_text: entry.entry_text, technician_name: entry.technician_name };
}

export interface EntryFailure {
  /** The error code per failing field, for the fields of the form only. */
  readonly fields: Readonly<Partial<Record<EntryField, string>>>;
  /** The code to show above the form when no field failed (a missing ticket or entry, a server or network error). Null when `fields` has entries. */
  readonly code: string | null;
}

/**
 * Splits a failed add or edit into per-field codes and a form-level code (R16.6). Only the two form fields
 * are read from `fields`; a 422 with none of them, or any other failure, gives a form-level code.
 */
export function entryFailureOf(error: unknown): EntryFailure {
  const fields: Partial<Record<EntryField, string>> = {};
  if (isStaffApiError(error)) {
    for (const name of ENTRY_FIELDS) {
      const code = Object.prototype.hasOwnProperty.call(error.fields, name) ? error.fields[name] : undefined;
      if (code !== undefined) fields[name] = code;
    }
  }
  return Object.keys(fields).length > 0 ? { fields, code: null } : { fields, code: codeOf(error) };
}

/** Entries oldest creation time first (R16.9). Equal or unreadable times keep their given order. */
export function sortHistory(entries: readonly StaffHistoryEntry[]): StaffHistoryEntry[] {
  return entries
    .map((entry, index) => ({ entry, index, ms: instantMs(entry.created_at) }))
    .sort((a, b) => {
      const left = Number.isNaN(a.ms) ? 0 : a.ms;
      const right = Number.isNaN(b.ms) ? 0 : b.ms;
      return left - right || a.index - b.index;
    })
    .map((item) => item.entry);
}

/** The ticket with `entry` added, or replaced when an entry with its id exists. History stays oldest first. */
export function withEntry(ticket: StaffTicketDetail, entry: StaffHistoryEntry): StaffTicketDetail {
  const exists = ticket.history.some((item) => item.id === entry.id);
  const history = exists
    ? ticket.history.map((item) => (item.id === entry.id ? entry : item))
    : [...ticket.history, entry];
  return { ...ticket, history: sortHistory(history) };
}

export interface EntryTimes {
  /** `created_at` formatted. */
  readonly created: string;
  /** `edited_at` formatted, or null when the entry was never edited: nothing is shown then (R16.9). */
  readonly edited: string | null;
}

export function entryTimes(entry: Pick<StaffHistoryEntry, 'created_at' | 'edited_at'>): EntryTimes {
  return {
    created: formatJerusalemMinute(entry.created_at),
    edited: entry.edited_at === null ? null : formatJerusalemMinute(entry.edited_at),
  };
}

// --- the photo -------------------------------------------------------------------------------------

export type PhotoView =
  /** The ticket has no photo: nothing is shown, no placeholder (R16.1). */
  | { readonly kind: 'none' }
  | { readonly kind: 'loading' }
  | { readonly kind: 'error'; readonly code: string; readonly retryable: boolean }
  | { readonly kind: 'ready'; readonly url: string };

export interface PhotoViewInput {
  readonly hasPhoto: boolean;
  readonly status: 'pending' | 'error' | 'success';
  readonly error: unknown;
  /** The object URL of the fetched blob, once created. */
  readonly url: string | null;
}

/**
 * What the photo area shows. A fetched photo stays shown whatever happens afterwards. A 404 says the photo
 * is gone, so retrying cannot help; other failures offer a retry.
 */
export function photoView(input: PhotoViewInput): PhotoView {
  if (!input.hasPhoto) return { kind: 'none' };
  if (input.url !== null) return { kind: 'ready', url: input.url };
  if (input.status === 'error') {
    const code = codeOf(input.error);
    return { kind: 'error', code, retryable: code !== 'ticket_not_found' };
  }
  return { kind: 'loading' };
}

// --- load state ------------------------------------------------------------------------------------

export function isTicketNotFound(error: unknown): boolean {
  return isStaffApiError(error) && error.code === 'ticket_not_found';
}

export type ScreenState =
  | { readonly kind: 'not-found' }
  | { readonly kind: 'loading' }
  /** Nothing was loaded and the request failed. */
  | { readonly kind: 'error'; readonly code: string }
  /** The ticket is shown. `staleErrorCode` is set when a later reload failed: the data stays. */
  | { readonly kind: 'ready'; readonly staleErrorCode: string | null };

export interface ScreenStateInput {
  /** False when the route parameter is not a ticket number. */
  readonly numberValid: boolean;
  readonly hasData: boolean;
  /** The error of the last request, null once a request succeeded. */
  readonly error: unknown;
}

export function screenState(input: ScreenStateInput): ScreenState {
  if (!input.numberValid || isTicketNotFound(input.error)) return { kind: 'not-found' };
  const failed = input.error !== null && input.error !== undefined;
  if (input.hasData) return { kind: 'ready', staleErrorCode: failed ? codeOf(input.error) : null };
  if (failed) return { kind: 'error', code: codeOf(input.error) };
  return { kind: 'loading' };
}
