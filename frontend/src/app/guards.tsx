/**
 * Route guards.
 *
 * `RequireAuth` keeps a signed-out visitor out of every screen but the login one, and sends an
 * employee who wanders into the console back to their mobile home (and vice versa). It is a
 * convenience and a routing aid, not the security boundary — every endpoint behind these screens is
 * enforced again server-side, which is where an attacker who edits the URL actually meets the wall.
 */

import type { ReactNode } from 'react';
import { Navigate, useLocation, type Location } from 'react-router-dom';

import type { UserRole } from '@/api/types';
import { useAuth } from '@/auth/AuthProvider';
import { homePathFor } from '@/app/navigation';

/** Which tree a role belongs to, so a guard can bounce a caller who is on the wrong one. */
const treeForRole = (role: UserRole): 'mobile' | 'console' => (role === 'employee' ? 'mobile' : 'console');

export function RequireAuth({ tree, children }: { tree: 'mobile' | 'console'; children: ReactNode }) {
  const { user, isReady } = useAuth();
  const location = useLocation();

  if (!isReady) {
    return <FullPageSpinner />;
  }
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  if (treeForRole(user.role) !== tree) {
    return <Navigate to={homePathFor(user.role)} replace />;
  }
  return <>{children}</>;
}

/**
 * The login route redirects an already-signed-in user away from the form.
 *
 * When a guard bounced them here it stored the page they were trying to reach as `state.from` (for
 * example the `/m/scan?token=...` a QR opened). Honouring it means sign-in continues to that page —
 * the token survives the login round-trip and the scan check-in completes automatically. With no
 * `from`, the user lands on their role's home as before. `from` is only followed when it belongs to
 * the user's own tree, so a stored console path cannot pull an employee off the mobile app.
 */
export function RedirectIfAuthenticated({ children }: { children: ReactNode }) {
  const { user, isReady } = useAuth();
  const location = useLocation();
  if (!isReady) {
    return <FullPageSpinner />;
  }
  if (user) {
    const home = homePathFor(user.role);
    const from = (location.state as { from?: Location } | null)?.from;
    const target = from ? `${from.pathname}${from.search}${from.hash}` : home;
    const wantsMobile = target.startsWith('/m');
    const onOwnTree = wantsMobile === (user.role === 'employee');
    return <Navigate to={onOwnTree ? target : home} replace />;
  }
  return <>{children}</>;
}

function FullPageSpinner() {
  // No text: this is on screen for a fraction of a second during session resolution, and any label
  // would flash. `aria-busy` tells assistive technology the page is settling.
  return <div className="full-page" aria-busy="true" />;
}
