/**
 * Site endpoints (Requirement 6, 7).
 *
 * The billing rate, its overtime companion and the whole `site_rates` history are optional on the
 * response because a site manager's payload has them stripped by redaction (Requirement 2.5, 17.7);
 * their absence means "not permitted to see", which the card reads directly. Assignments are not
 * billing data, so `employee_ids` is present for every reader.
 */

import type {
  Paginated,
  SiteCreate,
  SiteListItem,
  SiteRateInput,
  SiteResponse,
  SiteStatus,
  SiteUpdate,
} from './types';

import { apiFetch, apiFetchBlob } from './client';

export const siteKeys = {
  all: ['sites'] as const,
  list: (params: SiteListParams) => ['sites', 'list', params] as const,
  detail: (id: string) => ['sites', 'detail', id] as const,
};

export interface SiteListParams {
  status?: SiteStatus | null;
  limit: number;
  offset: number;
}

export const listSites = (params: SiteListParams): Promise<Paginated<SiteListItem>> => {
  const search = new URLSearchParams({ limit: String(params.limit), offset: String(params.offset) });
  if (params.status) {
    search.set('status', params.status);
  }
  return apiFetch<Paginated<SiteListItem>>(`/sites?${search.toString()}`);
};

export const getSite = (id: string): Promise<SiteResponse> => apiFetch<SiteResponse>(`/sites/${id}`);

export const createSite = (payload: SiteCreate): Promise<SiteResponse> =>
  apiFetch<SiteResponse>('/sites', { method: 'POST', body: JSON.stringify(payload) });

export const updateSite = (id: string, payload: SiteUpdate): Promise<SiteResponse> =>
  apiFetch<SiteResponse>(`/sites/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });

export const replaceSiteRates = (id: string, rates: SiteRateInput[]): Promise<SiteResponse> =>
  apiFetch<SiteResponse>(`/sites/${id}/rates`, { method: 'PUT', body: JSON.stringify({ rates }) });

export const setSiteEmployees = (id: string, employeeIds: string[]): Promise<SiteResponse> =>
  apiFetch<SiteResponse>(`/sites/${id}/employees`, {
    method: 'PUT',
    body: JSON.stringify({ employee_ids: employeeIds }),
  });


/** The two codes a separate-mode site carries; a unified site ignores this. */
export type QrAction = 'check_in' | 'check_out';

/**
 * GET /api/sites/{id}/qr — the site's printable QR as a Blob (Requirement 8.5). `format` picks PNG
 * or PDF; `action` names which code on a separate-mode site (omit it for a unified site).
 */
export const downloadSiteQr = (
  id: string,
  format: 'png' | 'pdf',
  action?: QrAction,
): Promise<Blob> => {
  const search = new URLSearchParams({ format });
  if (action) {
    search.set('action', action);
  }
  return apiFetchBlob(`/sites/${id}/qr?${search.toString()}`);
};

/**
 * POST /api/sites/{id}/qr/regenerate — rotate the site's QR token (admin only). Every previously
 * printed code stops working on the next scan (Requirement 8.6, 8.7). Returns the updated site card.
 */
export const regenerateSiteQr = (id: string): Promise<SiteResponse> =>
  apiFetch<SiteResponse>(`/sites/${id}/qr/regenerate`, { method: 'POST' });
