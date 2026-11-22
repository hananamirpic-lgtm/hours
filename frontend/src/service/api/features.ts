/**
 * Feature discovery for the service-ticket system (Requirements 20.5, 21.3, 21.5, 21.6).
 *
 * `GET /api/features` is the one place the front ends learn whether the Feature_Flag is on. It needs
 * no token, so this client deliberately does NOT go through `apiFetch`: that wrapper reads the staff
 * access token, and the customer portal is not signed in as staff. It uses a plain `fetch` with a
 * 10 second AbortController timeout against the same base path `apiFetch` uses.
 *
 * Two layers, so every branch is testable without a network:
 *  - `fetchFeatures()` performs the request and never throws. It returns a `FeaturesFetchResult`.
 *  - `interpretFeatures(result)` is pure. It maps that result to a `FeatureFlagState`.
 *
 * Interpretation table (design.md, Feature discovery, "Client rules"):
 *
 *   result                                             state
 *   -------------------------------------------------  ---------
 *   200, body parses, `service_tickets === true`       'on'
 *   200, body parses, anything else (false, "true", 1,
 *        missing key, null, array, ...)                'off'
 *   404 (an older API without the route)               'off'
 *   200, body is not valid JSON                        'unknown'
 *   5xx                                                'unknown'
 *   401, 403, any other 4xx, 3xx, 2xx other than 200   'unknown'
 *   network error                                      'unknown'
 *   timeout                                            'unknown'
 *
 * 'unknown' means "could not find out". It must never be shown as the not-available page, which
 * would state something untrue. 401 and 403 get no special meaning: the endpoint needs no
 * authentication, so such a status comes from a proxy or firewall in front of the API and says
 * nothing about the flag. Treating it as 'off' would be a false statement, treating it as 'on'
 * would be unsafe, so it is 'unknown' and the user may retry.
 */

import { useQuery } from '@tanstack/react-query';

/** The state of the flag as the front end sees it, once discovery has finished. */
export type FeatureFlagState = 'on' | 'off' | 'unknown';

/** What a gate sees: the flag state, or 'loading' while the first answer is outstanding. */
export type FlagGateState = FeatureFlagState | 'loading';

/** The response body after an attempt to parse it as JSON. */
export type FeaturesBody = { ok: true; value: unknown } | { ok: false };

/** Everything `fetchFeatures` can report. It never throws. */
export type FeaturesFetchResult =
  | { kind: 'response'; status: number; body: FeaturesBody }
  | { kind: 'network-error' }
  | { kind: 'timeout' };

/** Same base path convention as `src/api/client.ts` (`VITE_API_BASE_URL`, default `/api`). */
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '/api').replace(/\/$/, '');

export const FEATURES_PATH = '/features';
export const FEATURES_TIMEOUT_MS = 10_000;
export const FEATURES_STALE_TIME_MS = 5 * 60 * 1000;

/** The one query key. The flag cannot change without a restart, so every screen shares one entry. */
export const featuresKeys = {
  all: ['features'] as const,
};

/** Ask the API whether the service-ticket system is on. Never rejects. */
export async function fetchFeatures(timeoutMs: number = FEATURES_TIMEOUT_MS): Promise<FeaturesFetchResult> {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    const response = await fetch(`${API_BASE}${FEATURES_PATH}`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      // No cookies and no Authorization header: the endpoint is unauthenticated.
      credentials: 'omit',
      cache: 'no-store',
      signal: controller.signal,
    });

    // Only a 200 carries a body that matters. Reading it stays inside the timeout.
    let body: FeaturesBody = { ok: false };
    if (response.status === 200) {
      try {
        body = { ok: true, value: (await response.json()) as unknown };
      } catch {
        if (timedOut) {
          return { kind: 'timeout' };
        }
        body = { ok: false };
      }
    }
    return { kind: 'response', status: response.status, body };
  } catch {
    return timedOut ? { kind: 'timeout' } : { kind: 'network-error' };
  } finally {
    clearTimeout(timer);
  }
}

/** Pure mapping from what happened to what the gates should do. See the table at the top. */
export function interpretFeatures(result: FeaturesFetchResult): FeatureFlagState {
  if (result.kind !== 'response') {
    return 'unknown';
  }
  if (result.status === 404) {
    return 'off';
  }
  if (result.status !== 200 || !result.body.ok) {
    return 'unknown';
  }
  const value = result.body.value;
  const flag =
    typeof value === 'object' && value !== null ? (value as { service_tickets?: unknown }).service_tickets : undefined;
  return flag === true ? 'on' : 'off';
}

/**
 * Thrown by the query function when the answer is 'unknown', so that TanStack Query holds an error
 * rather than caching "unknown" as data for five minutes. A later mount or a manual retry then asks
 * again.
 */
export class FeaturesUnknownError extends Error {
  constructor() {
    super('The feature-discovery endpoint did not give a usable answer');
    this.name = 'FeaturesUnknownError';
  }
}

async function loadFeatureFlag(): Promise<'on' | 'off'> {
  const state = interpretFeatures(await fetchFeatures());
  if (state === 'unknown') {
    throw new FeaturesUnknownError();
  }
  return state;
}

/** Pure mapping from a query's status and data to the state a gate renders. */
export function toFlagGateState(status: 'pending' | 'error' | 'success', data: 'on' | 'off' | undefined): FlagGateState {
  if (status === 'success' && data !== undefined) {
    return data;
  }
  return status === 'error' ? 'unknown' : 'loading';
}

export interface UseFeaturesResult {
  state: FlagGateState;
  /** Ask again. Used by the retry panel. */
  refetch: () => void;
  /** True while a request is in flight, so the retry button can show it is working. */
  isFetching: boolean;
}

/** The flag state for the current screen. One shared query, key `['features']`. */
export function useFeatures(): UseFeaturesResult {
  const query = useQuery({
    queryKey: featuresKeys.all,
    queryFn: loadFeatureFlag,
    staleTime: FEATURES_STALE_TIME_MS,
    refetchOnWindowFocus: false,
    // The retry panel offers a manual retry, so no automatic retries here.
    retry: false,
  });

  return {
    state: toFlagGateState(query.status, query.data),
    refetch: () => {
      void query.refetch();
    },
    isFetching: query.isFetching,
  };
}
