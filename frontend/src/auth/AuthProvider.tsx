/**
 * Authentication state for the whole app.
 *
 * It answers three questions the router and shell keep asking: is someone signed in, who are they,
 * and how do we sign in or out. The current user comes from `GET /auth/me` through TanStack Query, so
 * a role change or a language change on the server is one invalidation away from being reflected.
 *
 * On start-up it tries the persisted refresh token once. If that trade succeeds the user lands back
 * where they were; if it fails they see the login screen, which is the correct outcome for an expired
 * or revoked session.
 */

import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import {
  authKeys,
  getCurrentUser,
  login as loginRequest,
  logout as logoutRequest,
  refresh,
  type LoginPayload,
} from '@/api/auth';
import { setAccessTokenGetter } from '@/api/client';
import type { CurrentUser } from '@/api/types';
import { changeLanguage, type Language } from '@/i18n';
import { clearTokens, getAccessToken, getRefreshToken, setTokens } from '@/auth/tokenStore';

// Register once, at module load, so every request made before the provider mounts still finds the
// token. The getter reads the single in-memory copy the token store owns.
setAccessTokenGetter(getAccessToken);

interface AuthContextValue {
  /** The signed-in user, or null while signed out. Undefined only during the very first resolution. */
  user: CurrentUser | null;
  isReady: boolean;
  signIn: (payload: LoginPayload) => Promise<void>;
  signOut: () => Promise<void>;
  /** Re-read the current user, e.g. after enrolling in 2FA or changing language on the server. */
  refetchUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [hasToken, setHasToken] = useState<boolean>(() => getAccessToken() !== null);
  const [bootstrapped, setBootstrapped] = useState<boolean>(() => getRefreshToken() === null);

  // A reload has an empty in-memory access token but may still hold a refresh token. Trade it once.
  useEffect(() => {
    if (bootstrapped || hasToken) {
      return;
    }
    const storedRefresh = getRefreshToken();
    if (storedRefresh === null) {
      setBootstrapped(true);
      return;
    }
    let cancelled = false;
    refresh(storedRefresh)
      .then((pair) => {
        if (cancelled) return;
        setTokens(pair.access_token, pair.refresh_token);
        setHasToken(true);
      })
      .catch(() => {
        clearTokens();
      })
      .finally(() => {
        if (!cancelled) setBootstrapped(true);
      });
    return () => {
      cancelled = true;
    };
  }, [bootstrapped, hasToken]);

  const userQuery = useQuery({
    queryKey: authKeys.me,
    queryFn: getCurrentUser,
    enabled: hasToken,
    retry: false,
    staleTime: 60_000,
  });

  // The signed-in user's stored language is the source of truth, so the interface follows the account
  // across devices rather than the browser's last guess (Requirement 21.2).
  useEffect(() => {
    const preferred = userQuery.data?.language;
    if (preferred) {
      void changeLanguage(preferred as Language);
    }
  }, [userQuery.data?.language]);

  const signIn = useCallback(
    async (payload: LoginPayload) => {
      const pair = await loginRequest(payload);
      setTokens(pair.access_token, pair.refresh_token);
      setHasToken(true);
      await queryClient.invalidateQueries({ queryKey: authKeys.me });
    },
    [queryClient],
  );

  const signOut = useCallback(async () => {
    try {
      await logoutRequest();
    } catch {
      // Even if the server call fails, the local session must end.
    }
    clearTokens();
    setHasToken(false);
    queryClient.removeQueries({ queryKey: authKeys.me });
  }, [queryClient]);

  const refetchUser = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: authKeys.me });
  }, [queryClient]);

  // The session is "ready" once the reload trade has finished and, if a token exists, the user query
  // has resolved one way or the other. A token that no longer resolves a user is a dead session.
  const isReady = bootstrapped && (!hasToken || userQuery.isFetched);
  const user = hasToken && userQuery.isSuccess ? userQuery.data : null;

  // A token that fails to resolve a user is expired or revoked: drop it so the login screen shows.
  useEffect(() => {
    if (hasToken && userQuery.isError) {
      clearTokens();
      setHasToken(false);
    }
  }, [hasToken, userQuery.isError]);

  const value = useMemo<AuthContextValue>(
    () => ({ user, isReady, signIn, signOut, refetchUser }),
    [user, isReady, signIn, signOut, refetchUser],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (context === null) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
