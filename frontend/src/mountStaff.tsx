import { QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from '@/App';
import { AuthProvider } from '@/auth/AuthProvider';
import { queryClient } from '@/lib/queryClient';
// Initialises i18next and sets the document language and direction.
import '@/i18n';
import '@/styles/global.css';

/**
 * The staff app, exactly as `main.tsx` mounted it before the customer portal existed. It lives in its own
 * module so that `main.tsx` can load either this or the portal, and the portal never evaluates the staff
 * `AuthProvider`, the global i18next (which writes `<html lang dir>` at import) or `global.css`.
 */
export function mountStaff(container: HTMLElement): void {
  createRoot(container).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <AuthProvider>
            <App />
          </AuthProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </StrictMode>,
  );
}
