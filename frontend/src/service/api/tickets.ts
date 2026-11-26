// The customer ticket list endpoint (R12.1, R12.9): `GET /service/tickets?page=N`.
//
// Only what the list needs lives here. The detail, cancel and create calls are other modules
// (`ticketDetail.ts`, `ticketCreate.ts`). A 401 is handled inside `portalFetch` (it clears the token),
// so nothing here reacts to it.
import { GENERIC_CODE, PortalApiError, portalFetch, type PortalRequestOptions } from './portalFetch';
import type { TicketPage, TicketRow } from './ticketTypes';

export type { TicketCategory, TicketPage, TicketRow } from './ticketTypes';

/** Query keys of the customer tickets. All of them sit under the `['portal']` root of `portalKeys`. */
export const ticketKeys = {
  /** Every ticket query, list and detail. */
  all: ['portal', 'tickets'] as const,
  /** Every page of the list: invalidate this after a cancellation or a new ticket. */
  lists: ['portal', 'tickets', 'list'] as const,
  list: (page: number) => ['portal', 'tickets', 'list', page] as const,
};

const STATUSES: ReadonlySet<string> = new Set(['received', 'in_progress', 'closed', 'cancelled']);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isTicketRow(value: unknown): value is TicketRow {
  if (!isRecord(value)) return false;
  const category = value.category;
  return (
    typeof value.ticket_number === 'number' &&
    typeof value.created_date === 'string' &&
    typeof value.apartment === 'string' &&
    typeof value.status === 'string' &&
    STATUSES.has(value.status) &&
    typeof value.urgent === 'boolean' &&
    isRecord(category) &&
    typeof category.name_he === 'string' &&
    typeof category.name_en === 'string'
  );
}

/** True when `value` has the shape of the list response. A reply that is not is treated as a failure. */
export function isTicketPage(value: unknown): value is TicketPage {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isTicketRow) &&
    typeof value.page === 'number' &&
    typeof value.page_size === 'number' &&
    typeof value.total === 'number'
  );
}

/** `GET /service/tickets?page={page}`. Throws `PortalApiError` on any failure, also on an unusable body. */
export async function listTickets(page: number, signal?: AbortSignal): Promise<TicketPage> {
  const options: PortalRequestOptions = signal ? { signal } : {};
  const body = await portalFetch<unknown>(`/tickets?page=${page}`, options);
  if (!isTicketPage(body)) throw new PortalApiError(200, GENERIC_CODE);
  return body;
}
