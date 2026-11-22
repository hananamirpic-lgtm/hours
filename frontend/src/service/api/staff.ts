/**
 * The staff client of the Ticket_API: one typed function per endpoint under `/api/service/staff`, plus the
 * shared photo route (design "Staff endpoints"; backend `api/staff_router.py` and `api/photo_router.py`).
 *
 * It sits on the existing `apiFetch`, so the staff access token, the base URL and the refresh behaviour are
 * the ones the Hours screens use. It never touches the customer token (`portalFetch` owns that).
 *
 * Error reading. `apiFetch` throws an `ApiError` that carries `detail.error.code` but drops the `fields`
 * map, and the management forms need both (a 422 names every failing field, R3.5, R16.6, R19.5). So this
 * module asks `apiFetch` to hand back the body of any 4xx or 5xx answer (`acceptStatuses`) and reads the
 * envelope itself with `parseErrorBody`, the same reader the portal client uses. A body that has a `detail`
 * (or `error`) key is an error answer; every success body of the Ticket_API is an object without those keys.
 * An error answer with no readable body (a proxy page, an empty body) is the code `generic`, a request that
 * got no answer at all is `network`.
 *
 * Every failure is thrown as a `StaffApiError { code, fields }`. Query helpers and body checks are pure and
 * exported for the tests.
 */
import { ApiError, apiFetch, apiFetchBlob, type RequestOptions } from '@/api/client';
import { ABORTED_CODE, GENERIC_CODE, NETWORK_CODE, parseErrorBody } from './portalFetch';
import type {
  CategoryBody,
  CategoryDeleteResult,
  CustomerBody,
  CustomerListQuery,
  FormSettings,
  HistoryEntryBody,
  ReportQuery,
  StaffCategory,
  StaffCustomer,
  StaffCustomerPage,
  StaffDashboard,
  StaffHistoryEntry,
  StaffLabels,
  StaffReport,
  StaffTicketDetail,
  StaffTicketPage,
  StatusChangeBody,
  TicketListQuery,
} from './staffTypes';
import type { FormLabelKey, LabelTexts } from './formTypes';

export * from './staffTypes';

/** The staff endpoints sit under the Ticket_API prefix `/service`, then `/staff`. */
export const STAFF_PREFIX = '/service/staff';
/** The shared photo route, served to staff and to the owning customer. */
export const SERVICE_PREFIX = '/service';

/** The code of a ticket staff 401 (no token, expired token). Sign-in is the staff app's own. */
export const STAFF_UNAUTHENTICATED_CODES: readonly string[] = ['not_authenticated', 'invalid_token'];

/** Every failure of a staff request. `fields` maps a failing form field to its code; it is empty when none. */
export class StaffApiError extends Error {
  constructor(
    readonly code: string,
    readonly fields: Readonly<Record<string, string>> = {},
  ) {
    super(`Staff API error ${code}`);
    this.name = 'StaffApiError';
  }
}

export function isStaffApiError(value: unknown): value is StaffApiError {
  return value instanceof StaffApiError;
}

// --- pure helpers ----------------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** True when a parsed body is an error answer: an object with a `detail` or an `error` key. */
export function isErrorBody(body: unknown): boolean {
  return isRecord(body) && ('detail' in body || 'error' in body);
}

/**
 * The error of a body returned for an accepted error status. `null` and `undefined` (a body that was not
 * JSON, or was empty) give `generic`; an object without a code gives `generic` too.
 */
export function staffErrorFromBody(body: unknown): StaffApiError {
  const parsed = parseErrorBody(body);
  return new StaffApiError(parsed.code, parsed.fields);
}

/** Every 4xx and 5xx status, so `apiFetch` returns the body instead of throwing without `fields`. */
const ERROR_STATUSES: readonly number[] = Array.from({ length: 200 }, (_, index) => 400 + index);

/** True when `error` says the staff session has ended (a 401 from the Ticket_API). */
export function isStaffUnauthenticated(error: unknown): boolean {
  return error instanceof StaffApiError && STAFF_UNAUTHENTICATED_CODES.includes(error.code);
}

type QueryValue = string | number | boolean | undefined;

