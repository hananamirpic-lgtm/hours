// Shared, pure pieces of the sign-in and change-password screens (R5.10, R6.9, R22.8, R23.2, R23.13,
// R23.14). No React, no DOM, no i18n instance: the view-models return TRANSLATION KEYS and a wait time,
// and the screens translate them when they render, so a language switch changes the text of a message
// that is already on screen (R8.6).
import { isPortalApiError } from '../../api/portalFetch';
import { firstFailingField } from '../formErrors';
import { waitTimeMessage, type WaitTime } from '../waitTime';

/** What a failed attempt shows. Built by `mapSignInError` and `mapChangePasswordError`. */
export interface AuthFeedback {
  /** Field name to translation key. The message is shown beside that field and linked with `aria-describedby`. */
  readonly fieldErrors: Readonly<Record<string, string>>;
  /** Translation key of a message that belongs to no single field, or null. */
  readonly formMessage: string | null;
  /**
   * The wait time of a 429, shown after the message it belongs to (the form message on sign-in, the
   * current-password message on change-password). Null for every other failure.
   */
  readonly waitTime: WaitTime | null;
  /** The first failing field in reading order, which takes keyboard focus (R23.13). Null: focus stays. */
  readonly focus: string | null;
  /** True when the message belongs to no single field and is written to the alert live region (R23.14). */
  readonly announce: boolean;
}

export const EMPTY_FEEDBACK: AuthFeedback = {
  fieldErrors: {},
  formMessage: null,
  waitTime: null,
  focus: null,
  announce: false,
};

/** The parts of a failure the mapping needs. */
export interface Failure {
  readonly status: number;
  readonly code: string;
  readonly retryAfter: string | null;
}

/**
 * A caught value as a `Failure`. A `PortalApiError` keeps its status, code and `Retry-After`; anything
 * else (a programming error, a rejected non-API value) is the same as no usable answer: status 0, code
 * `generic`, which every mapping turns into `errors.generic`.
 */
export function failureOf(error: unknown): Failure {
  if (isPortalApiError(error)) {
    return { status: error.status, code: error.code, retryAfter: error.retryAfter };
  }
  return { status: 0, code: 'generic', retryAfter: null };
}

/** The wait time of a 429 from its raw `Retry-After` value (R22.8): seconds up to 60, minutes above, none otherwise. */
export function waitTimeOf(failure: Failure): WaitTime {
  return waitTimeMessage(failure.retryAfter);
}

export interface FeedbackParts {
  fieldErrors?: Readonly<Record<string, string>>;
  formMessage?: string;
  waitTime?: WaitTime;
  announce?: boolean;
  /** False for a message that is announced instead of moving focus (a 429). Default true. */
  moveFocus?: boolean;
}

/**
 * Builds a feedback value. `focus` is the first failing field of `order` (R23.13), unless the message is
 * announced instead (`moveFocus: false`), so one failure never both moves focus and writes the alert region.
 */
export function buildFeedback(order: readonly string[], parts: FeedbackParts): AuthFeedback {
  const fieldErrors = parts.fieldErrors ?? {};
  return {
    fieldErrors,
    formMessage: parts.formMessage ?? null,
    waitTime: parts.waitTime ?? null,
    focus: parts.moveFocus === false ? null : firstFailingField(fieldErrors, order),
    announce: parts.announce ?? false,
  };
}

/** The feedback without the errors of fields the customer has edited since, so a stale `aria-invalid` goes away. */
export function withoutFieldErrors(feedback: AuthFeedback, edited: readonly string[]): AuthFeedback {
  if (edited.length === 0) return feedback;
  const fieldErrors: Record<string, string> = {};
  for (const [name, key] of Object.entries(feedback.fieldErrors)) {
    if (!edited.includes(name)) fieldErrors[name] = key;
  }
  // The wait time belongs to the 429 message; once its field is edited the message is gone with it.
  const waitStillShown = feedback.formMessage !== null || Object.keys(fieldErrors).length > 0;
  return {
    ...feedback,
    fieldErrors,
    waitTime: waitStillShown ? feedback.waitTime : null,
  };
}

/** The translation key of a field's error, or null. */
export function fieldErrorKey(feedback: AuthFeedback, field: string): string | null {
  return Object.prototype.hasOwnProperty.call(feedback.fieldErrors, field) ? (feedback.fieldErrors[field] ?? null) : null;
}

/** `aria-describedby` value: the given ids in order, skipping empty ones; undefined when there is none. */
export function describedBy(ids: readonly (string | null | false | undefined)[]): string | undefined {
  const present = ids.filter((id): id is string => typeof id === 'string' && id !== '');
  return present.length === 0 ? undefined : present.join(' ');
}

/** The `t` function as the helpers below use it. */
export type Translate = (key: string, options?: { count: number }) => string;

/** i18next's `t` as two plain call shapes. */
export interface RawTranslate {
  (key: string): string;
  (key: string, options: { count: number }): string;
}

/**
 * `t` or `i18n.t` as a `Translate`. Under `exactOptionalPropertyTypes`, i18next's `t` does not take an
 * explicit `undefined` second argument, so the two call shapes are kept apart here.
 */
export function bindTranslate(raw: RawTranslate): Translate {
  return (key, options) => (options === undefined ? raw(key) : raw(key, options));
}

/** "Try again in N seconds/minutes." in the current language, or "Try again later." (R22.8). */
export function waitTimeText(t: Translate, wait: WaitTime): string {
  switch (wait.unit) {
    case 'seconds':
      return t('portal.waitTime.seconds', { count: wait.value });
    case 'minutes':
      return t('portal.waitTime.minutes', { count: wait.value });
    case 'none':
      return t('portal.waitTime.none');
  }
}

/**
 * The visible line of an error: the "Error:" prefix (so colour is not the only cue, R23.5), the message,
 * then the wait time when there is one.
 */
export function errorLine(t: Translate, key: string, wait: WaitTime | null): string {
  const parts = [t('a11y.errorPrefix'), t(key)];
  if (wait !== null) parts.push(waitTimeText(t, wait));
  return parts.join(' ');
}

/**
 * The text written to the alert live region: the form message, or else the field messages in key order,
 * then the wait time. Empty when there is nothing to say.
 */
export function announcementText(feedback: AuthFeedback, t: Translate): string {
  const keys = feedback.formMessage !== null ? [feedback.formMessage] : Object.values(feedback.fieldErrors);
  const parts = keys.map((key) => t(key));
  if (feedback.waitTime !== null) parts.push(waitTimeText(t, feedback.waitTime));
  return parts.join(' ');
}
