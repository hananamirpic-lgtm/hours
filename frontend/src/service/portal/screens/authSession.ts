// What a successful sign-in or change-password does (R6.10, R8.3). Shared by both screens.
//
// The response of both endpoints is `{access_token, expires_in, must_change_password, ...}`. The token goes
// into the customer token store (which derives `exp` from the JWT claim or `now + expires_in`), the cached
// profile the session gate reads takes the new `must_change_password` at once, and the profile query is
// invalidated so the gate asks the server again and moves on: to the ticket list, or to the forced
// change-password screen. Nothing here logs or returns a password or a phone number.
import type { QueryClient } from '@tanstack/react-query';
import type { TokenStore } from '../customerToken';
import { portalKeys, type CustomerProfile } from '../session';

export interface AuthTokenResponse {
  readonly access_token: string;
  readonly expires_in: number;
  readonly must_change_password: boolean;
}

/** The response when it has a non-empty token and a boolean flag; null for anything else (a proxy page, `{}`). */
export function parseAuthTokenResponse(value: unknown): AuthTokenResponse | null {
  if (typeof value !== 'object' || value === null) return null;
  const { access_token, expires_in, must_change_password } = value as Record<string, unknown>;
  if (typeof access_token !== 'string' || access_token === '') return null;
  if (typeof must_change_password !== 'boolean') return null;
  return { access_token, expires_in: typeof expires_in === 'number' ? expires_in : Number.NaN, must_change_password };
}

/**
 * Stores the new token and tells the session gate. False, with nothing stored, when the response is not a
 * token response or no expiry can be derived from it; the caller then shows the generic error.
 */
export function completeAuthentication(
  value: unknown,
  tokens: Pick<TokenStore, 'setTokenFromResponse'>,
  client: QueryClient,
): boolean {
  const response = parseAuthTokenResponse(value);
  if (response === null) return false;
  if (!tokens.setTokenFromResponse(response.access_token, response.expires_in)) return false;
  // Only an existing profile is updated: the updater returning undefined leaves the cache untouched.
  client.setQueryData<CustomerProfile>(portalKeys.me, (profile) =>
    profile === undefined ? undefined : { ...profile, must_change_password: response.must_change_password },
  );
  void client.invalidateQueries({ queryKey: portalKeys.me });
  return true;
}