/**
 * A query string (with the leading `?`, or empty) from named values. `undefined`, `false` and blank
 * strings are left out, so an unset filter is never sent. A list value repeats its name.
 */
export function buildQuery(params: ReadonlyArray<readonly [string, QueryValue | readonly string[]]>): string {
  const search = new URLSearchParams();
  for (const [name, value] of params) {
    if (value === undefined || value === false) continue;
    if (Array.isArray(value)) {
      for (const item of value as readonly string[]) search.append(name, item);
      continue;
    }
    if (typeof value === 'string') {
      if (value.trim() === '') continue;
      search.append(name, value);
    } else if (value === true) {
      search.append(name, 'true');
    } else {
      search.append(name, String(value));
    }
  }
  const text = search.toString();
  return text === '' ? '' : `?${text}`;
}

/** `GET /staff/tickets` query. The search text is sent as typed; the server trims and validates it. */
export function ticketListQuery(query: TicketListQuery = {}): string {
  return buildQuery([
    ['page', query.page],
    ['search', query.search],
    ['status', query.status === undefined || query.status.length === 0 ? undefined : query.status],
    ['category_id', query.categoryId],
    ['city', query.city],
    ['urgent_only', query.urgentOnly],
    ['date_from', query.dateFrom],
    ['date_to', query.dateTo],
    ['urgent_first', query.urgentFirst],
  ]);
}

/** `GET /staff/customers` query. `active` true or false filters; undefined means every customer. */
export function customerListQuery(query: CustomerListQuery = {}): string {
  return buildQuery([
    ['page', query.page],
    ['search', query.search],
    ['active', query.active === undefined ? undefined : query.active ? 'true' : 'false'],
  ]);
}

/** `GET /staff/reports` query. */
export function reportQuery(query: ReportQuery = {}): string {
  return buildQuery([
    ['page', query.page],
    ['date_from', query.dateFrom],
    ['date_to', query.dateTo],
    ['city', query.city],
    ['category_id', query.categoryId],
  ]);
}

/** The query keys of the management pages. All sit under `['service-staff']`. */
export const staffKeys = {
  all: ['service-staff'] as const,
  dashboard: ['service-staff', 'dashboard'] as const,
  tickets: ['service-staff', 'tickets'] as const,
  ticketList: (query: TicketListQuery) => ['service-staff', 'tickets', 'list', query] as const,
  ticket: (ticketNumber: number) => ['service-staff', 'tickets', 'detail', ticketNumber] as const,
  customers: ['service-staff', 'customers'] as const,
  customerList: (query: CustomerListQuery) => ['service-staff', 'customers', 'list', query] as const,
  report: (query: ReportQuery) => ['service-staff', 'report', query] as const,
  categories: ['service-staff', 'categories'] as const,
  settings: ['service-staff', 'settings'] as const,
  labels: ['service-staff', 'labels'] as const,
};

// --- the transport ---------------------------------------------------------------------------------

interface StaffRequest {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  json?: unknown;
  signal?: AbortSignal;
}

async function send(path: string, request: StaffRequest): Promise<unknown> {
  const init: RequestOptions = {
    method: request.method ?? 'GET',
    acceptStatuses: [...ERROR_STATUSES],
  };
  if (request.json !== undefined) init.body = JSON.stringify(request.json);
  if (request.signal) init.signal = request.signal;
  try {
    return await apiFetch<unknown>(`${STAFF_PREFIX}${path}`, init);
  } catch (caught) {
    if (caught instanceof ApiError) throw new StaffApiError(caught.code ?? GENERIC_CODE);
    // A fetch that never got an answer (offline, DNS) rejects with a TypeError; a cancelled one with an AbortError.
    throw new StaffApiError(request.signal?.aborted ? ABORTED_CODE : NETWORK_CODE);
  }
}

/** A JSON endpoint: resolves to the body, or throws `StaffApiError` for an error answer or an unusable body. */
async function staffJson<T>(path: string, request: StaffRequest = {}): Promise<T> {
  const body = await send(path, request);
  if (isErrorBody(body) || body === null || body === undefined) throw staffErrorFromBody(body);
  return body as T;
}

