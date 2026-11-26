/**
 * The management dashboard (task 24.2; R10.9, R17.1, R17.4 to R17.7): three counts of open work, each a
 * link to the ticket list with its filters, reloaded every 30 seconds while the tab is visible.
 *
 *   DashboardPage  container: the query, tab visibility, the role.
 *   DashboardView  markup (`../dashboardView`), decided by `dashboardViewState` (`../dashboardModel`).
 *
 * A failed load leaves `data` as it was, so the old counts stay with the "not current" label and the error
 * message until a later load succeeds (R17.6, R17.7).
 */
import { useQuery } from '@tanstack/react-query';
import { useSyncExternalStore } from 'react';
import { useAuth } from '@/auth/AuthProvider';
import { getDashboard, StaffApiError, staffKeys } from '../../api/staff';
import { GENERIC_CODE } from '../../api/portalFetch';
import { dashboardQueryOptions, dashboardViewState, isDashboardResponse, isVisibleState } from '../dashboardModel';
import { DashboardView } from '../dashboardView';
import { isConfigStaffRole } from '../guards';
import '../i18n';

function subscribeToVisibility(onChange: () => void): () => void {
  document.addEventListener('visibilitychange', onChange);
  return () => document.removeEventListener('visibilitychange', onChange);
}

/** True while the browser tab is visible. */
function useTabVisible(): boolean {
  return useSyncExternalStore(
    subscribeToVisibility,
    () => isVisibleState(document.visibilityState),
    () => true,
  );
}

export function DashboardPage() {
  const { user } = useAuth();
  const visible = useTabVisible();

  const query = useQuery({
    queryKey: staffKeys.dashboard,
    queryFn: async ({ signal }) => {
      const body = await getDashboard(signal);
      // A body with the wrong shape is a failed load, so the old counts stay.
      if (!isDashboardResponse(body)) throw new StaffApiError(GENERIC_CODE);
      return body;
    },
    ...dashboardQueryOptions(visible),
  });

  const state = dashboardViewState({
    data: query.data,
    isError: query.isError,
    isConfigStaff: isConfigStaffRole(user?.role),
  });

  return <DashboardView state={state} />;
}
