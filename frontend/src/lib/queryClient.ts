import { QueryClient } from '@tanstack/react-query';

/**
 * Shared query client. Attendance data changes on every scan, so nothing is cached for long;
 * individual queries widen this where it is safe to.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});
