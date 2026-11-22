/**
 * The customer portal's HTTP client (R8.11, R8.12, R22.8; design "Frontend handling").
 *
 * It is separate from the staff `apiFetch` on purpose. `apiFetch` reads the staff access token from a
 * single global getter; this client reads only the customer token from `customerTokenStore`. Neither
 * module imports the other, so a customer token is never sent by the staff client and a staff token is
 * never sent from the portal. It also does not import the staff auth layer.
 *
 * Paths are relative to the Ticket_API prefix: `portalFetch('/tickets')` calls `{base}/service/tickets`.
 *
 * Every failure is thrown as a `PortalApiError { status, code, fields, retryAfter }`:
 *  - an HTTP error whose body is `{"detail": {"error": {"code", "fields"?}}}` carries that code and fields;
 *  - an HTTP error with any other body (HTML from a proxy, empty, an array, a FastAPI validation list)
 *    carries the code `generic`;
 *  - no response at all is status 0 with the code `network`, a timeout is status 0 with the code `timeout`.
 *    Neither is an API code, so `errorTextKey` maps both to `errors.generic` (R8.12), and the form screen
 *    can still tell a timeout apart for its "check the ticket list" message (R9.7).
 *
 * 401 handling is the pure `handleUnauthorized` below; this module applies its decision to the token store.
 */
import { customerTokenStore, type TokenStore } from '../portal/customerToken';

/** Same base path convention as `src/api/client.ts` and `features.ts` (`VITE_API_BASE_URL`, default `/api`). */
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '/api').replace(/\/$/, '');

/** The Ticket_API prefix under the API base. */
export const SERVICE_API_PREFIX = '/service';
/** Reads give up after 10 seconds. */
export const PORTAL_READ_TIMEOUT_MS = 10_000;
/** The ticket submission (multipart, with a photo) gives up after 120 seconds (R9.7). */
export const PORTAL_SUBMIT_TIMEOUT_MS = 120_000;

/** The code of an HTTP error whose body carried no usable code. */
export const GENERIC_CODE = 'generic';
export const NETWORK_CODE = 'network';
export const TIMEOUT_CODE = 'timeout';
export const ABORTED_CODE = 'aborted';

export class PortalApiError extends Error {
  constructor(
    /** HTTP status, or 0 when there was no response. */
    readonly status: number,
    /** The API error code, `generic` (unreadable body), `network`, `timeout` or `aborted`. */
    readonly code: string,
    /** The `fields` map of the envelope (field name to code); empty when there is none. */
    readonly fields: Readonly<Record<string, string>> = {},
    /** The raw `Retry-After` header value, or null when absent. Pass it to `waitTimeMessage`. */
    readonly retryAfter: string | null = null,
  ) {
    super(`Portal API error ${status} ${code}`);
    this.name = 'PortalApiError';
  }
}

export function isPortalApiError(value: unknown): value is PortalApiError {
  return value instanceof PortalApiError;
}

/**
 * A 404 that is the framework's own "Not Found" rather than an answer of the Ticket_API: it has no code.
 * It means the route is not mounted, for example after a restart with the flag off, so the portal asks
 * `/api/features` again (design "Feature discovery", client rules).
 */
export function isFrameworkNotFound(error: unknown): boolean {
  return error instanceof PortalApiError && error.status === 404 && error.code === GENERIC_CODE;
}

// --- error body parsing (pure) ---------------------------------------------------------------------

export interface ParsedErrorBody {
  readonly code: string;
  readonly fields: Readonly<Record<string, string>>;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/**
 * The code and fields of a parsed JSON error body. The envelope is `detail.error`; a top-level `error`
 * is read too, so the client keeps working if the backend lifts the envelope (as the staff client does).
 * Anything else is `generic` with no fields.
 */
export function parseErrorBody(body: unknown): ParsedErrorBody {
  const root = asRecord(body);
  const error = asRecord(asRecord(root?.detail)?.error) ?? asRecord(root?.error);
  const code = error?.code;
  if (typeof code !== 'string' || code === '') {
    return { code: GENERIC_CODE, fields: {} };
  }
  const rawFields = asRecord(error?.fields);
  const fields: Record<string, string> = {};
  if (rawFields !== null) {
    for (const [name, fieldCode] of Object.entries(rawFields)) {
      if (typeof fieldCode === 'string' && fieldCode !== '') fields[name] = fieldCode;
    }
  }
  return { code, fields };
}

// --- 401 handling (pure) ---------------------------------------------------------------------------

/** Which endpoint a 401 came from. It decides what the 401 means. */
export type PortalEndpoint = 'sign-in' | 'change-password' | 'sign-out' | 'other';

export interface UnauthorizedContext {
  readonly endpoint: PortalEndpoint;
}

export interface UnauthorizedDecision {
  /** Discard the stored customer token. */
  readonly clearToken: boolean;
  /** Send the customer to sign-in with the session-expired message (R8.11). */
  readonly showSessionExpired: boolean;
}

const NO_ACTION: UnauthorizedDecision = { clearToken: false, showSessionExpired: false };
const EXPIRE: UnauthorizedDecision = { clearToken: true, showSessionExpired: true };

/**
 * What to do with a 401 (R8.11):
 *
 *   endpoint          code                  clear token   session-expired message
 *   ----------------  --------------------  ------------  -----------------------
 *   sign-in           any                   no            no   (the screen shows the credential error)
 *   change-password   invalid_credentials   no            no   (a wrong current password stays on the screen)
 *   change-password   any other code        yes           yes
 *   sign-out          any                   yes           no   (the customer asked to leave)
 *   any other         any                   yes           yes
 */
export function handleUnauthorized(code: string, context: UnauthorizedContext): UnauthorizedDecision {
  switch (context.endpoint) {
    case 'sign-in':
      return NO_ACTION;
    case 'change-password':
      return code === 'invalid_credentials' ? NO_ACTION : EXPIRE;
    case 'sign-out':
      return { clearToken: true, showSessionExpired: false };
    case 'other':
      return EXPIRE;
  }
}

/** The endpoint class of a path relative to the Ticket_API prefix (query string ignored). */
export function endpointOf(path: string): PortalEndpoint {
  const clean = (path.split('?')[0] ?? '').replace(/\/+$/, '');
  switch (clean) {
    case '/auth/sign-in':
      return 'sign-in';
    case '/auth/change-password':
      return 'change-password';
    case '/auth/sign-out':
      return 'sign-out';
    default:
      return 'other';
  }
}

// --- the client ------------------------------------------------------------------------------------

export interface PortalRequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  /** A JSON-serialisable body. Sets `Content-Type: application/json`. */
  json?: unknown;
  /** A body sent as is, normally `FormData` for the submission. The browser sets the multipart boundary. */
  body?: BodyInit;
  /** 10 s for `read` (default), 120 s for `submit`. */
  kind?: 'read' | 'submit';
  /** Overrides the timeout of `kind`. For tests. */
  timeoutMs?: number;
  /** Caller cancellation, for example the signal TanStack Query passes. */
  signal?: AbortSignal;
  /** Send without the Authorization header. Sign-in has no token yet. */
  anonymous?: boolean;
  /** The token store. Tests inject one; the portal uses `customerTokenStore`. */
  tokens?: Pick<TokenStore, 'getToken' | 'clearToken' | 'expireSession'>;
}

