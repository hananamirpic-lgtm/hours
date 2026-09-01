/**
 * The global-search endpoint (Requirement 22.1, 22.2, 22.4, 22.5).
 *
 * `GET /api/search?q=` searches employees, sites and clients for a term, matching a partial name in
 * either language, a site number, a client phone and a passport number — case- and accent-insensitive
 * — and returns the hits grouped by kind, each scoped to the caller and paged independently. A blank
 * term comes back as empty groups, so the caller can skip the request entirely for one.
 *
 * A refusal comes back as the machine error envelope the shared client lifts, so a caller renders it
 * through `apiError`.
 */

import type { SearchResults } from './types';

import { apiFetch } from './client';

export interface SearchParams {
  q: string;
  limit?: number;
  offset?: number;
}

export const searchKeys = {
  all: ['search'] as const,
  query: (params: SearchParams) => ['search', params] as const,
};

/** Search employees, sites and clients for a term, scoped to the caller (Requirement 22.1). */
export const search = (params: SearchParams): Promise<SearchResults> => {
  const query = new URLSearchParams({ q: params.q });
  if (params.limit != null) {
    query.set('limit', String(params.limit));
  }
  if (params.offset != null) {
    query.set('offset', String(params.offset));
  }
  return apiFetch<SearchResults>(`/search?${query.toString()}`);
};
