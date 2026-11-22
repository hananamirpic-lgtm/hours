// The two live regions at the portal root and the hook screens use to write into them (R23.14, R23.15).
//
//   announce(text)  polite  `role="status"`  confirmation, status changes, language changed
//   alert(text)     assertive `role="alert"` session expired, rate limit, server error, numbers exhausted
//
// Both regions are in the DOM from the first render and stay there, empty until used, because a live
// region is only announced when its content changes after it exists. Writing the same text twice in a
// row would not change the content, so each write clears the region first and sets the text in the
// next tick. The text is cleared again after ten seconds so a stale message does not linger.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

export interface Announcer {
  /** Polite announcement (`role="status"`). */
  announce: (text: string) => void;
  /** Assertive announcement (`role="alert"`). */
  alert: (text: string) => void;
}

const NOOP_ANNOUNCER: Announcer = { announce: () => undefined, alert: () => undefined };

const AnnouncerContext = createContext<Announcer>(NOOP_ANNOUNCER);

/** How long a message stays in its region. */
export const ANNOUNCEMENT_LIFETIME_MS = 10_000;

/**
 * The announcer of the nearest `PortalAnnouncerProvider`. Without one it does nothing, so a screen or a
 * component test renders without the provider.
 */
export function useAnnouncer(): Announcer {
  return useContext(AnnouncerContext);
}

/** One region's text plus the writer for it. */
function useRegion(): [string, (text: string) => void] {
  const [text, setText] = useState('');
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);

  const write = useCallback((next: string) => {
    for (const timer of timers.current) clearTimeout(timer);
    timers.current = [];
    setText('');
    if (next === '') return;
    timers.current.push(setTimeout(() => setText(next), 50));
    timers.current.push(setTimeout(() => setText(''), ANNOUNCEMENT_LIFETIME_MS));
  }, []);

  useEffect(
    () => () => {
      for (const timer of timers.current) clearTimeout(timer);
    },
    [],
  );
  return [text, write];
}

export function PortalAnnouncerProvider({ children }: { children: ReactNode }) {
  const [statusText, writeStatus] = useRegion();
  const [alertText, writeAlert] = useRegion();
  const value = useMemo<Announcer>(() => ({ announce: writeStatus, alert: writeAlert }), [writeStatus, writeAlert]);
  return (
    <AnnouncerContext.Provider value={value}>
      {children}
      <div className="portal-live-regions">
        <div role="status" aria-live="polite" aria-atomic="true" className="portal-visually-hidden">
          {statusText}
        </div>
        <div role="alert" aria-live="assertive" aria-atomic="true" className="portal-visually-hidden">
          {alertText}
        </div>
      </div>
    </AnnouncerContext.Provider>
  );
}
