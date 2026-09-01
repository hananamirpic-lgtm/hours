/**
 * Thin fetch wrapper.
 *
 * It carries the bearer access token and pulls the machine `code` out of the error envelope, so a
 * caller can translate a failure rather than display a raw status. The envelope is still nested under
 * `detail` — the backend's API-conventions task lifts it to the top level later, and this reads both
 * shapes so it will not need touching when that happens.
 *
 * The token is read through a getter rather than stored here, so the auth layer owns the single copy
 * and this module never has a stale one after a refresh.
 */

import type { ApiErrorBody } from './types';

const API_BASE = '/api';

let accessTokenGetter: () => string | null = () => null;

/** The auth layer registers how to read the current access token. Called once at start-up. */
export const setAccessTokenGetter = (getter: () => string | null): void => {
  accessTokenGetter = getter;
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    /** The machine code from the error envelope, or null when the body carried none. */
    readonly code: string | null,
    message: string,
    /** The envelope's `params`, so a caller can name what collided (e.g. a duplicate passport). */
    readonly params: Record<string, string> = {},
    /** The envelope's `actions`, e.g. `["archive"]` on a client that cannot be deleted. */
    readonly actions: string[] = [],
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export interface RequestOptions extends RequestInit {
  /** Status codes whose body is a meaningful answer rather than a failure. */
  acceptStatuses?: number[];
  /** Send without the Authorization header. Login and refresh do not have a token yet. */
  anonymous?: boolean;
}

const extractError = (
  body: unknown,
): { code: string | null; params: Record<string, string>; actions: string[] } => {
  const envelope = (body as ApiErrorBody | null)?.detail?.error ?? (body as ApiErrorBody | null)?.error;
  return {
    code: envelope?.code ?? null,
    params: envelope?.params ?? {},
    actions: envelope?.actions ?? [],
  };
};

/**
 * Fetch a binary response (a QR image or PDF) as a Blob, carrying the bearer token the same way
 * `apiFetch` does. Separate because `apiFetch` assumes a JSON body and an error envelope; a file
 * download has neither. A non-2xx response still carries the JSON error envelope, so it is read and
 * thrown as an `ApiError` exactly as the JSON path does.
 */
export async function apiFetchBlob(path: string, options: RequestOptions = {}): Promise<Blob> {
  const { acceptStatuses = [], anonymous = false, headers, ...init } = options;

  const token = anonymous ? null : accessTokenGetter();
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(headers ?? {}),
    },
  });

  if (!response.ok && !acceptStatuses.includes(response.status)) {
    const body: unknown = await response.json().catch(() => null);
    const { code, params, actions } = extractError(body);
    throw new ApiError(
      response.status,
      code,
      `Request to ${path} failed with ${response.status}`,
      params,
      actions,
    );
  }

  return response.blob();
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { acceptStatuses = [], anonymous = false, headers, ...init } = options;

  const token = anonymous ? null : accessTokenGetter();
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(headers ?? {}),
    },
  });

  if (response.status === 204) {
    return undefined as T;
  }

  const body: unknown = await response.json().catch(() => null);

  if (!response.ok && !acceptStatuses.includes(response.status)) {
    const { code, params, actions } = extractError(body);
    throw new ApiError(
      response.status,
      code,
      `Request to ${path} failed with ${response.status}`,
      params,
      actions,
    );
  }

  return body as T;
}
