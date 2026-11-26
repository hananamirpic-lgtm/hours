/**
 * The client of the second-round configuration endpoints (task 30.7): the settlement list
 * (`/staff/localities`), the replaceable-parts list (`/staff/parts`) and the branding (`/staff/branding`,
 * `/branding/logo`). It sits beside `staff.ts` and reuses its error class and error-body reading, so every
 * failure is a `StaffApiError { code, fields }` exactly as for the issue categories.
 *
 * It sits on the existing `apiFetch`, so the staff access token, the base URL and the refresh behaviour are
 * the ones the Hours screens use. A multipart body (the branding form) is passed as `FormData`; `apiFetch`
 * leaves Content-Type unset for it, so the browser adds the boundary. No header is set by hand here.
 */
import { ApiError, apiFetch, apiFetchBlob, type RequestOptions } from '@/api/client';
import { ABORTED_CODE, GENERIC_CODE, NETWORK_CODE } from './portalFetch';
import { SERVICE_PREFIX, STAFF_PREFIX, StaffApiError, isErrorBody, staffErrorFromBody } from './staff';
import type {
  BrandingChange,
  NamedItemBody,
  NamedItemDeleteResult,
  NamedListKind,
  StaffBranding,
  StaffNamedItem,
} from './staffListsTypes';

export * from './staffListsTypes';

/** The query keys of the three screens. All sit under `['service-staff']`, like `staffKeys`. */
export const staffListKeys = {
  localities: ['service-staff', 'localities'] as const,
  parts: ['service-staff', 'parts'] as const,
  branding: ['service-staff', 'branding'] as const,
  brandingLogo: ['service-staff', 'branding', 'logo'] as const,
};

/** The query key of a named list. */
export function namedListKey(kind: NamedListKind): readonly string[] {
  return staffListKeys[kind];
}

// --- the transport ---------------------------------------------------------------------------------

/** Every 4xx and 5xx status, so `apiFetch` returns the body instead of throwing without `fields`. */
const ERROR_STATUSES: readonly number[] = Array.from({ length: 200 }, (_, index) => 400 + index);

interface ListRequest {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  json?: unknown;
  /** A multipart body. */
  form?: FormData;
  signal?: AbortSignal;
}

/** A JSON endpoint: resolves to the body, or throws `StaffApiError` for an error answer or an unusable body. */
async function listJson<T>(path: string, request: ListRequest = {}): Promise<T> {
  const init: RequestOptions = {
    method: request.method ?? 'GET',
    acceptStatuses: [...ERROR_STATUSES],
  };
  if (request.json !== undefined) init.body = JSON.stringify(request.json);
  else if (request.form !== undefined) init.body = request.form;
  if (request.signal) init.signal = request.signal;
  let body: unknown;
  try {
    body = await apiFetch<unknown>(`${STAFF_PREFIX}${path}`, init);
  } catch (caught) {
    if (caught instanceof ApiError) throw new StaffApiError(caught.code ?? GENERIC_CODE);
    // A fetch that never got an answer (offline, DNS) rejects with a TypeError; a cancelled one with an AbortError.
    throw new StaffApiError(request.signal?.aborted ? ABORTED_CODE : NETWORK_CODE);
  }
  if (isErrorBody(body) || body === null || body === undefined) throw staffErrorFromBody(body);
  return body as T;
}

const enc = encodeURIComponent;

// --- settlements and replaceable parts (R30.1, R29.6) -----------------------------------------------

/** The five calls of one named list. The two lists differ only in the path and in their error codes. */
export interface NamedListApi {
  /** Every item, active and inactive, oldest first. Configuration staff only. */
  readonly list: (signal?: AbortSignal) => Promise<readonly StaffNamedItem[]>;
  readonly create: (body: NamedItemBody) => Promise<StaffNamedItem>;
  /** Replaces both names. */
  readonly rename: (id: string, body: NamedItemBody) => Promise<StaffNamedItem>;
  readonly deactivate: (id: string) => Promise<StaffNamedItem>;
  readonly reactivate: (id: string) => Promise<StaffNamedItem>;
  /** `removed`, or `deactivated` when a ticket (or, for settlements, a customer) uses the item. */
  readonly remove: (id: string) => Promise<NamedItemDeleteResult>;
}

