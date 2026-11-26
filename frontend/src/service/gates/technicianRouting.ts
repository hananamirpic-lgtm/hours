/**
 * The pure decisions behind the technician gate and the console navigation entry
 * (Requirements 2.3, 2.4, 20.8, 20.9, 20.10, 20.15, 21.6, 21.7).
 *
 * Both take the role and the flag state as the gates see it. 'unknown' (the flag could not be read)
 * and 'loading' (no answer yet) never grant anything: they behave as off for the navigation entry.
 */

import type { UserRole } from '@/api/types';
import { isOperationalAdmin, navFor, type NavItem } from '@/app/navigation';
import type { FlagGateState } from '../api/features';

/**
 * What the technician gate does.
 *
 *  - pass      the role is not `technician`: render the children at once, no wait for the flag
 *  - loading   a technician and the flag is not known yet: a neutral status, no notice flash
 *  - redirect  a technician and the flag is on: navigate to the Management_Path
 *  - notice    a technician and the flag is off or unknown: the unavailable message and sign-out
 */
export type TechnicianRouting = 'pass' | 'loading' | 'redirect' | 'notice';

export function technicianRouting(role: UserRole | undefined, flag: FlagGateState): TechnicianRouting {
  if (role !== 'technician') {
    return 'pass';
  }
  switch (flag) {
    case 'on':
      return 'redirect';
    case 'loading':
      return 'loading';
    case 'off':
    case 'unknown':
      return 'notice';
  }
}

/** A console navigation entry as the shell renders it: a path and a translation key. */
export type ConsoleNavEntry = Pick<NavItem, 'to' | 'labelKey'>;

/** The one entry added for the management interface. */
export const TICKETS_NAV_ITEM: ConsoleNavEntry = { to: '/tickets', labelKey: 'nav.tickets' };

/**
 * The console navigation: exactly what `navFor(role)` returns, plus `TICKETS_NAV_ITEM` at the end
 * when the flag is on and the role is `admin` or `operations_admin`. `CONSOLE_NAV` and `navFor` are
 * not touched, so existing entries and their order are unchanged for every role. A technician gets
 * `navFor`'s result, which holds no Hours entry, and never the tickets entry. Accounting, site
 * manager and employee never get the tickets entry, whatever the flag.
 */
export function consoleNavItems(role: UserRole, flag: FlagGateState): ConsoleNavEntry[] {
  const items: ConsoleNavEntry[] = navFor(role);
  if (flag === 'on' && isOperationalAdmin(role)) {
    return [...items, TICKETS_NAV_ITEM];
  }
  return items;
}
