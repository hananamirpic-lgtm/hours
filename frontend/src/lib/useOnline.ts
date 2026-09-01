/**
 * Whether the browser currently has a network connection, tracked from the `online` / `offline`
 * events (Requirement 23.6).
 *
 * The mobile scan flow reads this to refuse a scan the moment the device is offline and to say
 * plainly that nothing was recorded, rather than firing a request that will hang or fail after a
 * delay and risk looking like a success. `navigator.onLine` is a hint, not a guarantee — a connected
 * Wi-Fi with no route out still reads `true` — so it is the front line, not the whole defence: a scan
 * that gets past it and then fails on the network is still surfaced as a failure by the caller, never
 * as a success. That is the invariant this task must not break.
 */

import { useSyncExternalStore } from 'react';

const subscribe = (onChange: () => void): (() => void) => {
  window.addEventListener('online', onChange);
  window.addEventListener('offline', onChange);
  return () => {
    window.removeEventListener('online', onChange);
    window.removeEventListener('offline', onChange);
  };
};

const getSnapshot = (): boolean =>
  typeof navigator === 'undefined' || typeof navigator.onLine !== 'boolean' ? true : navigator.onLine;

/** `true` when the browser reports a connection, `false` when it reports none. */
export const useOnline = (): boolean => useSyncExternalStore(subscribe, getSnapshot, () => true);
