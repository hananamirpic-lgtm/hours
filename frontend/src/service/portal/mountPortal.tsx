// Entry of the customer portal. `src/main.tsx` imports this module dynamically when the path is
// `/service` or starts with `/service/`, so it is a separate chunk that the staff app never loads.
//
// Isolation from the staff app, by construction:
//  - no `AuthProvider`, `apiFetch`, staff token getter, `@/i18n` or `global.css` is imported here or by
//    anything under `src/service/portal` or `src/service/api/portalFetch.ts` (a test checks the imports);
//  - its own `QueryClient`, its own i18next instance (`portalI18n`), its own token store and its own
//    stylesheet `portal.css`, which only this module imports.
import { QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { I18nextProvider } from 'react-i18next';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { PortalApp } from './PortalApp';
import { customerTokenStore } from './customerToken';
import { initPortalI18n } from './portalI18n';
import { clearCacheWhenSignedOut, createPortalQueryClient } from './portalQueryClient';
import './portal.css';

/** The router basename: the Portal_Path. */
export const PORTAL_BASENAME = '/service';

export function mountPortal(container: HTMLElement): void {
  // Sets <html lang dir> from the stored choice (Hebrew when there is none or it is unusable).
  const i18n = initPortalI18n();
  const queryClient = createPortalQueryClient();
  clearCacheWhenSignedOut(queryClient, customerTokenStore);
  createRoot(container).render(
    <StrictMode>
      <I18nextProvider i18n={i18n}>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter basename={PORTAL_BASENAME}>
            <PortalApp />
          </BrowserRouter>
        </QueryClientProvider>
      </I18nextProvider>
    </StrictMode>,
  );
}
