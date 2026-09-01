/**
 * Turn a thrown error into a translation key and its parameters, so a screen can render a failure in
 * the user's language from the machine `code` the API returns. The backend is locale-neutral by
 * design; the words live in the resource files under `apiError.<code>`.
 */

import { ApiError } from '@/api/client';

export interface TranslatableError {
  key: string;
  params: Record<string, string>;
}

export const toError = (error: unknown): TranslatableError => {
  if (error instanceof ApiError && error.code) {
    return { key: `apiError.${error.code}`, params: error.params };
  }
  return { key: 'apiError.unknown', params: {} };
};

/** Whether an error is a conflict that offers a named recorded action, e.g. `archive` on a client. */
export const hasAction = (error: unknown, action: string): boolean =>
  error instanceof ApiError && error.actions.includes(action);
