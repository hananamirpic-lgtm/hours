import type { AppLanguage, CurrentUser, TokenResponse, TotpSetupResponse } from './types';

import { apiFetch } from './client';

export const authKeys = {
  me: ['auth', 'me'] as const,
};

export interface LoginPayload {
  username: string;
  password: string;
  totp_code?: string;
}

/** Sign in. Anonymous: there is no token to send yet. */
export const login = (payload: LoginPayload): Promise<TokenResponse> =>
  apiFetch<TokenResponse>('/auth/login', {
    method: 'POST',
    anonymous: true,
    body: JSON.stringify(payload),
  });

/** Exchange a refresh token for a new pair. Anonymous: the access token may already have expired. */
export const refresh = (refreshToken: string): Promise<TokenResponse> =>
  apiFetch<TokenResponse>('/auth/refresh', {
    method: 'POST',
    anonymous: true,
    body: JSON.stringify({ refresh_token: refreshToken }),
  });

export const logout = (): Promise<void> => apiFetch<void>('/auth/logout', { method: 'POST' });

export const getCurrentUser = (): Promise<CurrentUser> => apiFetch<CurrentUser>('/auth/me');

/** Persist the caller's interface language against their account (Requirement 21.2). */
export const setLanguagePreference = (language: AppLanguage): Promise<CurrentUser> =>
  apiFetch<CurrentUser>('/auth/me/language', {
    method: 'PATCH',
    body: JSON.stringify({ language }),
  });

/**
 * Begin two-factor enrolment for the caller (Requirement 1.6). Issues a fresh secret and its
 * `otpauth://` URI; this does not enable 2FA — {@link verifyTwoFactor} does, once a code proves the
 * secret reached an authenticator. The response carries a credential and must never be persisted.
 */
export const setupTwoFactor = (): Promise<TotpSetupResponse> =>
  apiFetch<TotpSetupResponse>('/auth/2fa/setup', { method: 'POST' });

/** Complete enrolment by proving the pending secret with a code (Requirement 1.6). */
export const verifyTwoFactor = (totpCode: string): Promise<CurrentUser> =>
  apiFetch<CurrentUser>('/auth/2fa/verify', {
    method: 'POST',
    body: JSON.stringify({ totp_code: totpCode }),
  });

/**
 * Change the caller's own password, proving the current one first. Clears the first-use obligation
 * that blocks a non-administrator from the rest of the API, and returns the caller's updated state —
 * which is what drops the gate. Reachable while the caller is otherwise blocked.
 */
export const changePassword = (
  currentPassword: string,
  newPassword: string,
): Promise<CurrentUser> =>
  apiFetch<CurrentUser>('/auth/change-password', {
    method: 'POST',
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
