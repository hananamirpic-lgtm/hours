/**
 * Hard-coded bilingual strings for the flag gate.
 *
 * The gate wraps every sign-in screen and runs before any language is chosen, and it must not
 * depend on the staff i18n instance or on the `service` locale namespace. So each message is a
 * Hebrew and English pair that is always shown together (Requirement 21.5: the same text in both
 * languages).
 */

export interface BilingualText {
  he: string;
  en: string;
}

/** Shown between the Hebrew and the English part of one message. */
export const BILINGUAL_SEPARATOR = '/';

/**
 * The not-available page. Requirements.md and design.md fix no exact wording (R21.5 only requires
 * "the same text in Hebrew and in English"), so this is the wording chosen for task 21.2:
 * 'שירות הקריאות אינו זמין כרגע. / Service tickets are not available right now.'
 */
export const NOT_AVAILABLE_TEXT: BilingualText = {
  he: 'שירות הקריאות אינו זמין כרגע.',
  en: 'Service tickets are not available right now.',
};

/** Shown when the flag could not be read (network error, timeout, server error). Not "off". */
export const RETRY_TEXT: BilingualText = {
  he: 'לא ניתן לבדוק כרגע אם שירות הקריאות זמין.',
  en: 'We could not check whether service tickets are available.',
};

export const RETRY_BUTTON_TEXT: BilingualText = {
  he: 'נסו שוב',
  en: 'Try again',
};

export const LOADING_TEXT: BilingualText = {
  he: 'טוען…',
  en: 'Loading…',
};

/**
 * The technician notice (Requirement 21.7) reuses NOT_AVAILABLE_TEXT as its message. Its one control
 * is the sign-out button. The same Hebrew and English words as the staff catalogue's `auth.signOut`,
 * written here because the staff i18n instance is not used by these gates.
 */
export const SIGN_OUT_TEXT: BilingualText = {
  he: 'התנתקות',
  en: 'Sign out',
};

/** The heading of the temporary `/tickets` placeholder that the management interface replaces. */
export const TICKETS_HEADING_TEXT: BilingualText = {
  he: 'קריאות שירות',
  en: 'Service tickets',
};
