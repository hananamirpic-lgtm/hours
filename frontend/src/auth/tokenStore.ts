/**
 * Token storage.
 *
 * The access token lives in memory only: it is short-lived and never touches storage, so an XSS that
 * reads localStorage does not walk away with a working credential. The refresh token is persisted, so
 * a page reload can trade it for a fresh pair rather than forcing a new sign-in; that is the accepted
 * trade for not making the user log in on every refresh. The day this needs hardening, the refresh
 * token moves to an httpOnly cookie and this file is where it changes.
 */

const REFRESH_KEY = 'hours.refreshToken';

let accessToken: string | null = null;

export const getAccessToken = (): string | null => accessToken;

export const getRefreshToken = (): string | null => {
  try {
    return localStorage.getItem(REFRESH_KEY);
  } catch {
    return null;
  }
};

export const setTokens = (access: string, refresh: string): void => {
  accessToken = access;
  try {
    localStorage.setItem(REFRESH_KEY, refresh);
  } catch {
    // A browser with storage disabled keeps working for the current tab; only reload recovery is lost.
  }
};

export const clearTokens = (): void => {
  accessToken = null;
  try {
    localStorage.removeItem(REFRESH_KEY);
  } catch {
    // Nothing to recover from here.
  }
};
