// The customer session as the portal sees it (R6.2, R8.2, R8.3, R8.11).
//
// A stored token alone does not say whether the customer owes a forced password change, so the session
// gate asks `GET /service/auth/me` once per token and decides from the answer. Screens that change the
// state (sign-in, change-password) store the new token or invalidate `portalKeys.me`.
import { useQuery } from '@tanstack/react-query';
import { portalFetch, type PortalRequestOptions } from '../api/portalFetch';
import { customerTokenStore, useCustomerToken, type TokenStore } from './customerToken';

/** All portal query keys start with this root, so one `removeQueries` drops every customer's data. */
export const portalKeys = {
  root: ['portal'] as const,
  me: ['portal', 'me'] as const,
};

/** `GET /service/auth/me` (backend `CustomerProfile`). */
export interface CustomerProfile {
  full_name: string;
  phone: string;
  city: string | null;
  apartment: string | null;
  must_change_password: boolean;
  /** The customer's settlement with its current names; null or absent when the city is only text (R30.6). */
  locality?: { id: string; name_he: string; name_en: string } | null;
}

export function fetchCustomerProfile(signal?: AbortSignal): Promise<CustomerProfile> {
  const options: PortalRequestOptions = signal ? { signal } : {};
  return portalFetch<CustomerProfile>('/auth/me', options);
}

/** What the session gate renders. */
export type SessionView = 'sign-in' | 'loading' | 'error' | 'change-password' | 'signed-in';

export interface SessionInput {
  hasToken: boolean;
  /** Status of the profile query. */
  profileStatus: 'pending' | 'error' | 'success';
  /** `must_change_password` of the profile, once known. */
  mustChangePassword: boolean | undefined;
}

/**
 * Pure decision of the session gate:
 *
 *   no token                                  sign-in           (R8.2: any screen but the accessibility page)
 *   token, profile not answered yet           loading
 *   token, profile failed (not a 401)         error             (a retry panel; the token is kept)
 *   token, profile says must change password  change-password   (replaces every portal path, R6.2)
 *   token, profile says it is clear           signed-in
 *
 * A 401 on the profile removes the token (see `handleUnauthorized`), which turns the first row on.
 */
export function sessionView(input: SessionInput): SessionView {
  if (!input.hasToken) return 'sign-in';
  if (input.profileStatus === 'pending') return 'loading';
  if (input.profileStatus === 'error') return 'error';
  return input.mustChangePassword === true ? 'change-password' : 'signed-in';
}

export interface PortalSession {
  view: SessionView;
  profile: CustomerProfile | undefined;
  /** Ask the profile again. Used by the error panel. */
  retry: () => void;
  isFetching: boolean;
}

export function usePortalSession(): PortalSession {
  const token = useCustomerToken();
  const query = useQuery({
    queryKey: portalKeys.me,
    queryFn: ({ signal }) => fetchCustomerProfile(signal),
    enabled: token !== null,
    staleTime: 30_000,
  });
  return {
    view: sessionView({
      hasToken: token !== null,
      profileStatus: query.status,
      mustChangePassword: query.data?.must_change_password,
    }),
    profile: query.data,
    retry: () => {
      void query.refetch();
    },
    isFetching: query.isFetching,
  };
}

/**
 * Signs out: tells the server to revoke the token (best effort) and removes it locally whatever the
 * answer is, so a failed call never leaves the customer signed in on this device.
 */
export async function signOutCustomer(tokens: TokenStore = customerTokenStore): Promise<void> {
  try {
    await portalFetch('/auth/sign-out', { method: 'POST', tokens });
  } catch {
    /* the local removal below is what the customer asked for */
  }
  tokens.clearToken();
}
