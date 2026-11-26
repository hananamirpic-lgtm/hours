/**
 * The guard of the three configuration screens (Requirements 19.1, 19.16). It sits inside
 * `RequireTicketStaff`, so the visitor is already ticket staff. Config_Staff see the screen. A technician
 * sees a "not available" panel that renders none of the screen: its component is never mounted, so no
 * request is sent and no value or control exists. The API refuses a technician on these endpoints anyway
 * (403 `insufficient_role`).
 */
import type { ReactNode } from 'react';
import { useAuth } from '@/auth/AuthProvider';
import { configStaffDecision } from './guards';
import { ConfigStaffView } from './GuardViews';
import './i18n';

export function RequireConfigStaff({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  return <ConfigStaffView decision={configStaffDecision(user?.role)}>{children}</ConfigStaffView>;
}
