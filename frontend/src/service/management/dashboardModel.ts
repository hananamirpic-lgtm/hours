/**
 * View model of the management dashboard (task 24.2; R10.9, R17.1, R17.4 to R17.7).
 *
 * Pure: no React, no DOM, no i18n instance. The page turns what is decided here into markup.
 */
import { ticketListQuery, type StaffDashboard, type TicketListQuery } from '../api/staff';

// --- polling (R17.5) -------------------------------------------------------------------------------

/** The counts reload this often while the dashboard is visible in the browser tab. */
export const DASHBOARD_POLL_MS = 30_000;

/**
 * Query options of the dashboard (design "Polling").
 *
 *  - `enabled` is the visibility of the tab. While it is false the query sends nothing: not on the timer,
 *    not on a reconnect, and not on mount in a tab that opened hidden. When it turns true the query is
 *    stale (`staleTime: 0`), so it loads at once (R17.5). `refetchOnWindowFocus: 'always'` loads at once
 *    as well, and the two requests are one.
 *  - `retry: false`: a failed load is retried at the next reload (the timer), not on its own (R17.6).
 *  - `gcTime: 0`: counts of an earlier visit are not shown again as if they were current.
 *
 * `refetchInterval` keeps running after a failure and `data` survives it, so the old counts stay (R17.6).
 */
export function dashboardQueryOptions(visible: boolean) {
  return {
    enabled: visible,
    refetchInterval: DASHBOARD_POLL_MS,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: 'always',
    refetchOnReconnect: true,
    refetchOnMount: 'always',
    staleTime: 0,
    gcTime: 0,
    retry: false,
  } as const;
}

/** True unless the document says it is hidden. `undefined` (no document, as in a test) counts as visible. */
export function isVisibleState(visibilityState: string | undefined): boolean {
  return visibilityState !== 'hidden';
}

// --- the response ----------------------------------------------------------------------------------

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0;
}

/**
 * True when a parsed body has the shape of `ServiceDashboardResponse`: three whole numbers from 0 upward and
 * a warning that is absent, null, or an object with a whole `remaining`. Anything else is a failed load,
 * so the page keeps its old counts instead of showing nonsense.
 */
export function isDashboardResponse(body: unknown): body is StaffDashboard {
  if (typeof body !== 'object' || body === null) return false;
  const record = body as Record<string, unknown>;
  if (!isCount(record.open) || !isCount(record.in_progress) || !isCount(record.urgent)) return false;
  const warning = record.ticket_number_warning;
  if (warning === undefined || warning === null) return true;
  return typeof warning === 'object' && isCount((warning as Record<string, unknown>).remaining);
}

// --- what the page shows (R17.1, R17.6, R17.7, R10.9) ----------------------------------------------

export interface DashboardCounts {
  readonly open: number;
  readonly inProgress: number;
  readonly urgent: number;
}

/**
 * `loading`  nothing loaded yet and no failure: the loading text.
 * `none`     nothing loaded and the load failed: the error message and no count values (R17.6).
 * `counts`   the three counts.
 */
export type DashboardContent = 'loading' | 'none' | 'counts';

export interface DashboardViewState {
  readonly content: DashboardContent;
  readonly counts: DashboardCounts | null;
  /** The error message. It shows from the failure until a later load succeeds (R17.6, R17.7). */
  readonly showError: boolean;
  /** The "not current" label: counts are on screen and the latest load failed (R17.6). */
  readonly stale: boolean;
  /** The remaining ticket numbers to warn about, for Config_Staff only; null for no warning (R10.9). */
  readonly numberWarning: number | null;
}

export interface DashboardViewInput {
  /** The last successful load, kept through later failures. */
  readonly data: StaffDashboard | undefined;
  /** The latest load failed. False again as soon as a load succeeds. */
  readonly isError: boolean;
  /** The signed-in role is Config_Staff. */
  readonly isConfigStaff: boolean;
}

export function dashboardViewState({ data, isError, isConfigStaff }: DashboardViewInput): DashboardViewState {
  if (data === undefined) {
    return {
      content: isError ? 'none' : 'loading',
      counts: null,
      showError: isError,
      stale: false,
      numberWarning: null,
    };
  }
  // The server sends the warning to Config_Staff only. The page checks the role too, so a technician never
  // sees it, whatever the answer holds.
  const warning = data.ticket_number_warning ?? null;
  return {
    content: 'counts',
    counts: { open: data.open, inProgress: data.in_progress, urgent: data.urgent },
    showError: isError,
    stale: isError,
    numberWarning: isConfigStaff && warning !== null ? warning.remaining : null,
  };
}

// --- the three counts and their list links (R17.4) -------------------------------------------------

export type DashboardCountKey = 'open' | 'inProgress' | 'urgent';

export interface DashboardCountEntry {
  readonly key: DashboardCountKey;
  readonly labelKey: string;
  /** A second line under the label, or null. */
  readonly hintKey: string | null;
}

/** The counts in display order. */
export const DASHBOARD_COUNTS: readonly DashboardCountEntry[] = [
  { key: 'open', labelKey: 'management.dashboard.open', hintKey: 'management.dashboard.openHint' },
  { key: 'inProgress', labelKey: 'management.dashboard.inProgress', hintKey: null },
  { key: 'urgent', labelKey: 'management.dashboard.urgent', hintKey: null },
];

/** The management ticket list, inside the app's router. */
export const TICKET_LIST_PATH = '/tickets/list';

/**
 * The list filters of each count (R17.4, Requirement 15 criterion 5): Open is Received and In Progress,
 * In Progress is In Progress, Urgent is urgent only together with Received and In Progress.
 */
export function dashboardListFilters(key: DashboardCountKey): TicketListQuery {
  switch (key) {
    case 'open':
      return { status: ['received', 'in_progress'] };
    case 'inProgress':
      return { status: ['in_progress'] };
    case 'urgent':
      return { status: ['received', 'in_progress'], urgentOnly: true };
  }
}

/**
 * The link of a count: the list path and the filters as the staff API names them
 * (`?status=received&status=in_progress&urgent_only=true`), built by the staff client's own query builder.
 */
export function dashboardListTarget(key: DashboardCountKey): { pathname: string; search: string } {
  return { pathname: TICKET_LIST_PATH, search: ticketListQuery(dashboardListFilters(key)) };
}

/** The number a count shows. */
export function countValue(counts: DashboardCounts, key: DashboardCountKey): number {
  return counts[key];
}
