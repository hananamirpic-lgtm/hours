// The sign-in screen's view-model: request body, API error to feedback, element ids (R5.10, R5.12,
// R22.8, R23.1, R23.2, R23.13). Pure: no React, no DOM, no i18n instance.
import { errorTextKey } from '../formErrors';
import { buildFeedback, failureOf, waitTimeOf, type AuthFeedback } from './authFeedback';

/** The inputs in reading order (the DOM order), which is the order `firstFailingField` walks. */
export const SIGN_IN_FIELDS = ['phone', 'password'] as const;
export type SignInField = (typeof SIGN_IN_FIELDS)[number];

export interface SignInValues {
  readonly phone: string;
  readonly password: string;
}

export const EMPTY_SIGN_IN_VALUES: SignInValues = { phone: '', password: '' };

/** The body of `POST /service/auth/sign-in`. */
export interface SignInBody {
  readonly phone: string;
  readonly password: string;
}

/** Element ids. Constants, because there is one sign-in form on a page. */
export const SIGN_IN_IDS = {
  phone: 'portal-sign-in-phone',
  password: 'portal-sign-in-password',
  phoneHint: 'portal-sign-in-phone-hint',
  passwordHint: 'portal-sign-in-password-hint',
  phoneError: 'portal-sign-in-phone-error',
  formError: 'portal-sign-in-error',
  sessionExpired: 'portal-sign-in-session-expired',
  submit: 'portal-sign-in-submit',
} as const;

/** Field name to the id of its input, for moving focus. */
export const SIGN_IN_FIELD_IDS: Readonly<Record<string, string>> = {
  phone: SIGN_IN_IDS.phone,
  password: SIGN_IN_IDS.password,
};

// Directional marks that a phone number copied out of right-to-left text can carry. The server would
// read them as characters of the number and answer `invalid_phone`.
const DIRECTIONAL_MARKS = /[\u200e\u200f\u061c\u202a-\u202e\u2066-\u2069]/g;

/** The phone as typed, without directional marks and surrounding whitespace. The server normalises the spelling. */
export function cleanPhoneInput(raw: string): string {
  return raw.replace(DIRECTIONAL_MARKS, '').trim();
}

/** The request body. The password is sent exactly as typed: the server judges it (R5.4). */
export function buildSignInBody(values: SignInValues): SignInBody {
  return { phone: cleanPhoneInput(values.phone), password: values.password };
}

/**
 * What a failed sign-in shows (the table is in the task report):
 *
 *   401 (any code)                 form message `portal.signIn.error`, the same text for an unknown phone,
 *                                  an inactive customer and a wrong or malformed password (R5.10)
 *   422 `invalid_phone`            error beside the phone field, focus on it
 *   429 `too_many_attempts` / `rate_limited` (any 429)
 *                                  form message `errors.<code>` and the wait time from `Retry-After`
 *   anything else                  form message `errors.<code>`, `errors.generic` for network, timeout,
 *                                  an unreadable body or a code without text (R8.12)
 */
export function mapSignInError(error: unknown): AuthFeedback {
  const failure = failureOf(error);
  if (failure.status === 401) {
    return buildFeedback(SIGN_IN_FIELDS, { formMessage: 'portal.signIn.error', announce: true });
  }
  if (failure.status === 422 && failure.code === 'invalid_phone') {
    return buildFeedback(SIGN_IN_FIELDS, { fieldErrors: { phone: errorTextKey(failure.code) } });
  }
  if (failure.status === 429) {
    return buildFeedback(SIGN_IN_FIELDS, {
      formMessage: errorTextKey(failure.code),
      waitTime: waitTimeOf(failure),
      announce: true,
    });
  }
  return buildFeedback(SIGN_IN_FIELDS, { formMessage: errorTextKey(failure.code), announce: true });
}
