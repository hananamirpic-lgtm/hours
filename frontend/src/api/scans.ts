/**
 * Attendance scan endpoints (Requirement 9, 10, 11, 23).
 *
 * The whole employee flow runs through a handful of endpoints. `POST /api/scans` resolves a presented
 * QR token to a check-in, a check-out or a conflict; the server decides from the employee's state and
 * records a server-side time. The request carries the token and, optionally, a client nonce the
 * duplicate-window check reads so a double-tap or a retry resolves to the same entry rather than a
 * second one (Requirement 9.3). There is no timestamp and no location field — none exists, and the
 * backend rejects an unknown field such as `latitude` (Requirement 9.6, 20.10).
 *
 * A plain check-in that collides with an open shift at another site comes back as a 409
 * `open_shift_elsewhere`, which `apiFetch` throws as an `ApiError` carrying the other site's name and
 * the actions `["transition", "cancel"]` (Requirement 11.4). The confirmed move is `transition`
 * (close the old, open the new atomically) or `end-and-move` (close the old with no departure QR).
 */

import type { ScanResult, ScanStatusResponse, WorkHistoryResponse } from './types';

import { apiFetch } from './client';

export type { WorkHistoryDay, WorkHistoryResponse } from './types';

export const scanKeys = {
  status: ['scans', 'status'] as const,
  history: ['scans', 'history'] as const,
  /**
   * A distinct key for the my-hours page's *ranged* history, keyed on the from/to bounds so it never
   * collides in the cache with the home screen's unfiltered `history` list, and so changing the range
   * fetches afresh rather than reading a stale window.
   */
  historyRange: (dateFrom?: string, dateTo?: string) =>
    ['scans', 'history', 'range', dateFrom ?? null, dateTo ?? null] as const,
};

/** The body of a scan and a transition: a token, and an optional retry nonce. No time, no location. */
export interface ScanBody {
  qr_token: string;
  client_nonce?: string;
}

/** POST /api/scans — the unified scan. Resolves to check-in, check-out, or a 409 conflict. */
export const recordScan = (body: ScanBody): Promise<ScanResult> =>
  apiFetch<ScanResult>('/scans', { method: 'POST', body: JSON.stringify(body) });

/** POST /api/scans/checkout — close the current open shift explicitly (the button path). */
export const checkOut = (): Promise<ScanResult> =>
  apiFetch<ScanResult>('/scans/checkout', { method: 'POST' });

/** POST /api/scans/transition — the confirmed move: close the current shift, open one at the target. */
export const transition = (body: ScanBody): Promise<ScanResult> =>
  apiFetch<ScanResult>('/scans/transition', { method: 'POST', body: JSON.stringify(body) });

/** POST /api/scans/end-and-move — end the current shift with no departure QR (Requirement 11.6). */
export const endAndMove = (): Promise<ScanResult> =>
  apiFetch<ScanResult>('/scans/end-and-move', { method: 'POST' });

/** GET /api/scans/status — the caller's current open shift, or `{ open_shift: null }`. */
export const getScanStatus = (): Promise<ScanStatusResponse> =>
  apiFetch<ScanStatusResponse>('/scans/status');

/** An optional inclusive date window for the work history, each bound a canonical `YYYY-MM-DD`. */
export interface WorkHistoryRange {
  dateFrom?: string;
  dateTo?: string;
}

/**
 * GET /api/scans/history — the caller's own completed work by day, newest first (Requirement 23.3).
 * Scoped server-side to the caller's linked employee; never another person's, and no employee id is
 * ever sent. An empty `days` (no completed work, a login not linked to an employee, or a range with
 * nothing in it) renders as "no recent work" / "no hours in this range".
 *
 * With no argument it returns the recent-work slice the home screen reads (server-capped, newest
 * first). Given a `dateFrom` and/or `dateTo` it appends them as `date_from`/`date_to` query params,
 * and the server returns every day in that inclusive window with no cap. Absent bounds are omitted
 * from the query string entirely, so the no-arg call is byte-for-byte the request it always was.
 */
export const getWorkHistory = (range: WorkHistoryRange = {}): Promise<WorkHistoryResponse> => {
  const params = new URLSearchParams();
  if (range.dateFrom) {
    params.set('date_from', range.dateFrom);
  }
  if (range.dateTo) {
    params.set('date_to', range.dateTo);
  }
  const query = params.toString();
  return apiFetch<WorkHistoryResponse>(query ? `/scans/history?${query}` : '/scans/history');
};
