/**
 * Client endpoints (Requirement 5).
 *
 * A client carries no wage, payroll or billing figure, so nothing here is redacted. Deletion may be
 * refused with a 409 that offers archival as the recorded alternative (Requirement 5.4); the caller
 * reads that from the error envelope's `actions` and calls `archiveClient` instead.
 */

import type {
  ClientCreate,
  ClientListItem,
  ClientResponse,
  ClientSitesResponse,
  ClientUpdate,
  Paginated,
} from './types';

import { apiFetch } from './client';

export const clientKeys = {
  all: ['clients'] as const,
  list: (params: ClientListParams) => ['clients', 'list', params] as const,
  detail: (id: string) => ['clients', 'detail', id] as const,
  sites: (id: string) => ['clients', id, 'sites'] as const,
};

export interface ClientListParams {
  includeArchived: boolean;
  limit: number;
  offset: number;
}

export const listClients = (params: ClientListParams): Promise<Paginated<ClientListItem>> =>
  apiFetch<Paginated<ClientListItem>>(
    `/clients?include_archived=${params.includeArchived}&limit=${params.limit}&offset=${params.offset}`,
  );

export const getClient = (id: string): Promise<ClientResponse> =>
  apiFetch<ClientResponse>(`/clients/${id}`);

export const getClientSites = (id: string): Promise<ClientSitesResponse> =>
  apiFetch<ClientSitesResponse>(`/clients/${id}/sites`);

export const createClient = (payload: ClientCreate): Promise<ClientResponse> =>
  apiFetch<ClientResponse>('/clients', { method: 'POST', body: JSON.stringify(payload) });

export const updateClient = (id: string, payload: ClientUpdate): Promise<ClientResponse> =>
  apiFetch<ClientResponse>(`/clients/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });

export const deleteClient = (id: string): Promise<void> =>
  apiFetch<void>(`/clients/${id}`, { method: 'DELETE' });

export const archiveClient = (id: string): Promise<ClientResponse> =>
  apiFetch<ClientResponse>(`/clients/${id}/archive`, { method: 'POST' });
