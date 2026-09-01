/**
 * User management endpoints (Requirement 1, 2.1, 2.3, 20.8).
 *
 * A *user* is a login, distinct from an *employee*. These endpoints are administrator-only on the
 * server; the console hides the screen from other roles as a courtesy, not as the control. No response
 * carries a credential — a password is write-only, and the TOTP secret lives only on the 2FA enrolment
 * flow under `/auth`. Deactivation ends the target's sessions immediately (Requirement 20.8); a site
 * assignment changes a manager's visible scope on their next request (Requirement 2.3).
 */

import type {
  Paginated,
  UserCreate,
  UserListItem,
  UserResponse,
  UserRole,
  UserSitesResponse,
  UserUpdate,
} from './types';

import { apiFetch } from './client';

export const userKeys = {
  all: ['users'] as const,
  list: (params: UserListParams) => ['users', 'list', params] as const,
  detail: (id: string) => ['users', 'detail', id] as const,
};

export interface UserListParams {
  role?: UserRole | null;
  includeInactive: boolean;
  limit: number;
  offset: number;
}

export const listUsers = (params: UserListParams): Promise<Paginated<UserListItem>> => {
  const search = new URLSearchParams({
    include_inactive: String(params.includeInactive),
    limit: String(params.limit),
    offset: String(params.offset),
  });
  if (params.role) {
    search.set('role', params.role);
  }
  return apiFetch<Paginated<UserListItem>>(`/users?${search.toString()}`);
};

export const getUser = (id: string): Promise<UserResponse> => apiFetch<UserResponse>(`/users/${id}`);

export const createUser = (payload: UserCreate): Promise<UserResponse> =>
  apiFetch<UserResponse>('/users', { method: 'POST', body: JSON.stringify(payload) });

export const updateUser = (id: string, payload: UserUpdate): Promise<UserResponse> =>
  apiFetch<UserResponse>(`/users/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });

/** Deactivate a login; ends its sessions immediately (Requirement 20.8). */
export const deactivateUser = (id: string): Promise<UserResponse> =>
  apiFetch<UserResponse>(`/users/${id}/deactivate`, { method: 'POST' });

/** Restore a deactivated login; the user signs in fresh. */
export const reactivateUser = (id: string): Promise<UserResponse> =>
  apiFetch<UserResponse>(`/users/${id}/reactivate`, { method: 'POST' });

/** Replace a site manager's assigned sites (Requirement 2.3). */
export const setUserSites = (id: string, siteIds: string[]): Promise<UserSitesResponse> =>
  apiFetch<UserSitesResponse>(`/users/${id}/sites`, {
    method: 'PUT',
    body: JSON.stringify({ site_ids: siteIds }),
  });