/** The URL of a Ticket_API path. */
export function portalUrl(path: string): string {
  return `${API_BASE}${SERVICE_API_PREFIX}${path.startsWith('/') ? path : `/${path}`}`;
}

async function readJsonSafely(response: Response): Promise<unknown> {
  try {
    return (await response.json()) as unknown;
  } catch {
    return undefined;
  }
}

/**
 * Runs the request and `read`s the success response inside the timeout. Throws `PortalApiError` for every
 * failure. On a 401 it applies `handleUnauthorized` to the token store before throwing.
 */
async function portalRequest<T>(
  path: string,
  options: PortalRequestOptions,
  read: (response: Response) => Promise<T>,
): Promise<T> {
  const tokens = options.tokens ?? customerTokenStore;
  const sentToken = options.anonymous === true ? null : tokens.getToken();
  const timeoutMs = options.timeoutMs ?? (options.kind === 'submit' ? PORTAL_SUBMIT_TIMEOUT_MS : PORTAL_READ_TIMEOUT_MS);

  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const onCallerAbort = (): void => controller.abort();
  if (options.signal) {
    if (options.signal.aborted) controller.abort();
    else options.signal.addEventListener('abort', onCallerAbort, { once: true });
  }

  const headers: Record<string, string> = { Accept: 'application/json' };
  if (sentToken !== null) headers.Authorization = `Bearer ${sentToken}`;
  let body: BodyInit | undefined = options.body;
  if (options.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(options.json);
  }

  try {
    const init: RequestInit = {
      method: options.method ?? 'GET',
      headers,
      credentials: 'omit',
      cache: 'no-store',
      signal: controller.signal,
    };
    if (body !== undefined) init.body = body;
    const response = await fetch(portalUrl(path), init);

    if (!response.ok) {
      const parsed = parseErrorBody(await readJsonSafely(response));
      const error = new PortalApiError(
        response.status,
        parsed.code,
        parsed.fields,
        response.headers.get('Retry-After'),
      );
      if (response.status === 401) applyUnauthorized(error, path, sentToken, tokens);
      throw error;
    }
    return await read(response);
  } catch (caught) {
    if (caught instanceof PortalApiError) throw caught;
    if (timedOut) throw new PortalApiError(0, TIMEOUT_CODE);
    if (options.signal?.aborted) throw new PortalApiError(0, ABORTED_CODE);
    throw new PortalApiError(0, NETWORK_CODE);
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener('abort', onCallerAbort);
  }
}

/** Applies the 401 decision. A stale 401 (the customer has signed in again meanwhile) changes nothing. */
function applyUnauthorized(
  error: PortalApiError,
  path: string,
  sentToken: string | null,
  tokens: Pick<TokenStore, 'getToken' | 'clearToken' | 'expireSession'>,
): void {
  const decision = handleUnauthorized(error.code, { endpoint: endpointOf(path) });
  if (!decision.clearToken) return;
  if (sentToken !== null && tokens.getToken() !== sentToken) return;
  if (decision.showSessionExpired) tokens.expireSession();
  else tokens.clearToken();
}

/** A JSON request. Resolves to the parsed body, or `undefined` for 204 and for an empty body. */
export function portalFetch<T = unknown>(path: string, options: PortalRequestOptions = {}): Promise<T> {
  return portalRequest<T>(path, options, async (response) => {
    if (response.status === 204) return undefined as T;
    const text = await response.text();
    if (text === '') return undefined as T;
    try {
      return JSON.parse(text) as T;
    } catch {
      // A success status with a body that is not JSON (for example a proxy page): not an answer we can use.
      throw new PortalApiError(response.status, GENERIC_CODE);
    }
  });
}

/** A request whose success body is binary, for the ticket photo (fetched with the bearer header). */
export function portalFetchBlob(path: string, options: PortalRequestOptions = {}): Promise<Blob> {
  return portalRequest<Blob>(path, options, (response) => response.blob());
}
