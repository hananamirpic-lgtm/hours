// The ticket form without React (task 23.3; R9, R10.8, R11.1, R22.8, R22.10, R23.1, R23.2, R23.13).
//
// Everything the form decides is here as a pure function: the labels, the prefill, the category list,
// the required state, the multipart body, the client-side photo size check, and what each kind of failed
// submission means for the screen. `TicketFormScreen.tsx` holds the state and renders; this file holds
// the rules, so they are testable without a DOM.
import { PortalApiError } from '../../api/portalFetch';
import { FORM_LABEL_KEYS, type FormCategory, type FormConfig, type FormLabelKey, type FormSettings } from '../../api/formTypes';
import { errorTextKey, firstFailingField, fieldsToMessages, type ErrorTextKey, type FieldErrors, type FieldMessage } from '../formErrors';
import { waitTimeMessage, type WaitTime } from '../waitTime';

// --- constants -------------------------------------------------------------------------------------

/** The photo limit of the server (R11.4): 10 MB. A file of exactly this size is accepted. */
export const MAX_PHOTO_BYTES = 10_485_760;

/**
 * The `accept` of the photo input. There is no `capture` attribute on purpose: with it unset a phone
 * offers the camera and the photo gallery in one chooser (R11.1), and a desktop offers the file dialog.
 */
export const PHOTO_ACCEPT = 'image/jpeg,image/png,image/webp';

/** The route of the confirmation screen, relative to the router basename. */
export const CONFIRMATION_PATH = '/new/done';

/**
 * The names of the form's fields as the server reports them in a 422 `fields` map, in reading order
 * (the order they appear in the DOM, which is also the order focus moves in, R23.13).
 */
export const FORM_FIELD_ORDER = ['city', 'apartment', 'phone', 'category_id', 'description', 'photo', 'urgent'] as const;
export type FormFieldName = (typeof FORM_FIELD_ORDER)[number];

/** The field order for the current settings: the photo control is absent while uploads are off. */
export function fieldOrder(photoUploadEnabled: boolean): readonly FormFieldName[] {
  return photoUploadEnabled ? FORM_FIELD_ORDER : FORM_FIELD_ORDER.filter((name) => name !== 'photo');
}

/** The label key of each field name (the server calls the category field `category_id`, the label `category`). */
const LABEL_OF_FIELD: Readonly<Record<FormFieldName, FormLabelKey>> = {
  city: 'city',
  apartment: 'apartment',
  phone: 'phone',
  category_id: 'category',
  description: 'description',
  photo: 'photo',
  urgent: 'urgent',
};

export function labelKeyOfField(field: string): FormLabelKey | null {
  return Object.prototype.hasOwnProperty.call(LABEL_OF_FIELD, field) ? LABEL_OF_FIELD[field as FormFieldName] : null;
}

// --- element ids -----------------------------------------------------------------------------------

export const SUMMARY_ID = 'ticket-form-error-summary';
export const NOTICE_ID = 'ticket-form-notice';
export function fieldId(field: string): string {
  return `ticket-form-${field}`;
}
export function errorId(field: string): string {
  return `ticket-form-${field}-error`;
}
export function hintId(field: string): string {
  return `ticket-form-${field}-hint`;
}

/** The `aria-describedby` value of a field: its error message first, then its hint. Undefined when neither exists. */
export function describedBy(field: string, hasError: boolean, hasHint: boolean): string | undefined {
  const ids: string[] = [];
  if (hasError) ids.push(errorId(field));
  if (hasHint) ids.push(hintId(field));
  return ids.length === 0 ? undefined : ids.join(' ');
}

// --- labels ----------------------------------------------------------------------------------------

export type ResolvedLabels = Readonly<Record<FormLabelKey, string>>;

/**
 * The visible label of every field in `language` (R9.1). A Field_Label that is missing or blank in the
 * configuration falls back to the shipped text of the same language (`fields.*` in the locale), which the
 * caller passes in `fallbacks`.
 */
export function resolveLabels(
  labels: FormConfig['labels'] | null | undefined,
  language: 'he' | 'en',
  fallbacks: Readonly<Record<FormLabelKey, string>>,
): ResolvedLabels {
  const resolved = {} as Record<FormLabelKey, string>;
  for (const key of FORM_LABEL_KEYS) {
    const text = labels?.[key]?.[language];
    resolved[key] = typeof text === 'string' && text.trim() !== '' ? text : fallbacks[key];
  }
  return resolved;
}

// --- categories ------------------------------------------------------------------------------------

export interface CategoryOption {
  readonly id: string;
  readonly name: string;
}

/**
 * The dropdown entries in `language`, in the order the server sent them (active only, oldest first,
 * R9.3). A name that is blank in the current language falls back to the other language, so an entry is
 * never empty. There is no preselected entry: the screen adds a placeholder option and starts with `''`.
 */
