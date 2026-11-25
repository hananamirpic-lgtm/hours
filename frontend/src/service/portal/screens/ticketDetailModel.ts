// The pure model of the ticket detail screen (R12.2, R12.5, R12.8, R12.11, R13, R19.8, R23.3, R23.6).
// No React, no DOM, no i18n instance: the caller passes a translate function where text is needed, so the
// whole decision table is testable without a renderer.

import { GENERIC_CODE, isPortalApiError } from '../../api/portalFetch';
import type { CategoryRef, CustomerTicketDetail, FieldLabelKey, FormLabels } from '../../api/ticketDetail';
import type { TicketStatus, TicketStatusEntry } from '../../types';
import { diffStatuses, type StatusAnnouncement } from '../announcements';
import { errorTextKey } from '../formErrors';
import type { PortalLanguage } from '../portalI18n';
import { placeText } from './settlementModel';

/** Text for a locale key, in the current language. The screen passes `(key) => t(key)`. */
export type Translate = (key: string) => string;

// --- the ticket number in the address --------------------------------------------------------------

export const MIN_TICKET_NUMBER = 10000;
export const MAX_TICKET_NUMBER = 99999;

/**
 * The ticket number of the `:ticketNumber` route parameter, or null when it is not 5 digits from 10000 to
 * 99999. The server answers such a value with the same 404 as a missing ticket (R12.3), so the screen
 * shows the not-found panel without a request.
 */
export function parseTicketNumber(raw: string | null | undefined): number | null {
  if (raw === null || raw === undefined || !/^[0-9]{5}$/.test(raw)) return null;
  const value = Number(raw);
  return value >= MIN_TICKET_NUMBER && value <= MAX_TICKET_NUMBER ? value : null;
}

/** The number inside a sentence: wrapped in left-to-right isolates so right-to-left text does not reorder it. */
export function ltrIsolate(text: string): string {
  return `\u2066${text}\u2069`;
}

// --- dates and names -------------------------------------------------------------------------------

/** `YYYY-MM-DD` as `DD/MM/YYYY` (R12.2). Text that is not in that form is returned unchanged. */
export function formatCreatedDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  return match ? `${match[3]}/${match[2]}/${match[1]}` : value;
}

/** The category name in the current language; the other language when that name is empty. */
export function categoryName(category: Pick<CategoryRef, 'name_he' | 'name_en'>, language: PortalLanguage): string {
  const own = (language === 'he' ? category.name_he : category.name_en).trim();
  if (own !== '') return own;
  return (language === 'he' ? category.name_en : category.name_he).trim();
}

/**
 * The label of a form field in the current language: the Field_Label of `GET /form-config` when it has
 * text, `fields.<key>` of the locale otherwise (before the labels load, or when the call failed) (R12.2).
 */
export function fieldLabel(
  key: FieldLabelKey,
  labels: FormLabels | undefined,
  language: PortalLanguage,
  translate: Translate,
): string {
  const text = labels?.[key]?.[language];
  return typeof text === 'string' && text.trim() !== '' ? text.trim() : translate(`fields.${key}`);
}

// --- status and cancel availability ----------------------------------------------------------------

/** The cancel control is offered only while the displayed status is Received or In Progress (R13.5). */
export function canCancel(status: TicketStatus): boolean {
  return status === 'received' || status === 'in_progress';
}

/**
 * The live-region message of a status change found by a reload (R23.15). Uses `diffStatuses` on a
 * one-ticket snapshot: null on the first load, for identical data and for another ticket.
 */
export function statusAnnouncement(
  previous: TicketStatusEntry | null,
  next: TicketStatusEntry,
): StatusAnnouncement | null {
  return diffStatuses(previous === null ? null : [previous], [next])[0] ?? null;
}

// --- the rows of the detail ------------------------------------------------------------------------

/** The glyph in front of the urgent text. Decorative: the text carries the meaning (R12.6, R23.5). */
export const URGENT_ICON = '\u26A0\uFE0E';

export type DetailRowKey =
  | 'number'
  | 'status'
  | 'urgent'
  | 'created'
  | 'category'
  | 'city'
  | 'apartment'
  | 'phone'
  | 'description';

