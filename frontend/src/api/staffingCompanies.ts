/**
 * Staffing-company endpoints (Requirement 1, 3).
 *
 * A plain admin CRUD surface, mirroring `clients.ts`. Deletion may be refused with a 409 when active
 * employees are still linked; the caller reads the blocking employees from the error envelope's
 * `params.employees` and shows them so the admin knows who to deactivate first (Requirement 3.2, 3.3).
 */

import type {
  Paginated,
  StaffingCompanyCreate,
  StaffingCompanyListItem,
  StaffingCompanyResponse,
  StaffingCompanyUpdate,
} from './types';

import { apiFetch } from './client';

export const staffingCompanyKeys = {
  all: ['staffing-companies'] as const,
  list: (params: StaffingCompanyListParams) => ['staffing-companies', 'list', params] as const,
  detail: (id: string) => ['staffing-companies', 'detail', id] as const,
};

export interface StaffingCompanyListParams {
  limit: number;
  offset: number;
}

export const listStaffingCompanies = (
  params: StaffingCompanyListParams,
): Promise<Paginated<StaffingCompanyListItem>> =>
  apiFetch<Paginated<StaffingCompanyListItem>>(
    `/staffing-companies?limit=${params.limit}&offset=${params.offset}`,
  );

export const getStaffingCompany = (id: string): Promise<StaffingCompanyResponse> =>
  apiFetch<StaffingCompanyResponse>(`/staffing-companies/${id}`);

export const createStaffingCompany = (
  payload: StaffingCompanyCreate,
): Promise<StaffingCompanyResponse> =>
  apiFetch<StaffingCompanyResponse>('/staffing-companies', {
    method: 'POST',
    body: JSON.stringify(payload),
  });

export const updateStaffingCompany = (
  id: string,
  payload: StaffingCompanyUpdate,
): Promise<StaffingCompanyResponse> =>
  apiFetch<StaffingCompanyResponse>(`/staffing-companies/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });

export const deleteStaffingCompany = (id: string): Promise<void> =>
  apiFetch<void>(`/staffing-companies/${id}`, { method: 'DELETE' });