// The page frame of every portal screen that sits behind the flag gate: skip link, header with the
// language switch, `<main id="main">`, and the footer with the accessibility statement link and the
// accessibility contact (R8.4, R23.9, R23.10, R23.3; design "Page structure"). The company branding (logo
// and name, R28.3) is the first thing inside `<main>`, above the routed screen, so every screen shows it
// once, including sign-in, the forced change-password screen and the accessibility statement.
//
// Nothing here links to a Hours console screen (R8.10): the footer link is the portal's own
// `/service/accessibility` (router link, so the `/service` basename is added).
import type { MouseEvent } from 'react';
import { Link, Outlet } from 'react-router-dom';
import { useAccessibilityContact } from '../api/features';
import { LanguageSwitch } from './LanguageSwitch';
import { useCustomerToken } from './customerToken';
import { PortalBranding } from './PortalBranding';
import { usePortalTranslation } from './portalI18n';
import { signOutCustomer } from './session';

export const MAIN_ID = 'main';

/**
 * The contact wrapped in Unicode directional isolates (LRI ... PDI): a phone number or e-mail address
 * keeps its left-to-right digit order inside a right-to-left sentence, and the text stays one string.
 */
export function isolateLtr(text: string): string {
  return `\u2066${text}\u2069`;
}

function skipToMain(event: MouseEvent<HTMLAnchorElement>): void {
  // Focus the target directly instead of changing the URL hash, which the router would see as a navigation.
  event.preventDefault();
  document.getElementById(MAIN_ID)?.focus();
}

export function PortalShell() {
  const { t } = usePortalTranslation();
  const token = useCustomerToken();
  const contact = useAccessibilityContact();

  return (
    <div className="portal-app">
      <a className="portal-skip-link" href={`#${MAIN_ID}`} onClick={skipToMain}>
        {t('a11y.skipToContent')}
      </a>
      <header className="portal-header">
        <p className="portal-brand">{t('portal.appName')}</p>
        <div className="portal-header-actions">
          {token !== null && (
            <button
              type="button"
              className="portal-button portal-button--quiet"
              onClick={() => {
                void signOutCustomer();
              }}
            >
              {t('portal.account.signOut')}
            </button>
          )}
          <LanguageSwitch />
        </div>
      </header>
      <main id={MAIN_ID} tabIndex={-1} className="portal-main">
        <PortalBranding />
        <Outlet />
      </main>
      <footer className="portal-footer">
        <Link to="/accessibility">{t('portal.footer.accessibilityLink')}</Link>
        {contact !== null && <p>{t('portal.footer.accessibilityContact', { contact: isolateLtr(contact) })}</p>}
      </footer>
    </div>
  );
}
