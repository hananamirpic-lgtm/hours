// The customer token store (R8.2, R8.6, R8.11; design "Tokens and credential separation").
//
// localStorage key `hours.service.customerToken` holds JSON `{"token": "...", "exp": <epoch seconds>}`.
// There is no refresh token: when the token ends (8 hours) or a 401 says so, the customer signs in again.
//
// Where `exp` comes from. The design fixes the stored shape `{token, exp}` but not the source of `exp`,
// so: the `exp` claim of the JWT payload when the token decodes (that is the instant the server stops
// accepting it, to the second), otherwise `now + expires_in` of the sign-in response. The payload is
// decoded only to read `exp`; the signature is never checked here (the server does that).
//
// Staff isolation: this store is separate from `src/auth/tokenStore.ts` (key `hours.refreshToken`) and
// from the staff access-token getter. Nothing in here imports either.
//
// The store is built by `createTokenStore` from an injected storage, so the whole behaviour is testable
// without a DOM. `customerTokenStore` is the one instance the portal uses.
import { useSyncExternalStore } from 'react';
import { browserStorage, type StorageLike } from './storage';

export const CUSTOMER_TOKEN_KEY = 'hours.service.customerToken';

export interface StoredToken {
  readonly token: string;
  /** Expiry in epoch seconds. The token is dead at and after this second. */
  readonly exp: number;
}

/** What `parseStoredToken` accepts: a JSON object with a non-empty string `token` and a finite positive `exp`. */
export function parseStoredToken(raw: string | null): StoredToken | null {
  if (raw === null) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== 'object' || value === null) return null;
    const { token, exp } = value as { token?: unknown; exp?: unknown };
    if (typeof token !== 'string' || token === '') return null;
    if (typeof exp !== 'number' || !Number.isFinite(exp) || exp <= 0) return null;
    return { token, exp };
  } catch {
    return null;
  }
}

/** True when `now` (epoch seconds) is at or after `exp`. The server uses leeway 0, so does this. */
export function isExpired(exp: number, nowSeconds: number): boolean {
  return nowSeconds >= exp;
}

/** The `exp` claim of a JWT, or null when the token does not decode to a payload with a numeric `exp`. */
export function jwtExpiry(token: string): number | null {
  const parts = token.split('.');
  if (parts.length !== 3 || parts[1] === undefined || parts[1] === '') return null;
  try {
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4);
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    const payload: unknown = JSON.parse(new TextDecoder().decode(bytes));
    const exp = typeof payload === 'object' && payload !== null ? (payload as { exp?: unknown }).exp : undefined;
    return typeof exp === 'number' && Number.isFinite(exp) && exp > 0 ? exp : null;
  } catch {
    return null;
  }
}

/** `exp` for a freshly issued token: the JWT claim, else `now + expires_in`, else null (unusable). */
export function tokenExpiry(token: string, expiresInSeconds: unknown, nowSeconds: number): number | null {
  const claim = jwtExpiry(token);
  if (claim !== null) return claim;
  if (typeof expiresInSeconds === 'number' && Number.isFinite(expiresInSeconds) && expiresInSeconds > 0) {
    return nowSeconds + Math.floor(expiresInSeconds);
  }
  return null;
}

export interface TokenStore {
  /** The current token, or null. Removes an expired or malformed entry as a side effect. */
  getToken(): string | null;
  /** Stores a token and its expiry. Falls back to memory when storage throws. Clears any session-expired notice. */
  setToken(token: string, exp: number): void;
  /** Stores a token from a sign-in or change-password response. False when no expiry could be derived. */
  setTokenFromResponse(token: string, expiresInSeconds: unknown): boolean;
  /** Removes the token everywhere. */
  clearToken(): void;
  /** Removes the token and raises the session-expired notice (R8.11). */
  expireSession(): void;
  /** True while the session-expired notice is pending (until the next `setToken`). */
  getSessionExpired(): boolean;
  /** Clears the notice without touching the token. */
  dismissSessionExpired(): void;
  /** Listener runs after every change of token or notice. Returns the unsubscribe function. */
  subscribe(listener: () => void): () => void;
}

/** Just enough of `window` for cross-tab changes. */
export interface StorageEventSource {
  addEventListener(type: 'storage', listener: () => void): void;
  removeEventListener(type: 'storage', listener: () => void): void;
}

export interface TokenStoreOptions {
  /** The storage, or a function returning it (resolved on every access, so a late-appearing storage is found). */
  storage: StorageLike | null | (() => StorageLike | null);
  /** Epoch seconds. Defaults to the wall clock. */
  now?: () => number;
  /** Where cross-tab `storage` events come from. Defaults to `window` when it exists. */
  events?: StorageEventSource | null;
}