function namedListApi(resource: NamedListKind): NamedListApi {
  const base = `/${resource}`;
  return {
    list: async (signal) => {
      const body = await listJson<{ items: readonly StaffNamedItem[] }>(base, signal ? { signal } : {});
      return body.items;
    },
    create: (body) => listJson<StaffNamedItem>(base, { method: 'POST', json: body }),
    rename: (id, body) => listJson<StaffNamedItem>(`${base}/${enc(id)}`, { method: 'PUT', json: body }),
    deactivate: (id) => listJson<StaffNamedItem>(`${base}/${enc(id)}/deactivate`, { method: 'POST' }),
    reactivate: (id) => listJson<StaffNamedItem>(`${base}/${enc(id)}/reactivate`, { method: 'POST' }),
    remove: async (id) => {
      const body = await listJson<{ result: NamedItemDeleteResult }>(`${base}/${enc(id)}`, { method: 'DELETE' });
      return body.result;
    },
  };
}

/** `/staff/localities`: the settlement list. */
export const localitiesApi: NamedListApi = namedListApi('localities');

/** `/staff/parts`: the replaceable-parts list. */
export const partsApi: NamedListApi = namedListApi('parts');

/** The client of a list by its kind. */
export function namedListClient(kind: NamedListKind): NamedListApi {
  return kind === 'localities' ? localitiesApi : partsApi;
}

// --- branding (R28.1, R28.2) ------------------------------------------------------------------------

/** `GET /staff/branding`: the company name and whether a logo is stored. Configuration staff only. */
export function getBranding(signal?: AbortSignal): Promise<StaffBranding> {
  return listJson<StaffBranding>('/branding', signal ? { signal } : {});
}

/**
 * The multipart form of `PUT /staff/branding`: only the parts of `change`, in the order of the server's form
 * (`company_name`, `logo`, `remove_name`, `remove_logo`). The flags are the text `true`, which is the only
 * value the server accepts. The file goes under a fixed name, so the name of a local file never leaves the
 * browser.
 */
export function brandingFormData(change: BrandingChange): FormData {
  const form = new FormData();
  if (change.companyName !== undefined) form.append('company_name', change.companyName);
  if (change.logo !== undefined) form.append('logo', change.logo, 'logo');
  if (change.removeName === true) form.append('remove_name', 'true');
  if (change.removeLogo === true) form.append('remove_logo', 'true');
  return form;
}

/**
 * `PUT /staff/branding` as `multipart/form-data`. A 422 carries `fields` keyed `company_name`, `logo` or
 * `body`; a logo over 1 MiB is 413 `logo_too_large` (and a whole body over 2 MiB 413 `request_too_large`).
 * The answer is the branding as stored.
 */
export function updateBranding(change: BrandingChange): Promise<StaffBranding> {
  return listJson<StaffBranding>('/branding', { method: 'PUT', form: brandingFormData(change) });
}

/**
 * `GET /service/branding/logo` as a Blob, fetched with the staff bearer header (an `<img src>` could not
 * send it). Resolves to null when no logo is stored (404 with an empty body: the logo was just removed). A body that is not an
 * image is the `generic` error, so a proxy page is never handed to `URL.createObjectURL`. Show the blob
 * through an object URL and revoke it when it changes and on unmount.
 */
export async function fetchBrandingLogo(signal?: AbortSignal): Promise<Blob | null> {
  let blob: Blob;
  try {
    blob = await apiFetchBlob(`${SERVICE_PREFIX}/branding/logo`, signal ? { signal } : {});
  } catch (caught) {
    if (caught instanceof ApiError) {
      if (caught.status === 404) return null;
      throw new StaffApiError(caught.code ?? GENERIC_CODE);
    }
    throw new StaffApiError(signal?.aborted ? ABORTED_CODE : NETWORK_CODE);
  }
  if (!blob.type.startsWith('image/')) throw new StaffApiError(GENERIC_CODE);
  return blob;
}
