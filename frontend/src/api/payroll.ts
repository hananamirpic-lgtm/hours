/**
 * Payroll endpoints (Requirement 16).
 *
 * The whole payload is wage data, visible only to administrators and accounting (Requirement 2.6); the
 * server enforces that with a finance-role guard, so these calls carry no redaction of their own — a
 * caller who reaches them is already permitted to see the figures. Every value is locale-neutral: money
 * comes back as raw decimal strings and minutes as integers, and the screens format both.
 *
 * - `POST /api/payroll/calculate` computes an employee's month from its Approved or Locked entries,
 *   replacing any existing draft rather than duplicating it (Requirement 16.10). The recalculate
 *   action on the payroll screen calls this.
 * - `GET /api/payroll` lists the records for a period, filtered by year, month or employee
 *   (Requirement 16.6).
 * - `GET /api/payroll/{employee_id}/{year}/{month}` returns one record with its per-site allocation
 *   (Requirement 16.8).
 *
 * A refusal comes back as the machine error envelope the shared client lifts, so a caller renders it
 * through `apiError`.
 */

import type { PayrollCalculateRequest, PayrollListResponse, PayrollRecord } from './types';

import { apiFetch } from './client';

export const payrollKeys = {
  all: ['payroll'] as const,
  list: (params: PayrollListParams) => ['payroll', 'list', params] as const,
  detail: (employeeId: string, year: number, month: number) =>
    ['payroll', 'detail', employeeId, year, month] as const,
};

export interface PayrollListParams {
  year?: number | null;
  month?: number | null;
  employee_id?: string | null;
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

/** A page of payroll records for a period (Requirement 16.6). */
export const listPayroll = (params: PayrollListParams): Promise<PayrollListResponse> =>
  apiFetch<PayrollListResponse>(
    `/payroll${query({
      year: params.year,
      month: params.month,
      employee_id: params.employee_id,
      limit: params.limit,
      offset: params.offset,
    })}`,
  );

/** One employee's record for a month, with its per-site allocation (Requirement 16.8). */
export const getPayrollRecord = (
  employeeId: string,
  year: number,
  month: number,
): Promise<PayrollRecord> =>
  apiFetch<PayrollRecord>(`/payroll/${employeeId}/${year}/${month}`);

/**
 * Recalculate an employee's month (Requirement 16.10). Idempotent per employee and month: it replaces
 * an existing draft rather than adding a second record.
 */
export const calculatePayroll = (payload: PayrollCalculateRequest): Promise<PayrollRecord> =>
  apiFetch<PayrollRecord>('/payroll/calculate', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
