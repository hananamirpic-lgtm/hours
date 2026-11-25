// The pure model of the management New ticket screen (task 29.5; R26.1, R26.2, R26.7, R23.12). No React, no
// DOM, no i18n instance: the components translate the keys this module returns, so every decision is testable
// without a renderer.
//
// Two steps. Step 1 picks one active customer from `GET /staff/customers?active=true&search=...`. Step 2 is the
// ticket form of Requirement 9 criterion 1, prefilled from the customer, sent as multipart with `customer_id`
// to `POST /staff/tickets`. The form rules (labels, categories, photo size hint, field order, focus, error
// maps) are the pure helpers of the portal form model, reused here instead of copied; only the staff-specific
// parts (the picker, the `customer_id` part and the failure mapping) are written in this file.
import { errorTextKey, type FieldErrors } from '../portal/formErrors';
import { settlementChoice, settlementName, type SettlementChoice, type SettlementLanguage } from '../portal/screens/settlementModel';
import { FORM_FIELD_ORDER, buildFormData, type FormValues } from '../portal/screens/ticketFormModel';
import type { FormConfig, FormLocality, FormSettings } from '../api/formTypes';
import { isStaffApiError } from '../api/staff';
import type { CustomerListQuery, StaffCustomer } from '../api/staffTypes';
import { SEARCH_MAX_LENGTH } from './customersModel';
import { ticketDetailPath } from './ticketListModel';

export { SEARCH_MAX_LENGTH };

/** The route of the screen, inside the management router. It is declared before `list/:ticketNumber`. */
export const NEW_TICKET_PATH = '/tickets/list/new';

// --- step 1: the customer picker (R26.1) ------------------------------------------------------------

/**
 * The request of the picker: active customers only (R26.1), page 1, with the typed search when it has text.
 * The server applies the search rules of Requirement 3 criterion 10 (name or phone substring).
 */
export function pickerQuery(search: string): CustomerListQuery {
  const query: { page: number; active: boolean; search?: string } = { page: 1, active: true };
  if (search.trim() !== '') query.search = search.trim();
  return query;
}

/** The picker state the view shows. */
export type PickerState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'error'; readonly code: string }
  | { readonly kind: 'empty' }
  /** `total` is the number of matches; `items` holds the first page, so `total > items.length` means more exist. */
  | { readonly kind: 'results'; readonly items: readonly StaffCustomer[]; readonly total: number };

export interface PickerStateInput {
  /** The loaded page, or undefined while there is none. */
  readonly data: { readonly items: readonly StaffCustomer[]; readonly total: number } | undefined;
  /** The code of the failed request, or null. A failed request with an earlier page keeps that page on screen. */
  readonly errorCode: string | null;
}

export function pickerState(input: PickerStateInput): PickerState {
  if (input.errorCode !== null && input.data === undefined) return { kind: 'error', code: input.errorCode };
  if (input.data === undefined) return { kind: 'loading' };
  if (input.data.items.length === 0) return { kind: 'empty' };
  return { kind: 'results', items: input.data.items, total: input.data.total };
}

/** True when more customers match than the page shows, so the screen asks for a narrower search. */
export function hasMoreMatches(state: PickerState): boolean {
  return state.kind === 'results' && state.total > state.items.length;
}

/** A customer's settlement and apartment as plain strings; an absent or blank value is ''. */
export function customerPlace(customer: Pick<StaffCustomer, 'city' | 'apartment'>): { city: string; apartment: string } {
  return { city: (customer.city ?? '').trim(), apartment: (customer.apartment ?? '').trim() };
}

// --- step 2: the form (R26.2) -----------------------------------------------------------------------

/**
 * The settlement choice of the staff form (R30.2, R30.3, R30.6). It is active only while the form-config
 * lists at least one active settlement and says `settlements_required`; otherwise the City field stays free
 * text exactly as before. The result feeds `initialValues` and `buildStaffFormData`.
 */
export function staffSettlementChoice(config: Pick<FormConfig, 'localities' | 'settlements_required'>): SettlementChoice {
  const choice = settlementChoice(config);
  return choice.required && choice.localities.length > 0 ? choice : { required: false, localities: [] };
}

