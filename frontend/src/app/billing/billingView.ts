/**
 * Pure view helpers for the billing screen (Requirement 17.3, 17.4, 17.6).
 *
 * The API is locale-neutral: money arrives as raw decimal strings and minutes as integers. These
 * functions turn that payload into the small, testable shapes the screen renders — the billable
 * minutes a site row shows, and the set of sites that hang under a client in the per-client
 * drill-down — without touching React or i18n, so the arithmetic can be tested on its own and the
 * component stays a thin renderer.
 *
 * Money stays a string end to end and is parsed to a number only at the edge, where `formatCurrency`
 * needs one; the parse is centralised here so every screen reads a decimal string the same way. The
 * figures themselves are the backend's — computed with `Decimal` and `ROUND_HALF_UP` (Requirement
 * 17.1) — so this never re-derives an amount, it only presents what was calculated.
 */

import type { BillingSummary, ClientBilling, SiteBilling } from '@/api/types';

/**
 * Parse a locale-neutral decimal string (e.g. "645.00") to a number for formatting. An empty, missing
 * or unparseable value reads as zero, so a partial payload renders as ₪0.00 rather than NaN. A stored
 * read leaves a site's cost and profit `null` (they are computed at calculation time), so this must
 * accept `null` and read it as zero without complaint.
 */
export const parseAmount = (value: string | null | undefined): number => {
  if (value === null || value === undefined || value === '') {
    return 0;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
};

/** The billable minutes at one site: its regular and overtime minutes summed (Requirement 17.2). */
export const siteMinutes = (site: SiteBilling): number =>
  site.regular_minutes + site.overtime_minutes;

/**
 * Whether the summary carries live cost and profit. A stored read (`GET /api/billing`) leaves every
 * site's cost null and reports a zero total cost, so the cost and profit columns would all be empty
 * or misleading; the calculate response fills them in. The screen shows the profit columns only when
 * this is true, and otherwise invites a recalculation, so a null cost is never dressed up as ₪0.00
 * profit (Requirement 17.4).
 */
export const hasProfit = (summary: BillingSummary): boolean =>
  summary.sites.some((site) => site.cost !== null);

/** Whether any unapproved hours were left out of the summary — the excluded-hours notice (17.6). */
export const hasExcluded = (summary: BillingSummary): boolean =>
  summary.excluded_entry_count > 0 || summary.excluded_minutes > 0;

/**
 * The sites belonging to one client, in the summary's order (Requirement 17.3). The per-client
 * drill-down opens on a client row and lists exactly that client's sites, so their per-site billing,
 * cost and profit add up to the client total the row showed.
 */
export const sitesForClient = (summary: BillingSummary, clientId: string): SiteBilling[] =>
  summary.sites.filter((site) => site.client_id === clientId);

/** One client's aggregate row, looked up by id, or null when the summary carries no such client. */
export const clientById = (
  summary: BillingSummary,
  clientId: string,
): ClientBilling | null =>
  summary.clients.find((client) => client.client_id === clientId) ?? null;
