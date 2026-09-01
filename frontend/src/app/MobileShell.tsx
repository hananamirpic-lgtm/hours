/**
 * The employee mobile shell: minimal chrome, a title bar with the language toggle and sign-out, and
 * an outlet. The heavy screens — home with the one large action, the scanner — arrive with the
 * employee-mobile task; this frames them and proves the route tree and RTL behaviour.
 */

import { NavLink, Outlet } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { useAuth } from '@/auth/AuthProvider';
import { LanguageToggle } from '@/components/LanguageToggle';

export function MobileShell() {
  const { t } = useTranslation();
  const { signOut } = useAuth();

  return (
    <div className="mobile">
      <header className="mobile__bar">
        <span className="mobile__brand">{t('app.title')}</span>
        <div className="shell__actions">
          <LanguageToggle />
          <button
            type="button"
            className="button"
            onClick={() => {
              void signOut();
            }}
          >
            {t('auth.signOut')}
          </button>
        </div>
      </header>
      <nav className="mobile__nav" aria-label={t('mobileNav.label')}>
        <NavLink
          to="/m"
          end
          className={({ isActive }) =>
            `mobile__nav-link${isActive ? ' mobile__nav-link--active' : ''}`
          }
        >
          {t('mobileNav.home')}
        </NavLink>
        <NavLink
          to="/m/history"
          className={({ isActive }) =>
            `mobile__nav-link${isActive ? ' mobile__nav-link--active' : ''}`
          }
        >
          {t('mobileNav.myHours')}
        </NavLink>
      </nav>
      <main className="mobile__content">
        <Outlet />
      </main>
    </div>
  );
}
