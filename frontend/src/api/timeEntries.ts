/**
 * Time-entry endpoints — the manager hours view (Requirement 2.3, 2.4, 18.1, 22.3).
 *
 * `GET /api/time-entries` lists recorded shifts for the console hours view: a date range, an
 * employee, a site, a status, an anomaly flag and manual-only, every filter optional. The server
 * scopes the result to the caller's sites — a site manager sees only the entries at the sites they
 * run, an administrator or accounting sees every site — so nothing here narrows by site on the
 * client. The response is a stable-sorted page ordered by employee, work date and check-in time, so
 * the view lays out a chronological per-employee day across sites without re-sorting.
 *
 * Every value is locale-neutral: ISO timestamps and whole minutes, formatted by the screen. There is
 * no wage or billing field to redact — a time entry records when someone worked, not their pay.
 */

import type {
  Paginated,
  TimeEntryCreate,
  TimeEntryDelete,
  TimeEntryFlag,
  TimeEntryListItem,
  TimeEntryResponse,
  TimeEntryStatus,
  TimeEntryUpdate,
} from './types';

import { apiFetch } from './client';

export const timeEntryKeys = {
  all: ['time-entries'] as const,
  list: (params: TimeEntryListParams) => ['time-entries', 'list', params] as const,
};

/** The filters the hours view offers, all optional (Requirement 22.3). */
export interface TimeEntryListParams {
  date_from?: string | null;
  date_to?: string | null;
  employee_id?: string | null;
  site_id?: string | null;
  status?: TimeEntryStatus | null;
  /** Anomaly markers, matched as any-of. Sent as repeated `flag=` query parameters. */
  flags?: TimeEntryFlag[];
  manual_only?: boolean;
  limit: number;
  offset: number;
}

const buildQuery = (params: TimeEntryListParams): string => {
  const search = new URLSearchParams();
  search.set('limit', String(params.limit));
  search.set('offset', String(params.offset));
  if (params.date_from) {
    search.set('date_from', params.date_from);
  }
  if (params.date_to) {
    search.set('date_to', params.date_to);
  }
  if (params.employee_id) {
    search.set('employee_id', params.employee_id);
  }
  if (params.site_id) {
    search.set('site_id', params.site_id);
  }
  if (params.status) {
    search.set('status', params.status);
  }
  for (const flag of params.flags ?? []) {
    search.append('flag', flag);
  }
  if (params.manual_only) {
    search.set('manual_only', 'true');
  }
  return search.toString();
};

export const listTimeEntries = (params: TimeEntryListParams): Promise<Paginated<TimeEntryListItem>> =>
  apiFetch<Paginated<TimeEntryListItem>>(`/time-entries?${buildQuery(params)}`);

// --------------------------------------------------------------------------- writes (Requirement 12)
// Manual entry and correction. Open to administrators and site managers only; the employee role is
// forbidden by the server (Requirement 12.6), and a site manager is scoped to their sites
// (Requirement 2.3). Every operation carries a mandatory non-blank reason (Requirement 12.3), and the
// server applies the same overlap, plausibility and period-lock rules a scanned entry obeys
// (Requirement 12.5) — an overlap comes back a 409 that names the conflicting entry.

/** Create a completed shift by hand (Requirement 12.1). */
export const createTimeEntry = (payload: TimeEntryCreate): Promise<TimeEntryResponse> =>
  apiFetch<TimeEntryResponse>('/time-entries', {
    method: 'POST',
    body: JSON.stringify(payload),
  });

/** Correct the check-in or check-out time of an existing entry (Requirement 12.2). */
export const correctTimeEntry = (
  id: string,
  payload: TimeEntryUpdate,
): Promise<TimeEntryResponse> =>
  apiFetch<TimeEntryResponse>(`/time-entries/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });

/** Soft-delete an entry with a mandatory reason, retaining the row for audit (Requirement 12.7). */
export const deleteTimeEntry = (id: string, payload: TimeEntryDelete): Promise<TimeEntryResponse> =>
  apiFetch<TimeEntryResponse>(`/time-entries/${id}`, {
    method: 'DELETE',
    body: JSON.stringify(payload),
  });
