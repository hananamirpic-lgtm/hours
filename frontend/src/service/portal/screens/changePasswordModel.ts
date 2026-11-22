// The change-password screen's view-model (R6.4 to R6.6, R6.9, R22.8, R23.2, R23.13). Pure.
import { errorTextKey } from '../formErrors';
import { buildFeedback, failureOf, waitTimeOf, type AuthFeedback } from './authFeedback';

/** The inputs in reading order. */
export const CHANGE_PASSWORD_FIELDS = ['current_password', 'new_password'] as const;
export type ChangePasswordField = (typeof CHANGE_PASSWORD_FIELDS)[number];

export interface ChangePasswordValues {
  readonly current_password: string;
  readonly new_password: string;
}

export const EMPTY_CHANGE_PASSWORD_VALUES: ChangePasswordValues = { current_password: '', new_password: '' };

/** The body of `POST /service/auth/change-password`. */
export interface ChangePasswordBody {
  readonly current_password: string;
  readonly new_password: string;
}

export const CHANGE_PASSWORD_IDS = {
  current_password: 'portal-change-password-current',
  new_password: 'portal-change-password-new',
  currentHint: 'portal-change-password-current-hint',
  newHint: 'portal-change-password-new-hint',
  currentError: 'portal-change-password-current-error',
  newError: 'portal-change-password-new-error',
  formError: 'portal-change-password-error',
  submit: 'portal-change-password-submit',
} as const;

/** Field name to the id of its input, for moving focus. */
export const CHANGE_PASSWORD_FIELD_IDS: Readonly<Record<string, string>> = {
  current_password: CHANGE_PASSWORD_IDS.current_password,
  new_password: CHANGE_PASSWORD_IDS.new_password,
};

/** The request body. Both passwords are sent exactly as typed: the server judges them (R6.4, R6.6). */
export function buildChangePasswordBody(values: ChangePasswordValues): ChangePasswordBody {
  return { current_password: values.current_password, new_password: values.new_password };
}

/**
 * What a failed change shows. The customer stays signed in on the screen for every row except the last
 * two 401 cases (R6.9):
 *
 *   401 `invalid_credentials`      `portal.changePassword.currentPasswordWrong` beside the current-password
 *                                  field, focus on it
 *   422 `invalid_password_format`, `password_not_changed`
 *                                  `errors.<code>` beside the new-password field, focus on it
 *   429 (any code)                 `errors.<code>` beside the current-password field with the wait time from
 *                                  `Retry-After`, announced, focus stays
 *   401 any other code             form message `errors.<code>`: the portal client has already dropped the
 *                                  token, so the gate shows sign-in (R8.11); this text only covers a stale 401
 *   anything else                  form message `errors.<code>`, `errors.generic` for network, timeout, an
 *                                  unreadable body or a code without text (R8.12)
 */
export function mapChangePasswordError(error: unknown): AuthFeedback {
  const failure = failureOf(error);
  if (failure.status === 401 && failure.code === 'invalid_credentials') {
    return buildFeedback(CHANGE_PASSWORD_FIELDS, {
      fieldErrors: { current_password: 'portal.changePassword.currentPasswordWrong' },
    });
  }
  if (failure.status === 422 && (failure.code === 'invalid_password_format' || failure.code === 'password_not_changed')) {
    return buildFeedback(CHANGE_PASSWORD_FIELDS, { fieldErrors: { new_password: errorTextKey(failure.code) } });
  }
  if (failure.status === 429) {
    return buildFeedback(CHANGE_PASSWORD_FIELDS, {
      fieldErrors: { current_password: errorTextKey(failure.code) },
      waitTime: waitTimeOf(failure),
      announce: true,
      moveFocus: false,
    });
  }
  return buildFeedback(CHANGE_PASSWORD_FIELDS, { formMessage: errorTextKey(failure.code), announce: true });
}
