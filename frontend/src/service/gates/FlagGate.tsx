/**
 * The flag gate (Requirements 8.2, 20.5, 21.3, 21.5, 21.6).
 *
 * It wraps the customer portal tree and the management tree, outside every sign-in screen, so it
 * takes precedence over them: with the flag off or unknown the sign-in is never rendered.
 *
 *  - on       -> the children
 *  - off      -> the not-available page (Hebrew and English together)
 *  - unknown  -> a retry panel (the flag could not be read, which is not the same as "off")
 *  - loading  -> a minimal neutral status
 *
 * It uses no staff i18n instance and no staff AuthProvider, only a QueryClientProvider above it.
 */

import type { ReactNode } from 'react';

import { useFeatures, type FlagGateState } from '../api/features';
import { BILINGUAL_SEPARATOR, LOADING_TEXT, RETRY_BUTTON_TEXT, RETRY_TEXT, type BilingualText } from './flagGateStrings';
import { NotAvailable, pageStyle } from './NotAvailable';
import { flagGateView } from './flagGateView';

export function Bilingual({ text }: { text: BilingualText }) {
  return (
    <>
      <span lang="he" dir="rtl">
        {text.he}
      </span>{' '}
      <span aria-hidden="true">{BILINGUAL_SEPARATOR}</span>{' '}
      <span lang="en" dir="ltr">
        {text.en}
      </span>
    </>
  );
}

export function RetryPanel({ onRetry, busy }: { onRetry: () => void; busy: boolean }) {
  return (
    <main style={pageStyle}>
      <section role="alert">
        <p>
          <Bilingual text={RETRY_TEXT} />
        </p>
        <button type="button" onClick={onRetry} disabled={busy}>
          <Bilingual text={RETRY_BUTTON_TEXT} />
        </button>
      </section>
    </main>
  );
}

export function LoadingState() {
  return (
    <div role="status" style={pageStyle}>
      <Bilingual text={LOADING_TEXT} />
    </div>
  );
}

export interface FlagGateViewProps {
  state: FlagGateState;
  onRetry: () => void;
  busy?: boolean;
  children: ReactNode;
}

/** The gate without the query, so each view can be rendered for a given state. */
export function FlagGateContent({ state, onRetry, busy = false, children }: FlagGateViewProps) {
  switch (flagGateView(state)) {
    case 'content':
      return <>{children}</>;
    case 'not-available':
      return <NotAvailable />;
    case 'retry':
      return <RetryPanel onRetry={onRetry} busy={busy} />;
    case 'loading':
      return <LoadingState />;
  }
}

export interface FlagGateProps {
  children: ReactNode;
}

export function FlagGate({ children }: FlagGateProps) {
  const { state, refetch, isFetching } = useFeatures();
  return (
    <FlagGateContent state={state} onRetry={refetch} busy={isFetching}>
      {children}
    </FlagGateContent>
  );
}
