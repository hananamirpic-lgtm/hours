/** The Portal_Path: the customer portal lives under this path of the same host (R8.1). */
export const PORTAL_PATH = '/service';

/**
 * True for `/service` and `/service/...`, false for everything else, including `/services`, `/service-x`
 * and `/serviceable`. `src/main.tsx` uses it to choose between the portal and the staff app.
 */
export function isPortalPath(pathname: string): boolean {
  return pathname === PORTAL_PATH || pathname.startsWith(`${PORTAL_PATH}/`);
}