export type RowValue =
  /** Text read left to right inside `<bdi dir="ltr">`: the ticket number, the phone, the date. */
  | { readonly kind: 'ltr'; readonly text: string }
  | { readonly kind: 'plain'; readonly text: string }
  /** A description: rendered as text with `white-space: pre-wrap`, so its line breaks stay. */
  | { readonly kind: 'description'; readonly text: string }
  /** The urgent marker: icon plus text. */
  | { readonly kind: 'urgent'; readonly text: string };

export interface DetailRow {
  readonly key: DetailRowKey;
  readonly label: string;
  readonly value: RowValue;
}

/**
 * Every customer field of R12.2, in reading order, each with its label: Field_Labels for the form fields
 * (city, apartment, phone, category, description, urgent), the portal's own text for the ticket number,
 * status and date, which are not form fields. The urgent row appears only when the flag is set. An empty
 * description gives the no-description text.
 */
export function buildDetailRows(
  ticket: CustomerTicketDetail,
  language: PortalLanguage,
  labels: FormLabels | undefined,
  translate: Translate,
): DetailRow[] {
  const label = (key: FieldLabelKey): string => fieldLabel(key, labels, language, translate);
  const description = ticket.description;
  const rows: DetailRow[] = [
    { key: 'number', label: translate('portal.detail.number'), value: { kind: 'ltr', text: String(ticket.ticket_number) } },
    { key: 'status', label: translate('portal.detail.status'), value: { kind: 'plain', text: translate(`status.${ticket.status}`) } },
  ];
  if (ticket.urgent) {
    rows.push({ key: 'urgent', label: label('urgent'), value: { kind: 'urgent', text: translate('portal.urgent.marker') } });
  }
  rows.push(
    { key: 'created', label: translate('portal.detail.created'), value: { kind: 'ltr', text: formatCreatedDate(ticket.created_date) } },
    { key: 'category', label: label('category'), value: { kind: 'plain', text: categoryName(ticket.category, language) } },
    // The settlement in the current language when the ticket has one, else the stored city text (R30.5).
    { key: 'city', label: label('city'), value: { kind: 'plain', text: placeText(ticket.locality, ticket.city, language) } },
    { key: 'apartment', label: label('apartment'), value: { kind: 'plain', text: ticket.apartment } },
    { key: 'phone', label: label('phone'), value: { kind: 'ltr', text: ticket.phone } },
    {
      key: 'description',
      label: label('description'),
      value:
        description.trim() === ''
          ? { kind: 'plain', text: translate('portal.detail.noDescription') }
          : { kind: 'description', text: description },
    },
  );
  return rows;
}

// --- the photo -------------------------------------------------------------------------------------

export type PhotoView =
  | { readonly kind: 'none' }
  | { readonly kind: 'loading' }
  | { readonly kind: 'error'; readonly messageKeys: readonly string[]; readonly retryable: boolean }
  | { readonly kind: 'ready'; readonly url: string };

export interface PhotoViewInput {
  /** `has_photo` of the ticket. The Photo upload setting is not an input: a present photo always shows (R12.2). */
  readonly hasPhoto: boolean;
  readonly status: 'pending' | 'error' | 'success';
  readonly error: unknown;
  /** The object URL of the fetched blob, once created. */
  readonly url: string | null;
}

/** `[lead]`, plus the specific error text when the API sent a code the portal has text for. */
function leadAndSpecific(lead: string, error: unknown): string[] {
  const code = isPortalApiError(error) ? error.code : GENERIC_CODE;
  const specific = errorTextKey(code);
  return specific === 'errors.generic' ? [lead] : [lead, specific];
}

/**
 * What the photo area shows. A fetched photo stays shown whatever happens afterwards. Loading covers the
 * request and the moment before the object URL exists. A failure shows the error with a retry control,
 * except a 404, which says the photo is gone: asking again cannot help, the ticket reload reports the
 * change (R12.8; 503 `photo_storage_unavailable` is retryable).
 */
export function photoView(input: PhotoViewInput): PhotoView {
  if (!input.hasPhoto) return { kind: 'none' };
  if (input.url !== null) return { kind: 'ready', url: input.url };
  if (input.status === 'error') {
    const gone = isPortalApiError(input.error) && input.error.status === 404;
    return { kind: 'error', messageKeys: leadAndSpecific('portal.detail.photoError', input.error), retryable: !gone };
  }
  return { kind: 'loading' };
}

