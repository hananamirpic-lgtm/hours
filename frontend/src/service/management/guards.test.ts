// Tests of the management guards (task 24.1): the pure decisions, the navigation per role, and the markup each
// guard branch renders. No DOM and no testing library: `renderToStaticMarkup`. The guard components that read
// the auth provider are not rendered here; `GuardViews` holds what they render and takes the decision as a prop.
import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { Navigate } from 'react-router-dom';
import { StaticRouter } from 'react-router-dom/server';
import { describe, expect, it } from 'vitest';
import type { UserRole } from '@/api/types';
import { createPortalI18n, type PortalLanguage } from '@/service/portal/portalI18n';
import {
  CONFIG_NAV,
  TICKET_NAV,
  configStaffDecision,
  isConfigStaffRole,
  isTicketStaffRole,
  managementNav,
  ticketStaffDecision,
  type TicketStaffDecision,
} from './guards';
import { ConfigStaffView, TicketStaffView } from './GuardViews';
import { NotAuthorizedPage } from './NotAuthorizedPage';
import en from '@/i18n/locales/en/service.json';
import he from '@/i18n/locales/he/service.json';

const ALL_ROLES: UserRole[] = ['admin', 'operations_admin', 'technician', 'accounting', 'site_manager', 'employee'];
const TICKET_STAFF: UserRole[] = ['admin', 'operations_admin', 'technician'];
const OTHER_ROLES = ALL_ROLES.filter((role) => !TICKET_STAFF.includes(role));

describe('ticketStaffDecision', () => {
  it('waits while the session is being resolved, whatever else is known', () => {
    for (const role of [undefined, ...ALL_ROLES]) {
      for (const hasCustomerToken of [false, true]) {
        expect(ticketStaffDecision({ isReady: false, role, hasCustomerToken })).toBe('wait');
      }
    }
  });

  it('sends a visitor with no staff session and no customer token to the sign-in (R1.13)', () => {
    expect(ticketStaffDecision({ isReady: true, role: undefined, hasCustomerToken: false })).toBe('sign-in');
  });

  it('shows the not-authorized page, not the sign-in, to a customer token with no staff session (R1.13)', () => {
    expect(ticketStaffDecision({ isReady: true, role: undefined, hasCustomerToken: true })).toBe('not-authorized');
  });

  it.each(TICKET_STAFF)('allows %s, with or without a customer token in the storage', (role) => {
    expect(ticketStaffDecision({ isReady: true, role, hasCustomerToken: false })).toBe('allow');
    expect(ticketStaffDecision({ isReady: true, role, hasCustomerToken: true })).toBe('allow');
  });

  it.each(OTHER_ROLES)('shows the not-authorized page to %s (R1.13)', (role) => {
    expect(ticketStaffDecision({ isReady: true, role, hasCustomerToken: false })).toBe('not-authorized');
  });
});

describe('role helpers', () => {
  it('ticket staff is exactly admin, operations_admin and technician', () => {
    expect(ALL_ROLES.filter(isTicketStaffRole)).toEqual(TICKET_STAFF);
    expect(isTicketStaffRole(undefined)).toBe(false);
  });

  it('config staff is admin and operations_admin, never the technician (R19.1)', () => {
    expect(ALL_ROLES.filter(isConfigStaffRole)).toEqual(['admin', 'operations_admin']);
    expect(isConfigStaffRole(undefined)).toBe(false);
  });
});

describe('configStaffDecision', () => {
  it.each([
    ['admin', 'allow'],
    ['operations_admin', 'allow'],
    ['technician', 'unavailable'],
  ] as const)('%s -> %s', (role, expected) => {
    expect(configStaffDecision(role)).toBe(expected);
  });

  it('gives no access to a missing role or a role outside ticket staff', () => {
    expect(configStaffDecision(undefined)).toBe('unavailable');
    for (const role of OTHER_ROLES) expect(configStaffDecision(role)).toBe('unavailable');
  });
});

describe('managementNav', () => {
  const paths = (entries: readonly { to: string }[]): string[] => entries.map((entry) => entry.to);

  it('lists the four ticket entries in order for every role', () => {
    for (const role of ALL_ROLES) {
      expect(paths(managementNav(role).main)).toEqual([
        '/tickets',
        '/tickets/list',
        '/tickets/customers',
        '/tickets/reports',
      ]);
    }
  });

  it.each(['admin', 'operations_admin'] as const)(
    '%s also gets the six configuration entries, settlements, parts and branding included (R19.1, R28.1, R29.6, R30.1)',
    (role) => {
      expect(paths(managementNav(role).configuration)).toEqual([
        '/tickets/settings/categories',
        '/tickets/settings/localities',
        '/tickets/settings/parts',
        '/tickets/settings/labels',
        '/tickets/settings/options',
        '/tickets/settings/branding',
      ]);
    },
  );

  it('labels the new entries with their own nav keys', () => {
    const labels = Object.fromEntries(CONFIG_NAV.map((entry) => [entry.to, entry.labelKey]));
    expect(labels['/tickets/settings/localities']).toBe('management.nav.localities');
    expect(labels['/tickets/settings/parts']).toBe('management.nav.parts');
    expect(labels['/tickets/settings/branding']).toBe('management.nav.branding');
  });

  it('gives the technician no configuration entry, and so no link to the new screens', () => {
    expect(managementNav('technician').configuration).toEqual([]);
    for (const role of ALL_ROLES.filter((candidate) => !isConfigStaffRole(candidate))) {
      expect(managementNav(role).configuration).toEqual([]);
    }
  });

  it('only the dashboard entry matches its own path exactly, so it is not active on every page', () => {
    expect(TICKET_NAV.filter((entry) => entry.end).map((entry) => entry.to)).toEqual(['/tickets']);
    expect(CONFIG_NAV.some((entry) => entry.end)).toBe(false);
  });
});

