// The API calls behind the ticket form (task 23.3; R9, R11.1): the form configuration, the customer's
// own record for prefill, and the submission. All three go through `portalFetch`, so the customer
// token, the 10 s read timeout and the 120 s submit timeout (R9.12) are the client's.
//
// The submission is multipart: the browser sets the boundary, so no Content-Type is set here.
import type { CustomerProfile } from '../portal/session';
import {
  GENERIC_CODE,
  PORTAL_SUBMIT_TIMEOUT_MS,
  PortalApiError,
  portalFetch,
  type PortalRequestOptions,
} from './portalFetch';
import type { FormConfig, TicketCreated } from './formTypes';

/** Under the `['portal']` root, so signing out drops it with every other portal query. */
export const formConfigKey = ['portal', 'form-config'] as const;

function withSignal(signal: AbortSignal | undefined): PortalRequestOptions {
  return signal ? { signal } : {};
}

/** `GET /service/form-config`: active categories, the field labels and the two options. */
export function getFormConfig(signal?: AbortSignal): Promise<FormConfig> {
  return portalFetch<FormConfig>('/form-config', withSignal(signal));
}

/** `GET /service/auth/me`: the Customer_Record the form prefills from (R9.2). Same shape the session gate reads. */
export function getMe(signal?: AbortSignal): Promise<CustomerProfile> {
  return portalFetch<CustomerProfile>('/auth/me', withSignal(signal));
}

/**
 * `POST /service/tickets` with the multipart body. Resolves to the new Ticket_Number; rejects with a
 * `PortalApiError` (status 0 `timeout` after 120 s, status 0 `network` when there is no response).
 */
export async function createTicket(formData: FormData): Promise<number> {
  const created = await portalFetch<TicketCreated>('/tickets', {
    method: 'POST',
    body: formData,
    kind: 'submit',
    timeoutMs: PORTAL_SUBMIT_TIMEOUT_MS,
  });
  const number: unknown = created?.ticket_number;
  // A 201 whose body has no usable number: the ticket may exist, so the screen treats it like a lost answer.
  if (typeof number !== 'number' || !Number.isSafeInteger(number)) throw new PortalApiError(0, GENERIC_CODE);
  return number;
}