/** An endpoint with no body on success (204). Any other answer is an error. */
async function staffVoid(path: string, request: StaffRequest = {}): Promise<void> {
  const body = await send(path, request);
  if (body !== undefined) throw staffErrorFromBody(body);
}

const enc = encodeURIComponent;

// --- dashboard -------------------------------------------------------------------------------------

export function getDashboard(signal?: AbortSignal): Promise<StaffDashboard> {
  return staffJson<StaffDashboard>('/dashboard', signal ? { signal } : {});
}

// --- tickets ---------------------------------------------------------------------------------------

export function listTickets(query: TicketListQuery = {}, signal?: AbortSignal): Promise<StaffTicketPage> {
  return staffJson<StaffTicketPage>(`/tickets${ticketListQuery(query)}`, signal ? { signal } : {});
}

export function getTicket(ticketNumber: number | string, signal?: AbortSignal): Promise<StaffTicketDetail> {
  return staffJson<StaffTicketDetail>(`/tickets/${enc(String(ticketNumber))}`, signal ? { signal } : {});
}

/** `POST /staff/tickets/{n}/status`. Send the status the page last displayed as `expected_status`. */
export function changeTicketStatus(ticketNumber: number, body: StatusChangeBody): Promise<StaffTicketDetail> {
  return staffJson<StaffTicketDetail>(`/tickets/${ticketNumber}/status`, { method: 'POST', json: body });
}

/** `PUT /staff/tickets/{n}/assigned-technician`. An empty name clears the assignment. */
export function setAssignedTechnician(ticketNumber: number, name: string): Promise<StaffTicketDetail> {
  return staffJson<StaffTicketDetail>(`/tickets/${ticketNumber}/assigned-technician`, {
    method: 'PUT',
    json: { assigned_technician_name: name },
  });
}

/** `POST /staff/tickets/{n}/history`. An absent technician name becomes the acting username. */
export function addHistoryEntry(ticketNumber: number, body: HistoryEntryBody): Promise<StaffHistoryEntry> {
  return staffJson<StaffHistoryEntry>(`/tickets/${ticketNumber}/history`, { method: 'POST', json: body });
}

/** `PUT /staff/tickets/{n}/history/{id}`. Replaces the text and the technician name. */
export function editHistoryEntry(
  ticketNumber: number,
  entryId: string,
  body: HistoryEntryBody,
): Promise<StaffHistoryEntry> {
  return staffJson<StaffHistoryEntry>(`/tickets/${ticketNumber}/history/${enc(entryId)}`, {
    method: 'PUT',
    json: body,
  });
}

/**
 * `GET /service/tickets/{n}/photo`, the route shared with the owning customer. The staff bearer token is
 * sent by `apiFetchBlob`. The result is a JPEG blob; show it through `URL.createObjectURL` and revoke the URL
 * when the page unmounts.
 */
export async function fetchTicketPhoto(ticketNumber: number, signal?: AbortSignal): Promise<Blob> {
  try {
    return await apiFetchBlob(`${SERVICE_PREFIX}/tickets/${ticketNumber}/photo`, signal ? { signal } : {});
  } catch (caught) {
    if (caught instanceof ApiError) throw new StaffApiError(caught.code ?? GENERIC_CODE);
    throw new StaffApiError(signal?.aborted ? ABORTED_CODE : NETWORK_CODE);
  }
}

// --- customers -------------------------------------------------------------------------------------

export function listCustomers(query: CustomerListQuery = {}, signal?: AbortSignal): Promise<StaffCustomerPage> {
  return staffJson<StaffCustomerPage>(`/customers${customerListQuery(query)}`, signal ? { signal } : {});
}

export function createCustomer(body: CustomerBody): Promise<StaffCustomer> {
  return staffJson<StaffCustomer>('/customers', { method: 'POST', json: body });
}

/** `PUT /staff/customers/{id}`: replaces the four editable values. */
export function updateCustomer(customerId: string, body: CustomerBody): Promise<StaffCustomer> {
  return staffJson<StaffCustomer>(`/customers/${enc(customerId)}`, { method: 'PUT', json: body });
}