export function categoryOptions(categories: readonly FormCategory[], language: 'he' | 'en'): CategoryOption[] {
  return categories.map((category) => {
    const own = language === 'he' ? category.name_he : category.name_en;
    const other = language === 'he' ? category.name_en : category.name_he;
    return { id: category.id, name: own.trim() !== '' ? own : other };
  });
}

// --- values ----------------------------------------------------------------------------------------

export interface FormValues {
  readonly city: string;
  readonly apartment: string;
  readonly phone: string;
  /** The chosen category id, or '' while none is chosen. */
  readonly categoryId: string;
  readonly description: string;
  readonly urgent: boolean;
}

/** The part of the customer's record the form prefills from (R9.2). */
export interface PrefillSource {
  readonly city: string | null;
  readonly apartment: string | null;
  readonly phone: string;
}

/**
 * The values the form opens with: City, Apartment number and Phone number from the Customer_Record
 * (empty where the record holds none), and nothing else. No category is chosen and Urgent is off.
 */
export function initialValues(me: PrefillSource): FormValues {
  return {
    city: me.city ?? '',
    apartment: me.apartment ?? '',
    phone: me.phone,
    categoryId: '',
    description: '',
    urgent: false,
  };
}

/** The error-map name of a value key: the server names the category field `category_id`. */
export function errorFieldOf(key: keyof FormValues): FormFieldName {
  return key === 'categoryId' ? 'category_id' : key;
}

/** The three-way state of the Description field (R9.5, R23.1). */
export type RequiredState = 'required' | 'optional';

export function descriptionState(settings: FormSettings): RequiredState {
  return settings.description_required ? 'required' : 'optional';
}

// --- photo -----------------------------------------------------------------------------------------

/** True when a file is over the 10 MB limit. 10,485,760 bytes is accepted, 10,485,761 is too large. */
export function photoTooLarge(sizeBytes: number): boolean {
  return sizeBytes > MAX_PHOTO_BYTES;
}

/**
 * The error code shown at once when a chosen photo is over the limit (R22.10), or null. It is a hint:
 * the submission still sends the file and the server decides (R11.4).
 */
export function clientPhotoError(file: { readonly size: number } | null): 'photo_too_large' | null {
  return file !== null && photoTooLarge(file.size) ? 'photo_too_large' : null;
}

// --- the request body ------------------------------------------------------------------------------

/**
 * The multipart body of the submission. Values go as typed (the server trims). The `photo` part is
 * present only when a photo is chosen and uploads are on; with no photo the part is left out entirely,
 * because an empty file part is rejected as an invalid image (R11.3).
 */
export function buildFormData(values: FormValues, photo: File | null, settings: FormSettings): FormData {
  const body = new FormData();
  body.append('city', values.city);
  body.append('apartment', values.apartment);
  body.append('phone', values.phone);
  body.append('category_id', values.categoryId);
  body.append('description', values.description);
  body.append('urgent', values.urgent ? 'true' : 'false');
  if (photo !== null && settings.photo_upload_enabled) body.append('photo', photo, photo.name);
  return body;
}

// --- failed submissions ----------------------------------------------------------------------------

/** A message that belongs to no single field (R23.14). */
export type FormNotice =
  /** 5xx, no response, timeout or an unreadable success body: the ticket may or may not exist (R9.12). */
  | { readonly kind: 'submit_failed' }
  /** 429 (R22.8). */
  | { readonly kind: 'rate_limited'; readonly wait: WaitTime }
  /** 503 `ticket_numbers_exhausted` (R10.8). */
  | { readonly kind: 'numbers_exhausted' }
  /** Any other API error with no field to put it on. */
  | { readonly kind: 'error'; readonly code: string };

export interface SubmitFailure {
  /** Field name to error code, shown beside the fields. Empty when the failure is a notice only. */
  readonly fieldErrors: FieldErrors;
  readonly notice: FormNotice | null;
}

/**
 * What a failed submission means for the screen:
 *
 *   422 with `fields`                 fieldErrors = the server's map (R9.6, R9.7)
 *   413 (any code)                    fieldErrors.photo = photo_too_large (R22.10: `request_too_large` or an unknown code too)
 *   429                               notice rate_limited with the wait time from `Retry-After` (R22.8)
 *   503 ticket_numbers_exhausted      notice numbers_exhausted (R10.8)
 *   5xx, status 0 (network, timeout)  notice submit_failed: check the list before sending again (R9.12)
 *   any other 4xx, 422 with no fields notice error with that code (generic text for an unknown code)
 *
 * Anything that is not a `PortalApiError` is treated like a network failure.
 */
