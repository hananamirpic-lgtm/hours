/**
 * Approval workflow and period-locking endpoints (Requirement 15).
 *
 * With location verification out of scope, manager approval is the principal control over attendance
 * accuracy, so these are the endpoints the approval and period screens rest on:
 *
 * - `POST /api/time-entries/bulk-status` advances a set of entries along the ladder Draft → Review →
 *   Approved → Locked, scoped by the server to the caller's sites (Requirement 15.2, 15.3).
 * - `POST /api/periods/{year}/{month}/lock` freezes a month's Approved entries, warning first when
 *   unapproved entries remain (Requirement 15.4, 15.7).
 * - `POST /api/periods/{year}/{month}/unlock` reopens a locked month with a mandatory reason (15.6).
 * - `GET /api/periods` lists the months the workflow has touched and their state.
 *
 * Every value is locale-neutral: the screens format and translate. A refusal comes back as the
 * machine error envelope the shared client lifts, so a caller renders it through `apiError`.
 */

import type {
  BulkStatusRequest,
  BulkStatusResponse,
  PeriodListResponse,
  PeriodLockRequest,
  PeriodLockResult,
  PeriodState,
  PeriodUnlockRequest,
} from './types';

import { apiFetch } from './client';

export const periodKeys = {
  all: ['periods'] as const,
  list: () => ['periods', 'list'] as const,
};

/** Advance a set of entries to a target status (Requirement 15.2, 15.3). */
export const bulkChangeStatus = (payload: BulkStatusRequest): Promise<BulkStatusResponse> =>
  apiFetch<BulkStatusResponse>('/time-entries/bulk-status', {
    method: 'POST',
    body: JSON.stringify(payload),
  });

/**
 * Lock a calendar month (Requirement 15.4, 15.7). When the month holds unapproved entries and
 * `force` is not set, the result comes back `locked: false` with the warning list rather than an
 * error — the administrator's next move is to act on that list, so it is a normal 200.
 */
export const lockPeriod = (
  year: number,
  month: number,
  payload: PeriodLockRequest = {},
): Promise<PeriodLockResult> =>
  apiFetch<PeriodLockResult>(`/periods/${year}/${month}/lock`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });

/** Reopen a locked month with a mandatory reason (Requirement 15.6). */
export const unlockPeriod = (
  year: number,
  month: number,
  payload: PeriodUnlockRequest,
): Promise<PeriodState> =>
  apiFetch<PeriodState>(`/periods/${year}/${month}/unlock`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });

/** The months the workflow has touched, most recent first (Requirement 15.4). */
export const listPeriods = (): Promise<PeriodListResponse> =>
  apiFetch<PeriodListResponse>('/periods');
