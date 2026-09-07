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

/** The nav items a role may see. Admin sees all; everyone else sees what names their role. */
export const navFor = (role: UserRole): NavItem[] =>
  role === 'admin' ? CONSOLE_NAV : CONSOLE_NAV.filter((item) => item.roles.includes(role));

/** Where a role lands after signing in: employees on the mobile tree, everyone else on the console. */
export const homePathFor = (role: UserRole): string => (role === 'employee' ? '/m' : '/');
