/**
 * The technician gate (Requirements 2.3, 2.4, 20.15, 21.7).
 *
 * It sits inside `ChangePasswordGate` (a forced password change stays first) and above the console
 * shell, so no Hours screen renders for a technician. The decision is `technicianRouting`:
 *
 *  - not a technician  -> the children, at once. The flag is not consulted for these roles.
 *  - technician, flag on       -> `<Navigate to="/tickets" replace />`
 *  - technician, off/unknown   -> the unavailable notice with sign-out, no redirect
 *  - technician, flag loading  -> a neutral status, so the notice does not flash
 */

import type { ReactNode } from 'react';
import { Navigate } from 'react-router-dom';

import { useAuth } from '@/auth/AuthProvider';
import { useFeatures } from '../api/features';
import { LoadingState } from './FlagGate';
import { technicianRouting } from './technicianRouting';
import { TicketsUnavailable } from './TicketsUnavailable';

/** The Management_Path. */
export const MANAGEMENT_PATH = '/tickets';

function TechnicianFlagGate() {
  const { state } = useFeatures();
  switch (technicianRouting('technician', state)) {
    case 'redirect':
      return <Navigate to={MANAGEMENT_PATH} replace />;
    case 'notice':
      return <TicketsUnavailable />;
    case 'loading':
    case 'pass':
      return <LoadingState />;
  }
}

export function TechnicianGate({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  // Only a technician reads the flag here, so every other role renders at once.
  if (technicianRouting(user?.role, 'loading') === 'pass') {
    return <>{children}</>;
  }
  return <TechnicianFlagGate />;
}
