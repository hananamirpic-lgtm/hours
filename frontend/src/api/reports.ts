/**
 * Report and dashboard endpoints (Requirement 18).
 *
 * These read the figures the payroll and billing engines already produced — nothing here recomputes
 * money. Every value is locale-neutral: money comes back as raw decimal strings and minutes as
 * integers, and the screen formats both.
 *
 * - `GET /api/reports/dashboard` returns the administrator home dashboard for the current month: the
 *   active-employee and active-site counts, the month's total hours, billing, cost and profit, and the
 *   attention counts (Requirement 18.5, 18.6). Finance data — administrators and accounting only.
 * - `GET /api/reports/profitability` returns total billing, cost and gross profit for the selected
 *   filters — month, employee, site, client and project (Requirement 18.4). Finance data.
 * - `GET /api/reports/missing-reports` returns the missing-report findings for a date range, scoped by
 *   role (Requirement 14.3). The dashboard attention section links here filtered to a kind.
 *
 * A refusal comes back as the machine error envelope the shared client lifts, so a caller renders it
 * through `apiError`.
 */

import type {
  DashboardReport,
  MissingReportsResult,
  ProfitabilityReport,
} from './types';

import { apiFetch } from './client';

export interface ProfitabilityParams {
  year: number;
  month: number;
  employeeId?: string | null;
  siteId?: string | null;
  clientId?: string | null;
  project?: string | null;
}

export interface MissingReportsParams {
  dateFrom: string;
  dateTo: string;
  employeeId?: string | null;
  siteId?: string | null;
}

export const reportKeys = {
  all: ['reports'] as const,
  dashboard: () => ['reports', 'dashboard'] as const,
  profitability: (params: ProfitabilityParams) => ['reports', 'profitability', params] as const,
  missingReports: (params: MissingReportsParams) => ['reports', 'missing-reports', params] as const,
};

/** The administrator home dashboard for the current month (Requirement 18.5, 18.6). */
export const readDashboard = (): Promise<DashboardReport> =>
  apiFetch<DashboardReport>('/reports/dashboard');

/** Total billing, cost and gross profit for the selected filters (Requirement 18.4). */
export const readProfitability = (params: ProfitabilityParams): Promise<ProfitabilityReport> => {
  const search = new URLSearchParams({
    year: String(params.year),
    month: String(params.month),
  });
  if (params.employeeId) {
    search.set('employee_id', params.employeeId);
  }
  if (params.siteId) {
    search.set('site_id', params.siteId);
  }
  if (params.clientId) {
    search.set('client_id', params.clientId);
  }
  if (params.project) {
    search.set('project', params.project);
  }
  return apiFetch<ProfitabilityReport>(`/reports/profitability?${search.toString()}`);
};

/** The missing-report findings for a date range, scoped by role (Requirement 14.3). */
export const readMissingReports = (params: MissingReportsParams): Promise<MissingReportsResult> => {
  const search = new URLSearchParams({
    date_from: params.dateFrom,
    date_to: params.dateTo,
  });
  if (params.employeeId) {
    search.set('employee_id', params.employeeId);
  }
  if (params.siteId) {
    search.set('site_id', params.siteId);
  }
  return apiFetch<MissingReportsResult>(`/reports/missing-reports?${search.toString()}`);
};
