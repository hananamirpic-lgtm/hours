/**
 * Route guards.
 *
 * `RequireAuth` keeps a signed-out visitor out of every screen but the login one, and sends an
 * employee who wanders into the console back to their mobile home (and vice versa). It is a
 * convenience and a routing aid, not the security boundary — every endpoint behind these screens is
 * enforced again server-side, which is where an attacker who edits the URL actually meets the wall.
 */

import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';

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

/** The login route redirects an already-signed-in user to their home rather than showing the form. */
export function RedirectIfAuthenticated({ children }: { children: ReactNode }) {
  const { user, isReady } = useAuth();
  if (!isReady) {
    return <FullPageSpinner />;
  }
  if (user) {
    return <Navigate to={homePathFor(user.role)} replace />;
  }
  return <>{children}</>;
}

function FullPageSpinner() {
  // No text: this is on screen for a fraction of a second during session resolution, and any label
  // would flash. `aria-busy` tells assistive technology the page is settling.
  return <div className="full-page" aria-busy="true" />;
}