// --- markup ------------------------------------------------------------------------------------------------

const here = { pathname: '/tickets/list/12345', search: '', hash: '', state: null, key: 'k' };

function render(language: PortalLanguage, element: ReactElement, location = '/tickets'): string {
  return renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n: createPortalI18n(language) },
      createElement(StaticRouter, { location }, element),
    ),
  );
}

const text = { en, he };

describe('NotAuthorizedPage', () => {
  it.each(['en', 'he'] as const)('says it in %s and shows nothing else', (language) => {
    const html = render(language, createElement(NotAuthorizedPage, { variant: 'staff', homePath: '/' }));
    expect(html).toContain(text[language].management.notAuthorized.title);
    expect(html).toContain(text[language].management.notAuthorized.body);
    expect(html).toContain('<h1');
    expect(html.match(/<h1/g)).toHaveLength(1);
    expect(html.match(/<(a|button|input|select|textarea)\b/g)).toHaveLength(1);
  });

  it('sends a staff user of another role to the home of their role, in the app', () => {
    const html = render('en', createElement(NotAuthorizedPage, { variant: 'staff', homePath: '/m' }));
    expect(html).toContain('href="/m"');
  });

  it('sends a customer to the customer portal with a full navigation', () => {
    const html = render('en', createElement(NotAuthorizedPage, { variant: 'customer' }));
    expect(html).toContain('href="/service"');
  });
});

describe('TicketStaffView', () => {
  const view = (decision: TicketStaffDecision, role: UserRole | undefined): string =>
    render(
      'en',
      createElement(TicketStaffView, {
        decision,
        role,
        from: here,
        children: createElement('p', null, 'SECRET-TICKET-DATA'),
      }),
    );

  it('renders the children only when the decision is allow', () => {
    expect(view('allow', 'technician')).toContain('SECRET-TICKET-DATA');
    for (const decision of ['wait', 'not-authorized'] as const) {
      expect(view(decision, decision === 'not-authorized' ? 'accounting' : undefined)).not.toContain(
        'SECRET-TICKET-DATA',
      );
    }
  });

  it('redirects to the existing sign-in and remembers where the visitor was going (R1.13)', () => {
    // TicketStaffView uses no hooks, so it can be called to inspect the element it returns. Rendering a
    // <Navigate> in a static router only logs a warning and shows nothing.
    const element = TicketStaffView({
      decision: 'sign-in',
      role: undefined,
      from: here,
      children: createElement('p', null, 'SECRET-TICKET-DATA'),
    }) as ReactElement<{ to: string; replace: boolean; state: { from: typeof here } }>;
    expect(element.type).toBe(Navigate);
    expect(element.props.to).toBe('/login');
    expect(element.props.replace).toBe(true);
    expect(element.props.state).toEqual({ from: here });
  });

  it('waits with an empty busy page', () => {
    const html = view('wait', undefined);
    expect(html).toContain('aria-busy="true"');
    expect(html).not.toContain('<h1');
  });

  it('shows the staff not-authorized page to another role and the customer one when there is no role', () => {
    const staff = view('not-authorized', 'accounting');
    expect(staff).toContain(en.management.notAuthorized.title);
    expect(staff).toContain('href="/"');
    const customer = view('not-authorized', undefined);
    expect(customer).toContain(en.management.notAuthorized.title);
    expect(customer).toContain('href="/service"');
  });

  it('sends an employee who opens a management URL to the mobile home, with no data', () => {
    const html = view('not-authorized', 'employee');
    expect(html).toContain('href="/m"');
    expect(html).not.toContain('SECRET-TICKET-DATA');
  });
});

describe('ConfigStaffView', () => {
  const view = (language: PortalLanguage, decision: 'allow' | 'unavailable'): string =>
    render(language, createElement(ConfigStaffView, { decision, children: createElement('form', null, 'CONFIG-VALUES') }));

  it('renders the screen for config staff', () => {
    expect(view('en', 'allow')).toContain('CONFIG-VALUES');
  });

  it.each(['en', 'he'] as const)('shows a technician "not available" in %s with no values and no controls', (language) => {
    const html = view(language, 'unavailable');
    expect(html).not.toContain('CONFIG-VALUES');
    expect(html).toContain(text[language].management.configUnavailable.title);
    expect(html).toContain(text[language].management.configUnavailable.body);
    expect(html.match(/<(a|button|input|select|textarea|form)\b/g)).toBeNull();
  });
});