const MAX_TIMER_MS = 2_147_483_647;

export function createTokenStore(options: TokenStoreOptions): TokenStore {
  const resolveStorage = (): StorageLike | null => {
    try {
      return typeof options.storage === 'function' ? options.storage() : options.storage;
    } catch {
      return null;
    }
  };
  const nowSeconds = options.now ?? ((): number => Math.floor(Date.now() / 1000));
  const events: StorageEventSource | null =
    options.events !== undefined ? options.events : typeof window === 'undefined' ? null : window;

  // The in-memory copy. It is the source of truth after a failed write (`memoryOnly`) and the fallback
  // when a read throws.
  let memory: StoredToken | null = null;
  let memoryOnly = false;
  let sessionExpired = false;
  const listeners = new Set<() => void>();
  let timer: ReturnType<typeof setTimeout> | undefined;

  const notify = (): void => {
    for (const listener of Array.from(listeners)) listener();
  };

  const removeFromStorage = (): void => {
    try {
      resolveStorage()?.removeItem(CUSTOMER_TOKEN_KEY);
    } catch {
      /* nothing more to do: the memory copy is cleared by the caller */
    }
  };

  /** The live entry, with expired and malformed ones removed. No notification: safe to call while rendering. */
  const load = (): StoredToken | null => {
    let entry: StoredToken | null;
    if (memoryOnly) {
      entry = memory;
    } else {
      try {
        const storage = resolveStorage();
        const raw = storage === null ? null : storage.getItem(CUSTOMER_TOKEN_KEY);
        entry = parseStoredToken(raw);
        if (raw !== null && entry === null) removeFromStorage();
      } catch {
        entry = memory;
      }
    }
    if (entry !== null && isExpired(entry.exp, nowSeconds())) {
      entry = null;
      memory = null;
      removeFromStorage();
    }
    return entry;
  };

  const schedule = (): void => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
    if (listeners.size === 0) return;
    const entry = load();
    if (entry === null) return;
    const delayMs = Math.min(Math.max((entry.exp - nowSeconds()) * 1000, 0), MAX_TIMER_MS);
    timer = setTimeout(() => {
      timer = undefined;
      // The token ran out while the page was open: the gate must leave the signed-in screens (R8.2).
      if (load() === null) {
        sessionExpired = true;
        notify();
      } else {
        schedule();
      }
    }, delayMs + 50);
  };

  const onStorageEvent = (): void => {
    schedule();
    notify();
  };

  const store: TokenStore = {
    getToken() {
      return load()?.token ?? null;
    },
    setToken(token, exp) {
      const entry: StoredToken = { token, exp };
      memory = entry;
      sessionExpired = false;
      try {
        const storage = resolveStorage();
        if (storage === null) {
          memoryOnly = true;
        } else {
          storage.setItem(CUSTOMER_TOKEN_KEY, JSON.stringify(entry));
          memoryOnly = false;
        }
      } catch {
        memoryOnly = true;
      }
      schedule();
      notify();
    },
    setTokenFromResponse(token, expiresInSeconds) {
      const exp = tokenExpiry(token, expiresInSeconds, nowSeconds());
      if (exp === null) return false;
      store.setToken(token, exp);
      return true;
    },
    clearToken() {
      memory = null;
      removeFromStorage();
      schedule();
      notify();
    },
    expireSession() {
      memory = null;
      removeFromStorage();
      sessionExpired = true;
      schedule();
      notify();
    },
    getSessionExpired() {
      return sessionExpired;
    },
    dismissSessionExpired() {
      if (!sessionExpired) return;
      sessionExpired = false;
      notify();
    },
    subscribe(listener) {
      listeners.add(listener);
      if (listeners.size === 1) {
        events?.addEventListener('storage', onStorageEvent);
        schedule();
      }
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) {
          events?.removeEventListener('storage', onStorageEvent);
          if (timer !== undefined) clearTimeout(timer);
          timer = undefined;
        }
      };
    },
  };
  return store;
}

/** The one store the mounted portal uses. */
export const customerTokenStore: TokenStore = createTokenStore({ storage: browserStorage });

/** The current customer token, re-rendering when it changes (sign-in, sign-out, 401, expiry, other tab). */
export function useCustomerToken(store: TokenStore = customerTokenStore): string | null {
  return useSyncExternalStore(store.subscribe, store.getToken, store.getToken);
}

/** True while a session-expired notice is waiting to be shown on the sign-in screen. */
export function useSessionExpired(store: TokenStore = customerTokenStore): boolean {
  return useSyncExternalStore(store.subscribe, store.getSessionExpired, store.getSessionExpired);
}
