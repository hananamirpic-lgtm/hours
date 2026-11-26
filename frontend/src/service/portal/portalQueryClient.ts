// The portal's own QueryClient. It is not the staff `queryClient` (src/lib/queryClient.ts): the two apps
// share no cache, so a customer's tickets can never appear in a staff query and the reverse.
import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query';
import { featuresKeys } from '../api/features';
import { isFrameworkNotFound, isPortalApiError } from '../api/portalFetch';
import type { TokenStore } from './customerToken';
import { portalKeys } from './session';

/**
 * Retry rule for portal queries: a 4xx is an answer (retrying cannot change it), so it is not retried.
 * A network failure, a timeout or a 5xx is retried once.
 */
export function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  if (isPortalApiError(error) && error.status >= 400 && error.status < 500) return false;
  return failureCount < 1;
}

/**
 * Reactions to an error that any query or mutation raised:
 *  - the framework's own 404 (no code) means the route is gone, for example a restart with the flag
 *    off, so the flag is asked again (design "Feature discovery");
 *  - 403 `password_change_required` means the profile the gate holds is out of date, so it is asked again.
 */
export function reactToError(client: QueryClient, error: unknown): void {
  if (isFrameworkNotFound(error)) {
    void client.invalidateQueries({ queryKey: featuresKeys.all });
    return;
  }
  if (isPortalApiError(error) && error.status === 403 && error.code === 'password_change_required') {
    void client.invalidateQueries({ queryKey: portalKeys.me });
  }
}

export function createPortalQueryClient(): QueryClient {
  // The cache callbacks need the client, which does not exist until the caches do. They run only
  // after a request fails, by which time `client` is set.
  const holder: { client?: QueryClient } = {};
  const onError = (error: unknown): void => {
    if (holder.client) reactToError(holder.client, error);
  };
  holder.client = new QueryClient({
    queryCache: new QueryCache({ onError }),
    mutationCache: new MutationCache({ onError }),
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        retry: shouldRetryQuery,
        // Screens that poll set 'always' themselves (R12.5). Everything else stays quiet on focus.
        refetchOnWindowFocus: false,
      },
      mutations: { retry: false },
    },
  });
  return holder.client;
}

/**
 * Drops every portal query whenever the token is gone (sign-out, 401, expiry), so the next customer to
 * sign in on this device never sees the previous one's tickets. Returns the unsubscribe function.
 */
export function clearCacheWhenSignedOut(client: QueryClient, tokens: TokenStore): () => void {
  return tokens.subscribe(() => {
    if (tokens.getToken() === null) client.removeQueries({ queryKey: portalKeys.root });
  });
}