/** One entry of the settlement select: the id and the name in the current language. */
export interface SettlementSelectOption {
  readonly id: string;
  readonly name: string;
}

/** The select entries in `language`, in the order the server sent them (a blank name falls back to the other language). */
export function settlementSelectOptions(
  localities: readonly FormLocality[],
  language: SettlementLanguage,
): readonly SettlementSelectOption[] {
  return localities.map((locality) => ({ id: locality.id, name: settlementName(locality, language) }));
}

/**
 * The multipart body: the customer form parts exactly as the portal sends them (`buildFormData`: the photo
 * part only when a photo is chosen and uploads are on) plus the `customer_id` part.
 *
 * With `settlementsRequired` the body carries `locality_id` and no `city` part, as the customer submission
 * does (R30.2); without it, `city` and no `locality_id` (R30.3).
 */
export function buildStaffFormData(
  customerId: string,
  values: FormValues,
  photo: File | null,
  settings: FormSettings,
  settlementsRequired = false,
): FormData {
  const body = buildFormData(values, photo, settings, settlementsRequired);
  body.append('customer_id', customerId);
  return body;
}

/** A message that belongs to no single field. */
export type NewTicketNotice =
  /** A 5xx, no answer or an unreadable answer: the ticket may or may not exist, so check the list first. */
  | { readonly kind: 'submit_failed' }
  /** Any other API code with no field to put it on: `customer_not_found`, `customer_inactive`, ... */
  | { readonly kind: 'error'; readonly code: string };

export interface SubmitFailure {
  /** Form field name to error code. Empty when the failure is a notice only. */
  readonly fieldErrors: FieldErrors;
  readonly notice: NewTicketNotice | null;
}

const FORM_FIELDS: readonly string[] = FORM_FIELD_ORDER;

/** Codes that mean the request never got a usable answer from the Ticket_API. */
const LOST_ANSWER_CODES: readonly string[] = ['network', 'generic', 'aborted', 'timeout'];

/** A 413 from the body guard names the photo, as in the portal form (R22.10). */
const TOO_LARGE_CODES: readonly string[] = ['request_too_large', 'photo_too_large'];

/**
 * What a failed submission means for the screen:
 *
 *   422 with `fields`                       each form field in the map shows its code beside the field; a code on
 *                                           another part (`customer_id`, `unknown_field`) becomes the notice
 *   request_too_large, photo_too_large      the photo field shows `photo_too_large`
 *   network, generic, timeout               notice submit_failed: check the ticket list before sending again
 *   any other code                          notice with that code (`customer_not_found` 404, `customer_inactive`
 *                                           409, `service_unavailable`, ...); unknown codes read as generic text
 *
 * Anything that is not a `StaffApiError` is treated like a lost answer.
 */
export function failureFromError(error: unknown): SubmitFailure {
  if (!isStaffApiError(error)) return { fieldErrors: {}, notice: { kind: 'submit_failed' } };

  const formErrors: Record<string, string> = {};
  let otherCode: string | null = null;
  for (const [name, code] of Object.entries(error.fields)) {
    if (FORM_FIELDS.includes(name)) formErrors[name] = code;
    else otherCode ??= code;
  }
  if (Object.keys(formErrors).length > 0) {
    return { fieldErrors: formErrors, notice: otherCode === null ? null : { kind: 'error', code: otherCode } };
  }
  if (otherCode !== null) return { fieldErrors: {}, notice: { kind: 'error', code: otherCode } };
  if (TOO_LARGE_CODES.includes(error.code)) return { fieldErrors: { photo: 'photo_too_large' }, notice: null };
  if (LOST_ANSWER_CODES.includes(error.code)) return { fieldErrors: {}, notice: { kind: 'submit_failed' } };
  return { fieldErrors: {}, notice: { kind: 'error', code: error.code } };
}

/** The translation key of a notice. */
export function noticeKey(notice: NewTicketNotice): string {
  return notice.kind === 'submit_failed' ? 'management.newTicket.submitFailed' : errorTextKey(notice.code);
}

// --- after the 201 ----------------------------------------------------------------------------------

/** The page the screen opens after the ticket was created (R26.2): the new ticket's detail. */
export function createdTicketPath(created: { readonly ticket_number: number }): string {
  return ticketDetailPath(created.ticket_number);
}
