/**
 * The console navigation, and who may see each item (Requirement 2, front-end half).
 *
 * Every route is enforced again server-side — hiding a link a role cannot use is a courtesy, not the
 * control. The `labelKey` is a translation key, never a label, so the menu is bilingual by
 * construction and the lint rule has nothing to flag.
 */

import type { UserRole } from '@/api/types';

export interface NavItem {
  /** Path relative to the console root. */
  to: string;
  labelKey: string;
  /** Roles permitted to see the item. The admin sees everything regardless (see `navFor`). */
  roles: UserRole[];
}

const FINANCE: UserRole[] = ['accounting'];
const MANAGERS: UserRole[] = ['site_manager', 'accounting'];

/**
 * Console items in menu order. Admin is omitted from each `roles` list because admin sees all of them
 * — `navFor` adds that rule once rather than repeating it on every line.
 */
export const CONSOLE_NAV: NavItem[] = [
  { to: '/', labelKey: 'nav.dashboard', roles: MANAGERS },
  { to: '/employees', labelKey: 'nav.employees', roles: MANAGERS },
  { to: '/staffing-companies', labelKey: 'nav.staffingCompanies', roles: [] },
  { to: '/clients', labelKey: 'nav.clients', roles: [] },
  { to: '/sites', labelKey: 'nav.sites', roles: MANAGERS },
  { to: '/hours', labelKey: 'nav.hours', roles: MANAGERS },
  { to: '/approvals', labelKey: 'nav.approvals', roles: ['site_manager'] },
  { to: '/periods', labelKey: 'nav.periods', roles: [] },
  { to: '/payroll', labelKey: 'nav.payroll', roles: FINANCE },
  { to: '/billing', labelKey: 'nav.billing', roles: FINANCE },
  { to: '/reports', labelKey: 'nav.reports', roles: MANAGERS },
  { to: '/users', labelKey: 'nav.users', roles: [] },
  { to: '/audit', labelKey: 'nav.audit', roles: MANAGERS },
  { to: '/settings', labelKey: 'nav.settings', roles: [] },
];

/** The console destinations an operations admin must never see: they are the money-only screens. */
const MONEY_ONLY_PATHS = new Set(['/payroll', '/billing']);

/**
 * The nav items a role may see.
 *
 * Admin sees all. An operations admin is a full operational administrator with no financial
 * visibility, so it sees the admin set minus the money-only destinations (Payroll, Billing); the
 * Reports item stays, but the Reports page itself hides the money (profitability) tab from the role,
 * and the API omits money figures regardless. Every other role sees only what names it.
 */
export const navFor = (role: UserRole): NavItem[] => {
  if (role === 'admin') {
    return CONSOLE_NAV;
  }
  if (role === 'operations_admin') {
    return CONSOLE_NAV.filter((item) => !MONEY_ONLY_PATHS.has(item.to));
  }
  return CONSOLE_NAV.filter((item) => item.roles.includes(role));
};

/** Where a role lands after signing in: employees on the mobile tree, everyone else on the console. */
export const homePathFor = (role: UserRole): string => (role === 'employee' ? '/m' : '/');


/**
 * Whether a role is an operational administrator: the full admin, or the operations admin who has the
 * same operational powers but no financial visibility. Management screens (employees, sites, clients,
 * staffing companies, approvals, hours) gate their create/edit/delete/upload controls on this, so the
 * operations admin gets the operational actions while money stays hidden by the API's redaction. Use
 * this instead of comparing to 'admin' directly, so a new operational role is granted in one place.
 */
export const isOperationalAdmin = (role: UserRole | undefined): boolean =>
  role === 'admin' || role === 'operations_admin';