// --- load state ------------------------------------------------------------------------------------

/** A 404 that is the Ticket_API's own answer for a ticket that is foreign, missing or malformed (R12.3). */
export function isTicketNotFound(error: unknown): boolean {
  return isPortalApiError(error) && error.status === 404 && error.code === 'ticket_not_found';
}

export type ScreenState =
  /** No ticket data is shown: the panel of R12.11. */
  | { readonly kind: 'not-found' }
  | { readonly kind: 'loading' }
  /** Nothing was loaded and the request failed: the error with a retry control, no data. */
  | { readonly kind: 'error'; readonly messageKeys: readonly string[] }
  /** The ticket is shown. `staleErrorKeys` is set when a later reload failed: the data stays (R12.8). */
  | { readonly kind: 'ready'; readonly staleErrorKeys: readonly string[] | null };

export interface ScreenStateInput {
  /** False when the route parameter is not a ticket number. */
  readonly numberValid: boolean;
  readonly hasData: boolean;
  /** The error of the last request, null once a request succeeded. */
  readonly error: unknown;
}

export function screenState(input: ScreenStateInput): ScreenState {
  if (!input.numberValid || isTicketNotFound(input.error)) return { kind: 'not-found' };
  if (input.hasData) {
    return {
      kind: 'ready',
      staleErrorKeys: input.error === null || input.error === undefined ? null : leadAndSpecific('portal.detail.loadError', input.error),
    };
  }
  if (input.error !== null && input.error !== undefined) {
    return { kind: 'error', messageKeys: leadAndSpecific('portal.detail.loadError', input.error) };
  }
  return { kind: 'loading' };
}

// --- cancellation ----------------------------------------------------------------------------------

export interface CancelFailure {
  /** Locale keys of the message, shown in order. */
  readonly messageKeys: readonly string[];
  /** Reload the ticket: its state differs from what the customer saw, or it is gone (R13.9). */
  readonly reload: boolean;
}

/**
 * The portal's answer to a failed cancellation (R13.9):
 *
 *   409 ticket_status_changed   reload, `errors.ticket_status_changed` (the current status is then shown)
 *   409 invalid_transition      reload, `errors.invalid_transition`      (closed or cancelled meanwhile)
 *   422 invalid_expected_status reload, cancelFailed + `errors.invalid_expected_status`
 *   404 ticket_not_found        reload (the reload answers 404 and the not-found panel replaces the ticket)
 *   503, 5xx, 429, network      no reload, cancelFailed + the code's text (generic for network and timeout)
 */
export function cancelFailureOf(error: unknown): CancelFailure {
  const code = isPortalApiError(error) ? error.code : GENERIC_CODE;
  switch (code) {
    case 'ticket_status_changed':
    case 'invalid_transition':
      return { messageKeys: [errorTextKey(code)], reload: true };
    case 'invalid_expected_status':
    case 'ticket_not_found':
      return { messageKeys: leadAndSpecific('portal.detail.cancelFailed', error), reload: true };
    default:
      return { messageKeys: leadAndSpecific('portal.detail.cancelFailed', error), reload: false };
  }
}

/** The confirmation dialog: closed, asking, or waiting for the answer to the one request it sent. */
export type CancelPhase = 'closed' | 'confirming' | 'pending';

export type CancelAction = 'open' | 'dismiss' | 'confirm' | 'settled';

/**
 * The state machine of the dialog (R13.2). It is component state and takes no input from the ticket data,
 * so a 30-second reload cannot change it (R12.5). `confirm` is accepted only while confirming, which is
 * how one confirmation sends at most one request; `dismiss` is ignored while a request is pending.
 */
export function cancelFlowReducer(phase: CancelPhase, action: CancelAction): CancelPhase {
  switch (action) {
    case 'open':
      return phase === 'closed' ? 'confirming' : phase;
    case 'dismiss':
      return phase === 'confirming' ? 'closed' : phase;
    case 'confirm':
      return phase === 'confirming' ? 'pending' : phase;
    case 'settled':
      return 'closed';
  }
}
