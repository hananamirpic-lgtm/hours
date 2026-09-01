/**
 * Billing endpoints (Requirement 17).
 *
 * The whole payload is billing data, visible only to administrators and accounting (Requirement 2.5,
 * 17.7); the server enforces that with a finance-role guard, so these calls carry no redaction of
 * their own — a caller who reaches them is already permitted to see the figures. Every value is
 * locale-neutral: money comes back as raw decimal strings and minutes as integers, and the screen
 * formats both.
 *
 * - `GET /api/billing` returns a period's stored billing, per site and aggregated per client
 *   (Requirement 17.3). Cost and profit are computed at calculation time, so a stored read reports the
 *   billed amounts with the cost columns empty.
 * - `POST /api/billing/calculate` recomputes a month's per-site billing and profit from its Approved
 *   or Locked entries (Requirement 17.6), replacing any existing drafts, and returns the same summary
 *   with the live cost and profit filled in.
 *
 * A refusal — for example a billable day with no site rate in force — comes back as the machine error
 * envelope the shared client lifts, so a caller renders it through `apiError`.
 */

import type { BillingCalculateRequest, BillingSummary } from './types';

import { apiFetch } from './client';

export interface BillingParams {
  year: number;
  month: number;
}

export const billingKeys = {
  all: ['billing'] as const,
  summary: (params: BillingParams) => ['billing', 'summary', params] as const,
};

/** A period's stored billing summary, per site and per client (Requirement 17.3). */
export const readBilling = (params: BillingParams): Promise<BillingSummary> => {
  const search = new URLSearchParams({
    year: String(params.year),
    month: String(params.month),
  });
  return apiFetch<BillingSummary>(`/billing?${search.toString()}`);
};

/**
 * Recalculate a month's billing and profit (Requirement 17.6). Replaces the existing drafts rather
 * than duplicating them, and returns the summary with the live per-site cost and profit.
 */
export const calculateBilling = (payload: BillingCalculateRequest): Promise<BillingSummary> =>
  apiFetch<BillingSummary>('/billing/calculate', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
