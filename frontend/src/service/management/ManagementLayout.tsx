/**
 * The frame of the Management_Interface: a sidebar of navigation, a top bar with the identity, the
 * language toggle and sign-out, and an outlet for the page. It reuses the console shell's classes, so
 * layout, the mobile drawer and right-to-left mirroring come from the same stylesheet.
 *
 * Configuration entries (categories, settlements, parts, labels, options, branding) are listed only for Config_Staff (R19.1); a
 * technician's navigation has the four ticket entries and nothing else. The entries come from
 * `managementNav`, which is tested.
 */
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { NavLink, Outlet } from 'react-router-dom';
import { useAuth } from '@/auth/AuthProvider';
import { LanguageToggle } from '@/components/LanguageToggle';
import { managementNav, type ManagementNavEntry } from './guards';
import { SERVICE_NAMESPACE } from './i18n';

function NavEntries({
  entries,
  onNavigate,
  label,
}: {
  entries: readonly ManagementNavEntry[];
  onNavigate: () => void;
  label?: string;
}) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  return (
    <ul className="nav-list" aria-label={label}>
      {entries.map((entry) => (
        <li key={entry.to}>
          <NavLink
            to={entry.to}
            end={entry.end}
            className={({ isActive }) => (isActive ? 'nav-link nav-link--active' : 'nav-link')}
            onClick={onNavigate}
          >
            {t(entry.labelKey)}
          </NavLink>
        </li>
      ))}
    </ul>
  );
}

export function ManagementLayout() {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  // Texts shared with the console (menu button, role names, sign-out) live in the `common` namespace.
  const { t: common } = useTranslation('common');
  const { user, signOut } = useAuth();
  const [navOpen, setNavOpen] = useState(false);

  // Escape closes the off-canvas menu on a narrow screen, like the console shell.
  useEffect(() => {
    if (!navOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setNavOpen(false);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [navOpen]);

  if (!user) return null;
  const nav = managementNav(user.role);
  const closeMenu = () => setNavOpen(false);

  return (
    <div className="shell">
      <button
        type="button"
        className="button shell__nav-toggle"
        aria-label={navOpen ? common('nav.closeMenu') : common('nav.openMenu')}
        aria-expanded={navOpen}
        onClick={() => setNavOpen((open) => !open)}
      >
        {common('nav.menu')}
      </button>
      {navOpen ? <div className="shell__nav-backdrop" role="presentation" onClick={closeMenu} /> : null}
      <aside className={navOpen ? 'shell__nav shell__nav--open' : 'shell__nav'}>
        <div className="shell__brand">{t('management.title')}</div>
        <nav aria-label={t('management.nav.label')}>
          <NavEntries entries={nav.main} onNavigate={closeMenu} />
          {nav.configuration.length > 0 ? (
            <>
              <p className="subtitle">{t('management.nav.configuration')}</p>
              <NavEntries
                entries={nav.configuration}
                onNavigate={closeMenu}
                label={t('management.nav.configuration')}
              />
            </>
          ) : null}
        </nav>
      </aside>
      <div className="shell__main">
        <header className="shell__topbar">
          <div className="shell__identity">
            <span className="shell__username">{user.username}</span>
            <span className="badge">{common(`role.${user.role}`)}</span>
          </div>
          <div className="shell__actions">
            <LanguageToggle />
            <button
              type="button"
              className="button"
              onClick={() => {
                void signOut();
              }}
            >
              {t('management.nav.signOut')}
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
