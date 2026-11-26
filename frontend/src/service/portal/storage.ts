// Browser storage access for the customer portal. Pure helpers, no React.
//
// `localStorage` can be missing (server rendering, tests), can throw on access (blocked cookies, a
// sandboxed frame) and can throw on write (private mode, full quota). Every caller therefore takes a
// `StorageLike` or null and wraps each call in try/catch (R8.5, R8.6).

/** The part of the Web Storage interface the portal uses. A test passes a plain object. */
export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** `window.localStorage`, or null when it does not exist or merely touching it throws. */
export function browserStorage(): StorageLike | null {
  try {
    if (typeof window === 'undefined') return null;
    return window.localStorage ?? null;
  } catch {
    return null;
  }
}
