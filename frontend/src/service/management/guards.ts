/**
 * The pure decisions behind the management guards and navigation (Requirements 1.13, 19.1, 19.16).
 *
 * The guard components read the staff session and the customer token and pass what they found to these
 * functions, so every branch is testable without a DOM or an auth provider. Like the route guards of the
 * Hours console, the guards are a courtesy: the Ticket_API checks the role again on every request.
 */
import type { UserRole } from '@/api/types';
import { isOperationalAdmin } from '@/app/navigation';

/** The roles served by the staff endpoints of the Ticket_API (R1.2). */
export const TICKET_STAFF_ROLES: readonly UserRole[] = ['admin', 'operations_admin', 'technician'];

/** True for a role that may use the Management_Interface. */
export function isTicketStaffRole(role: UserRole | undefined): boolean {
  return role !== undefined && TICKET_STAFF_ROLES.includes(role);
}

/** True for Config_Staff: the administrator and the operations administrator, never the technician (R19.1). */
export function isConfigStaffRole(role: UserRole | undefined): boolean {
  return isOperationalAdmin(role);
}

/**
 * What `RequireTicketStaff` does:
 *
 *   wait            the session is still being resolved: a neutral, empty page
 *   sign-in         no staff session and no customer token: the existing sign-in, remembering where we were
 *   not-authorized  a staff role outside the three, or a customer token with no staff session
 *   allow           a ticket staff session
 */
export type TicketStaffDecision = 'wait' | 'sign-in' | 'not-authorized' | 'allow';

export interface TicketStaffInput {
  /** The auth provider finished resolving the session. */
  readonly isReady: boolean;
  /** The role of the signed-in staff user, or undefined with no staff session. */
  readonly role: UserRole | undefined;
  /** The portal storage key holds a live customer token. */
  readonly hasCustomerToken: boolean;
}

export function ticketStaffDecision({ isReady, role, hasCustomerToken }: TicketStaffInput): TicketStaffDecision {
  if (!isReady) return 'wait';
  if (role === undefined) {
    // A signed-in customer who opens a Management screen sees the not-authorized page, not the sign-in (R1.13).
    return hasCustomerToken ? 'not-authorized' : 'sign-in';
  }
  return isTicketStaffRole(role) ? 'allow' : 'not-authorized';
}

/** What `RequireConfigStaff` does: Config_Staff see the screen, a technician sees "not available" (R19.16). */
export type ConfigStaffDecision = 'allow' | 'unavailable';

export function configStaffDecision(role: UserRole | undefined): ConfigStaffDecision {
  return isConfigStaffRole(role) ? 'allow' : 'unavailable';
}

/** One navigation entry of the management layout: a path and a `service` namespace translation key. */
export interface ManagementNavEntry {
  readonly to: string;
  readonly labelKey: string;
  /** True when the entry matches only its own path, not the paths below it. */
  readonly end: boolean;
}

/** The entries every ticket staff member sees. */
export const TICKET_NAV: readonly ManagementNavEntry[] = [
  { to: '/tickets', labelKey: 'management.nav.dashboard', end: true },
  { to: '/tickets/list', labelKey: 'management.nav.tickets', end: false },
  { to: '/tickets/customers', labelKey: 'management.nav.customers', end: false },
  { to: '/tickets/reports', labelKey: 'management.nav.reports', end: false },
];

/** The configuration entries, shown to Config_Staff only (R19.1). */
export const CONFIG_NAV: readonly ManagementNavEntry[] = [
  { to: '/tickets/settings/categories', labelKey: 'management.nav.categories', end: false },
  { to: '/tickets/settings/labels', labelKey: 'management.nav.labels', end: false },
  { to: '/tickets/settings/options', labelKey: 'management.nav.options', end: false },
];

export interface ManagementNav {
  readonly main: readonly ManagementNavEntry[];
  /** Empty for a technician. */
  readonly configuration: readonly ManagementNavEntry[];
}

/** The navigation for a role. A technician gets no configuration entry. */
export function managementNav(role: UserRole | undefined): ManagementNav {
  return { main: TICKET_NAV, configuration: isConfigStaffRole(role) ? CONFIG_NAV : [] };
}
