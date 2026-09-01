import type { HealthResponse, ReadinessResponse } from './types';

import { apiFetch } from './client';

export const healthKeys = {
  liveness: ['health', 'liveness'] as const,
  readiness: ['health', 'readiness'] as const,
};

export const getHealth = (): Promise<HealthResponse> =>
  apiFetch<HealthResponse>('/health', { anonymous: true });

/**
 * Readiness answers with 503 when a dependency is down, and that body is the answer we want to
 * display, so the status code is accepted rather than thrown.
 */
export const getReadiness = (): Promise<ReadinessResponse> =>
  apiFetch<ReadinessResponse>('/health/ready', { acceptStatuses: [503], anonymous: true });
