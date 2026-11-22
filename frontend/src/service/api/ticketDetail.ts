/**
 * Typed client of the customer ticket detail endpoints (R12.2, R12.11, R13, R11.9; design "Portal").
 *
 *   GET  /service/tickets/{n}           -> CustomerTicketDetail            404 ticket_not_found
 *   POST /service/tickets/{n}/cancel    -> CustomerTicketDetail (cancelled) 404, 409 ticket_status_changed,
 *                                          409 invalid_transition, 422 invalid_expected_status
 *   GET  /service/tickets/{n}/photo     -> image/jpeg bytes, as a Blob      404 ticket_not_found, 503
 *   GET  /service/form-config           -> only the Field_Labels are read here
 *
 * Everything goes through `portalFetch` / `portalFetchBlob`, so the customer bearer header, the 10 s
 * timeout and the `PortalApiError` shape are the portal's. The photo is fetched as a Blob with the bearer
 * header (an `<img src>` could not send it) and shown through `URL.createObjectURL`.
 */
import {
  portalFetch,
  portalFetchBlob,
  PortalApiError,
  GENERIC_CODE,
  type PortalRequestOptions,
} from './portalFetch';
import type { TicketStatus } from '../types';

export const TICKET_STATUSES: readonly TicketStatus[] = ['received', 'in_progress', 'closed', 'cancelled'];

/** The seven Field_Label keys of `GET /form-config` (backend `FieldLabelKey`). */
export const FIELD_LABEL_KEYS = ['city', 'apartment', 'phone', 'category', 'description', 'photo', 'urgent'] as const;
export type FieldLabelKey = (typeof FIELD_LABEL_KEYS)[number];

/** The Hebrew and English text of one label. */
export interface LabelTexts {
  readonly he: string;
  readonly en: string;
}

/** The labels a form-config answer carries, by field key. Any key may be missing. */
export type FormLabels = Readonly<Partial<Record<FieldLabelKey, LabelTexts>>>;

export interface CategoryRef {
  readonly id: string;
  readonly name_he: string;
  readonly name_en: string;
}

/** `GET /tickets/{n}` and the answer of a cancellation (backend `CustomerTicketDetail`). */
export interface CustomerTicketDetail {
  readonly ticket_number: number;
  readonly category: CategoryRef;
  /** `YYYY-MM-DD`, computed by the server. */
  readonly created_date: string;
  readonly status: TicketStatus;
  readonly urgent: boolean;
  readonly city: string;
  readonly apartment: string;
  readonly phone: string;
  readonly description: string;
  readonly has_photo: boolean;
}

/** Query keys. They start with the portal root, so sign-out drops them with every other portal query. */
export const ticketDetailKeys = {
  detail: (ticketNumber: number) => ['portal', 'tickets', ticketNumber, 'detail'] as const,
  photo: (ticketNumber: number) => ['portal', 'tickets', ticketNumber, 'photo'] as const,
  labels: ['portal', 'ticket-detail', 'labels'] as const,
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isStatus(value: unknown): value is TicketStatus {
  return typeof value === 'string' && (TICKET_STATUSES as readonly string[]).includes(value);
}

/**
 * Checks the shape of a detail answer. A success status with a body of another shape (a proxy page that
 * happens to be JSON, an older API) is not an answer the screen can show, so it is the `generic` error
 * and the screen shows its retry state instead of crashing on a missing field.
 */
export function parseTicketDetail(raw: unknown): CustomerTicketDetail {
  if (
    !isRecord(raw) ||
    typeof raw.ticket_number !== 'number' ||
    !isRecord(raw.category) ||
    typeof raw.category.name_he !== 'string' ||
    typeof raw.category.name_en !== 'string' ||
    typeof raw.created_date !== 'string' ||
    !isStatus(raw.status) ||
    typeof raw.urgent !== 'boolean' ||
    typeof raw.city !== 'string' ||
    typeof raw.apartment !== 'string' ||
    typeof raw.phone !== 'string' ||
    typeof raw.description !== 'string' ||
    typeof raw.has_photo !== 'boolean'
  ) {
    throw new PortalApiError(200, GENERIC_CODE);
  }
  return raw as unknown as CustomerTicketDetail;
}

/** The label texts of a form-config answer; entries that are not `{he, en}` strings are left out. */
export function parseFormLabels(raw: unknown): FormLabels {
  const labels = isRecord(raw) && isRecord(raw.labels) ? raw.labels : {};
  const result: Partial<Record<FieldLabelKey, LabelTexts>> = {};
  for (const key of FIELD_LABEL_KEYS) {
    const entry = labels[key];
    if (isRecord(entry) && typeof entry.he === 'string' && typeof entry.en === 'string') {
      result[key] = { he: entry.he, en: entry.en };
    }
  }
  return result;
}

function ticketPath(ticketNumber: number, suffix = ''): string {
  // A number, not caller text: nothing to escape, and nothing but digits reaches the URL.
  return `/tickets/${String(Math.trunc(ticketNumber))}${suffix}`;
}

/** `GET /service/tickets/{n}`. */
export async function getTicket(ticketNumber: number, signal?: AbortSignal): Promise<CustomerTicketDetail> {
  const raw = await portalFetch<unknown>(ticketPath(ticketNumber), signal ? { signal } : {});
  return parseTicketDetail(raw);
}

/**
 * `POST /service/tickets/{n}/cancel` with the last-seen status (R13.1, R13.7). The answer is the updated
 * ticket, with the status `cancelled`.
 */
export async function cancelTicket(
  ticketNumber: number,
  expectedStatus: TicketStatus,
  signal?: AbortSignal,
): Promise<CustomerTicketDetail> {
  const raw = await portalFetch<unknown>(ticketPath(ticketNumber, '/cancel'), {
    method: 'POST',
    json: { expected_status: expectedStatus },
    ...(signal ? { signal } : {}),
  });
  return parseTicketDetail(raw);
}

/**
 * `GET /service/tickets/{n}/photo` as a Blob, fetched with the bearer header. `portalFetchBlob` already
 * supports a binary success body, so the shared client needed no change. A body that is not an image is
 * the `generic` error, so a proxy page is never handed to `URL.createObjectURL`.
 */
export async function getTicketPhoto(
  ticketNumber: number,
  signal?: AbortSignal,
  /** Tests inject a token store; the portal uses the customer token store. */
  tokens?: PortalRequestOptions['tokens'],
): Promise<Blob> {
  const blob = await portalFetchBlob(ticketPath(ticketNumber, '/photo'), {
    ...(signal ? { signal } : {}),
    ...(tokens ? { tokens } : {}),
  });
  if (blob.size === 0 || !blob.type.toLowerCase().startsWith('image/')) {
    throw new PortalApiError(200, GENERIC_CODE);
  }
  return blob;
}

/** `GET /service/form-config`, reduced to the Field_Labels the detail screen shows. */
export async function getFormLabels(signal?: AbortSignal): Promise<FormLabels> {
  const raw = await portalFetch<unknown>('/form-config', signal ? { signal } : {});
  return parseFormLabels(raw);
}