export function deactivateCustomer(customerId: string): Promise<StaffCustomer> {
  return staffJson<StaffCustomer>(`/customers/${enc(customerId)}/deactivate`, { method: 'POST' });
}

export function reactivateCustomer(customerId: string): Promise<StaffCustomer> {
  return staffJson<StaffCustomer>(`/customers/${enc(customerId)}/reactivate`, { method: 'POST' });
}

/** `POST /staff/customers/{id}/reset-password`: 204, no body; the new password is never returned. */
export function resetCustomerPassword(customerId: string): Promise<void> {
  return staffVoid(`/customers/${enc(customerId)}/reset-password`, { method: 'POST' });
}

// --- reports ---------------------------------------------------------------------------------------

export function getReport(query: ReportQuery = {}, signal?: AbortSignal): Promise<StaffReport> {
  return staffJson<StaffReport>(`/reports${reportQuery(query)}`, signal ? { signal } : {});
}

// --- configuration (admin and operations_admin; a technician gets 403 `insufficient_role`) ---------

/** `GET /staff/categories`: every category, active and inactive, oldest first. Open to all ticket staff. */
export async function listCategories(signal?: AbortSignal): Promise<readonly StaffCategory[]> {
  const body = await staffJson<{ items: readonly StaffCategory[] }>('/categories', signal ? { signal } : {});
  return body.items;
}

export function createCategory(body: CategoryBody): Promise<StaffCategory> {
  return staffJson<StaffCategory>('/categories', { method: 'POST', json: body });
}

/** `PUT /staff/categories/{id}`: replaces both names. */
export function renameCategory(categoryId: string, body: CategoryBody): Promise<StaffCategory> {
  return staffJson<StaffCategory>(`/categories/${enc(categoryId)}`, { method: 'PUT', json: body });
}

export function deactivateCategory(categoryId: string): Promise<StaffCategory> {
  return staffJson<StaffCategory>(`/categories/${enc(categoryId)}/deactivate`, { method: 'POST' });
}

export function reactivateCategory(categoryId: string): Promise<StaffCategory> {
  return staffJson<StaffCategory>(`/categories/${enc(categoryId)}/reactivate`, { method: 'POST' });
}

/** `DELETE /staff/categories/{id}`: `removed`, or `deactivated` when tickets use the category. */
export async function deleteCategory(categoryId: string): Promise<CategoryDeleteResult> {
  const body = await staffJson<{ result: CategoryDeleteResult }>(`/categories/${enc(categoryId)}`, {
    method: 'DELETE',
  });
  return body.result;
}

/** `GET /staff/settings`. */
export async function getSettings(signal?: AbortSignal): Promise<FormSettings> {
  const body = await staffJson<{ settings: FormSettings }>('/settings', signal ? { signal } : {});
  return body.settings;
}

/** `PUT /staff/settings`: both options together, as strict booleans. */
export async function updateSettings(settings: FormSettings): Promise<FormSettings> {
  const body = await staffJson<{ settings: FormSettings }>('/settings', {
    method: 'PUT',
    json: { description_required: settings.description_required, photo_upload_enabled: settings.photo_upload_enabled },
  });
  return body.settings;
}

/**
 * `PUT /staff/labels`: replaces the labels named in `labels`; the others keep theirs. The answer holds all
 * seven labels. A 422 carries `fields` keyed `"<field>.he"` or `"<field>.en"` (and the bare field key for
 * `unknown_field`).
 */
export async function updateLabels(
  labels: Partial<Record<FormLabelKey, LabelTexts>>,
): Promise<StaffLabels> {
  const body = await staffJson<{ labels: StaffLabels }>('/labels', { method: 'PUT', json: { labels } });
  return body.labels;
}

/**
 * The seven current labels. There is no `GET /staff/labels` and `GET /form-config` is a customer endpoint,
 * so this sends `PUT /staff/labels` with no labels: the service validates nothing, stores nothing and
 * answers with every label (`services/config_admin.update_labels` returns `get_labels` when it has no
 * failures, and writes only the keys it was given).
 */
export function readLabels(): Promise<StaffLabels> {
  return updateLabels({});
}
