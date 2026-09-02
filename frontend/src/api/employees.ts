/**
 * Employee endpoints (Requirement 3, 4, 7).
 *
 * Every call is locale-neutral: the server returns ISO dates and raw decimals, and the screens format
 * them. The card, its rate history and the wage-bearing responses are typed as one `EmployeeResponse`
 * whose wage fields are optional, because a site manager's payload has them removed by redaction
 * rather than nulled — an absent `hourly_wage` means "not permitted", which the card reads directly.
 */

import type {
  DocumentListResponse,
  DocumentResponse,
  DocumentType,
  DocumentUploadTicket,
  EmployeeCreate,
  EmployeeListItem,
  EmployeeRateInput,
  EmployeeResponse,
  EmployeeSitesResponse,
  EmployeeStatus,
  EmployeeUpdate,
  Paginated,
} from './types';

import { apiFetch } from './client';

export const employeeKeys = {
  all: ['employees'] as const,
  list: (params: EmployeeListParams) => ['employees', 'list', params] as const,
  detail: (id: string) => ['employees', 'detail', id] as const,
  documents: (id: string) => ['employees', id, 'documents'] as const,
  sites: (id: string) => ['employees', id, 'sites'] as const,
};

export interface EmployeeListParams {
  status?: EmployeeStatus | null;
  limit: number;
  offset: number;
}

const query = (params: Record<string, string | number | null | undefined>): string => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== '') {
      search.set(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : '';
};

export const listEmployees = (params: EmployeeListParams): Promise<Paginated<EmployeeListItem>> =>
  apiFetch<Paginated<EmployeeListItem>>(
    `/employees${query({ status: params.status, limit: params.limit, offset: params.offset })}`,
  );

export const getEmployee = (id: string): Promise<EmployeeResponse> =>
  apiFetch<EmployeeResponse>(`/employees/${id}`);

export const createEmployee = (payload: EmployeeCreate): Promise<EmployeeResponse> =>
  apiFetch<EmployeeResponse>('/employees', { method: 'POST', body: JSON.stringify(payload) });

export const updateEmployee = (id: string, payload: EmployeeUpdate): Promise<EmployeeResponse> =>
  apiFetch<EmployeeResponse>(`/employees/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });

export const changeEmployeeStatus = (
  id: string,
  status: EmployeeStatus,
  reason?: string,
): Promise<EmployeeResponse> =>
  apiFetch<EmployeeResponse>(`/employees/${id}/status`, {
    method: 'PATCH',
    body: JSON.stringify({ status, ...(reason ? { reason } : {}) }),
  });

export const replaceEmployeeRates = (
  id: string,
  rates: EmployeeRateInput[],
): Promise<EmployeeResponse> =>
  apiFetch<EmployeeResponse>(`/employees/${id}/rates`, {
    method: 'PUT',
    body: JSON.stringify({ rates }),
  });

// --------------------------------------------------------------------------- documents

export const listEmployeeDocuments = (id: string): Promise<DocumentListResponse> =>
  apiFetch<DocumentListResponse>(`/employees/${id}/documents`);

export interface UploadInit {
  type: DocumentType;
  file_name: string;
  mime_type: string;
  size_bytes: number;
  expiry_date?: string | null;
}

export const createUploadUrl = (
  employeeId: string,
  init: UploadInit,
): Promise<DocumentUploadTicket> =>
  apiFetch<DocumentUploadTicket>(`/employees/${employeeId}/documents/upload-url`, {
    method: 'POST',
    body: JSON.stringify(init),
  });

export interface UploadComplete {
  file_key: string;
  type: DocumentType;
  file_name: string;
  mime_type: string;
  expiry_date?: string | null;
}

export const completeUpload = (
  employeeId: string,
  payload: UploadComplete,
): Promise<DocumentResponse> =>
  apiFetch<DocumentResponse>(`/employees/${employeeId}/documents`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });

export const getDocumentDownloadUrl = (documentId: string): Promise<{ url: string; expires_in_seconds: number }> =>
  apiFetch<{ url: string; expires_in_seconds: number }>(`/documents/${documentId}/download-url`);

/**
 * A short-lived signed URL for an employee's photo, or { url: null } when none is on file
 * (GET /api/employees/{id}/photo-url). Readable by the same roles that read the card; the card
 * renders the returned URL as an <img>. The photo lives in private storage, so the URL is short-lived.
 */
export const getEmployeePhotoUrl = (
  employeeId: string,
): Promise<{ url: string | null; expires_in_seconds: number }> =>
  apiFetch<{ url: string | null; expires_in_seconds: number }>(`/employees/${employeeId}/photo-url`);

export const deleteDocument = (documentId: string): Promise<void> =>
  apiFetch<void>(`/documents/${documentId}`, { method: 'DELETE' });

/**
 * PUT the file straight to private object storage using the presigned URL. This request does not go
 * through the API and carries no bearer token, so it is a bare `fetch` rather than `apiFetch`.
 */
export const putToStorage = async (
  ticket: DocumentUploadTicket,
  file: File | Blob,
): Promise<void> => {
  const response = await fetch(ticket.upload_url, {
    method: 'PUT',
    headers: ticket.required_headers,
    body: file,
  });
  if (!response.ok) {
    throw new Error(`Upload failed with ${response.status}`);
  }
};

// --------------------------------------------------------------------------- site assignment

export const getEmployeeSites = (id: string): Promise<EmployeeSitesResponse> =>
  apiFetch<EmployeeSitesResponse>(`/employees/${id}/sites`);

export const setEmployeeSites = (id: string, siteIds: string[]): Promise<EmployeeSitesResponse> =>
  apiFetch<EmployeeSitesResponse>(`/employees/${id}/sites`, {
    method: 'PUT',
    body: JSON.stringify({ site_ids: siteIds }),
  });
