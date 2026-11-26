/**
 * The guard of every Management_Interface screen (Requirement 1.13), inside the `FlagGate`.
 *
 *  - no staff session            -> `<Navigate to="/login" state={{from}}>`, the existing staff sign-in. The
 *                                   login route's `RedirectIfAuthenticated` returns to `from` after sign-in.
 *  - no staff session, but a customer token in the portal storage
 *                                -> the not-authorized page, not the sign-in
 *  - a role outside admin, operations_admin, technician
 *                                -> the not-authorized page, with no data
 *
 * The decision itself is `ticketStaffDecision` (pure, tested). This component only reads the session.
 */
import type { ReactNode } from 'react';
import { useLocation } from 'react-router-dom';
import { useAuth } from '@/auth/AuthProvider';
import { customerTokenStore } from '../portal/customerToken';
import { ticketStaffDecision } from './guards';
import { TicketStaffView } from './GuardViews';
import './i18n';

export function RequireTicketStaff({ children }: { children: ReactNode }) {
  const { user, isReady } = useAuth();
  const location = useLocation();
  const role = user?.role;
  const decision = ticketStaffDecision({
    isReady,
    role,
    hasCustomerToken: customerTokenStore.getToken() !== null,
  });
  return (
    <TicketStaffView decision={decision} role={role} from={location}>
      {children}
    </TicketStaffView>
  );
}