export function failureFromError(error: unknown): SubmitFailure {
  if (!(error instanceof PortalApiError)) return { fieldErrors: {}, notice: { kind: 'submit_failed' } };
  if (error.status === 413) return { fieldErrors: { photo: 'photo_too_large' }, notice: null };
  if (error.status === 429) return { fieldErrors: {}, notice: { kind: 'rate_limited', wait: waitTimeMessage(error.retryAfter) } };
  if (error.status === 503 && error.code === 'ticket_numbers_exhausted') {
    return { fieldErrors: {}, notice: { kind: 'numbers_exhausted' } };
  }
  if (error.status === 0 || error.status >= 500) return { fieldErrors: {}, notice: { kind: 'submit_failed' } };
  if (error.status === 422 && Object.keys(error.fields).length > 0) return { fieldErrors: error.fields, notice: null };
  return { fieldErrors: {}, notice: { kind: 'error', code: error.code } };
}

/** A translation key with the count a plural key needs. */
export interface TextRef {
  readonly key: string;
  readonly count?: number;
}

/** The translation key of a wait time (`portal.waitTime.*`): plural by `count`, or `none`. */
export function waitTimeRef(wait: WaitTime): TextRef {
  return wait.unit === 'none' ? { key: 'portal.waitTime.none' } : { key: `portal.waitTime.${wait.unit}`, count: wait.value };
}

/** The texts of a notice, in reading order: the message, and for a rate limit the wait time after it (R22.8). */
export function noticeTexts(notice: FormNotice): TextRef[] {
  switch (notice.kind) {
    case 'submit_failed':
      return [{ key: 'portal.form.submitFailed' }];
    case 'rate_limited':
      return [{ key: 'errors.rate_limited' }, waitTimeRef(notice.wait)];
    case 'numbers_exhausted':
      return [{ key: 'errors.ticket_numbers_exhausted' }];
    case 'error':
      return [{ key: errorTextKey(notice.code) }];
  }
}

// --- field errors ----------------------------------------------------------------------------------

/** The field that takes focus after a failed validation: the first failing one in reading order (R23.13), or null. */
export function focusTarget(fieldErrors: FieldErrors | null | undefined, photoUploadEnabled: boolean): FormFieldName | null {
  const name = firstFailingField(fieldErrors, fieldOrder(photoUploadEnabled));
  return name === null ? null : (name as FormFieldName);
}

/** One `{field, textKey}` per failing field, in reading order, then fields the form does not have (for the summary). */
export function errorSummaryItems(fieldErrors: FieldErrors, photoUploadEnabled: boolean): FieldMessage[] {
  return fieldsToMessages(fieldErrors, fieldOrder(photoUploadEnabled));
}

/** The error text key of one field, or null when it has no error. */
export function fieldErrorKey(fieldErrors: FieldErrors, field: string): ErrorTextKey | null {
  return Object.prototype.hasOwnProperty.call(fieldErrors, field) ? errorTextKey(fieldErrors[field]) : null;
}

/** `fieldErrors` without `field`: changing a value clears the message that was about the old value. */
export function withoutFieldError(fieldErrors: FieldErrors, field: string): FieldErrors {
  if (!Object.prototype.hasOwnProperty.call(fieldErrors, field)) return fieldErrors;
  const rest: Record<string, string> = { ...fieldErrors };
  delete rest[field];
  return rest;
}

/** The errors the form shows: the server's, plus the immediate photo-size hint where the server has not spoken. */
export function shownErrors(serverErrors: FieldErrors, photo: { readonly size: number } | null, photoUploadEnabled: boolean): FieldErrors {
  const clientCode = photoUploadEnabled ? clientPhotoError(photo) : null;
  return clientCode === null ? serverErrors : { photo: clientCode, ...serverErrors };
}

// --- confirmation ----------------------------------------------------------------------------------

/** The router state the form passes to the confirmation route. */
export interface ConfirmationState {
  readonly ticketNumber: number;
}

export function confirmationState(ticketNumber: number): ConfirmationState {
  return { ticketNumber };
}

/**
 * The ticket number carried by a router state, or null when there is none or it is not a positive whole
 * number. The browser keeps the state across a reload; a visit with no state (a bookmark, a typed address)
 * gets the neutral confirmation, never a number the customer did not just receive.
 */
export function ticketNumberFromState(state: unknown): number | null {
  if (typeof state !== 'object' || state === null) return null;
  const value = (state as { ticketNumber?: unknown }).ticketNumber;
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null;
}

const NUMBER_SLOT = '\u0001';

/**
 * Splits a sentence that contains the ticket number around it, so the number can be rendered in its own
 * `<bdi dir="ltr">`. `render` is the translator called with the placeholder instead of the number.
 */
export function splitAroundNumber(render: (slot: string) => string): { before: string; after: string } {
  const [before = '', ...rest] = render(NUMBER_SLOT).split(NUMBER_SLOT);
  return { before, after: rest.join('') };
}

/** First strong isolate: keeps a file name of the other direction from reordering the sentence around it. */
export function isolateText(text: string): string {
  return `\u2068${text}\u2069`;
}
