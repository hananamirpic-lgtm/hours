/**
 * The management console shell: a sidebar of role-driven navigation, a top bar with the language
 * toggle and sign-out, and an outlet for the active screen.
 *
 * Layout is expressed in logical properties (inline-start, block-end), so the sidebar sits on the
 * left in English and the right in Hebrew from the same stylesheet — no direction-specific rules and
 * nothing to mirror by hand.
 *
 * On a phone the fixed sidebar would eat the width, so under the mobile breakpoint it becomes an
 * off-canvas drawer that slides from the inline-start edge (left in English, right in Hebrew) and is
 * opened by the hamburger button in the top bar. The drawer is state-driven here — a class on the
 * nav and a backdrop — and it closes on a link tap, on the backdrop, and on Escape, so navigating
 * always dismisses it. Above the breakpoint the toggle is hidden and the sidebar sits beside the
 * content exactly as before (see `.shell__nav-toggle` in global.css).
 */

import { useEffect, useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { useAuth } from '@/auth/AuthProvider';
import { navFor } from '@/app/navigation';
import { GlobalSearch } from '@/app/search/GlobalSearch';
import { LanguageToggle } from '@/components/LanguageToggle';

export function ConsoleShell() {
  const { t } = useTranslation();
  const { user, signOut } = useAuth();
  const [navOpen, setNavOpen] = useState(false);

  // Escape closes the off-canvas menu, the ordinary dismissal for an overlay panel.
  useEffect(() => {
    if (!navOpen) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setNavOpen(false);
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [navOpen]);

  if (!user) {
    return null;
  }

  const items = navFor(user.role);

  return (
    <div className="shell">
      <button
        type="button"
        className="button shell__nav-toggle"
        aria-label={navOpen ? t('nav.closeMenu') : t('nav.openMenu')}
        aria-expanded={navOpen}
        onClick={() => setNavOpen((open) => !open)}
      >
        {t('nav.menu')}
      </button>

      {navOpen ? (
        <div
          className="shell__nav-backdrop"
          role="presentation"
          onClick={() => setNavOpen(false)}
        />
      ) : null}

      <aside
        className={navOpen ? 'shell__nav shell__nav--open' : 'shell__nav'}
        aria-label={t('nav.primary')}
      >
        <div className="shell__brand">{t('app.title')}</div>
        <nav>
          <ul className="nav-list">
            {items.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.to === '/'}
                  className={({ isActive }) => (isActive ? 'nav-link nav-link--active' : 'nav-link')}
                  onClick={() => setNavOpen(false)}
                >
                  {t(item.labelKey)}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
      </aside>

      <div className="shell__main">
        <header className="shell__topbar">
          <div className="shell__identity">
            <span className="shell__username">{user.username}</span>
            <span className="badge">{t(`role.${user.role}`)}</span>
          </div>
          <GlobalSearch />
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

        <main className="shell__content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
