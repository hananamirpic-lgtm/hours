// Decides between the sign-in screen, the forced change-password screen and the signed-in routes
// (R6.2, R8.2, R8.3, R8.11). The decision itself is the pure `sessionView` in `session.ts`.
//
// The screens render IN PLACE: the URL is not rewritten, so a bookmarked `/service/tickets/12345` keeps
// its path while sign-in or change-password shows, and returns to that path afterwards (R6.2).
import { useEffect, type ReactNode } from 'react';
import { useAnnouncer } from './Announcer';
import { useSessionExpired } from './customerToken';
import { usePortalTranslation } from './portalI18n';
import { ChangePasswordScreen, SignInScreen } from './screens/placeholders';
import { usePortalSession, type SessionView } from './session';

export interface SessionViewContentProps {
  view: SessionView;
  onRetry: () => void;
  busy?: boolean;
  children: ReactNode;
}

/** The gate without the queries, so each view can be rendered for a given state. */
export function SessionViewContent({ view, onRetry, busy = false, children }: SessionViewContentProps) {
  const { t } = usePortalTranslation();
  switch (view) {
    case 'sign-in':
      return <SignInScreen />;
    case 'change-password':
      return <ChangePasswordScreen />;
    case 'signed-in':
      return <>{children}</>;
    case 'loading':
      return <p>{t('portal.loading')}</p>;
    case 'error':
      return (
        <section role="alert">
          <p>{t('portal.session.loadError')}</p>
          <button type="button" className="portal-button" onClick={onRetry} disabled={busy}>
            {t('portal.session.retry')}
          </button>
        </section>
      );
  }
}

export function PortalSessionGate({ children }: { children: ReactNode }) {
  const session = usePortalSession();
  const sessionExpired = useSessionExpired();
  const { alert } = useAnnouncer();
  const { i18n } = usePortalTranslation();

  // A 401 or the end of the 8 hours raised the notice: say it once, without moving focus (R23.14).
  // `i18n.t` reads the language at that moment, so a later language switch does not repeat it.
  useEffect(() => {
    if (sessionExpired) alert(i18n.t('a11y.live.sessionExpired'));
  }, [sessionExpired, alert, i18n]);

  return (
    <SessionViewContent view={session.view} onRetry={session.retry} busy={session.isFetching}>
      {children}
    </SessionViewContent>
  );
}
