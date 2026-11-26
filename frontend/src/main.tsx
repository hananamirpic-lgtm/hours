import { isPortalPath } from '@/service/portalPath';

const container = document.getElementById('root');
if (!container) {
  throw new Error('Root element #root is missing from index.html');
}
// Two separate apps share this entry. Each is a dynamic chunk, so the portal never loads the staff
// AuthProvider, global i18next or global.css, and the staff app never loads the portal chunk.
if (isPortalPath(location.pathname)) {
  void import('@/service/portal/mountPortal').then((m) => m.mountPortal(container));
} else {
  void import('@/mountStaff').then((m) => m.mountStaff(container));
}
