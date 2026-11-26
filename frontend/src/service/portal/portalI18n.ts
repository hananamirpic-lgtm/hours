// The portal's own i18next instance (R8.5, R8.6, R8.7, R8.13, R23.9; design "Language, direction, display").
//
// Isolation from the staff app: this module never imports `@/i18n` (which initialises the global
// i18next, reads `hours.language` and writes `<html lang dir>` at import). It creates its own instance
// with `i18next.createInstance()`, one namespace (`service`), and its own storage key. Importing this
// module has no side effect; `initPortalI18n()` does the work and `mountPortal` calls it once.
import i18next, { type i18n as I18nInstance, type InitOptions } from 'i18next';
import { useTranslation } from 'react-i18next';
// Relative on purpose: only the two locale files are shared with the staff tree, never `@/i18n`.
import en from '../../i18n/locales/en/service.json';
import he from '../../i18n/locales/he/service.json';
import { browserStorage, type StorageLike } from './storage';

export const PORTAL_LANGUAGES = ['he', 'en'] as const;
export type PortalLanguage = (typeof PORTAL_LANGUAGES)[number];
export const DEFAULT_PORTAL_LANGUAGE: PortalLanguage = 'he';
export const PORTAL_NAMESPACE = 'service';
/** localStorage key of the language choice. The staff app uses `hours.language`. */
export const PORTAL_LANGUAGE_KEY = 'hours.service.language';

/**
 * The language to show for a stored value: `en` only for exactly the string `en`. Empty, missing, any
 * other string (`fr`, `EN`, ` en`), or a non-string gives Hebrew (R8.5). `he` gives Hebrew.
 */
export function resolveLanguage(stored: unknown): PortalLanguage {
  return stored === 'en' ? 'en' : DEFAULT_PORTAL_LANGUAGE;
}

/** `rtl` for Hebrew, `ltr` for English (R8.7, R8.13). */
export function directionOf(language: PortalLanguage): 'rtl' | 'ltr' {
  return language === 'he' ? 'rtl' : 'ltr';
}

/** The other language, which is what the language switch offers. */
export function otherLanguage(language: PortalLanguage): PortalLanguage {
  return language === 'he' ? 'en' : 'he';
}

/** Reads the stored choice. Any failure (no storage, a throwing storage) gives Hebrew. */
export function readStoredLanguage(storage: StorageLike | null = browserStorage()): PortalLanguage {
  try {
    return resolveLanguage(storage === null ? null : storage.getItem(PORTAL_LANGUAGE_KEY));
  } catch {
    return DEFAULT_PORTAL_LANGUAGE;
  }
}

/** Stores the choice. Returns false when it could not be stored; never throws and shows no error (R8.6). */
export function writeStoredLanguage(
  language: PortalLanguage,
  storage: StorageLike | null = browserStorage(),
): boolean {
  try {
    if (storage === null) return false;
    storage.setItem(PORTAL_LANGUAGE_KEY, language);
    return true;
  } catch {
    return false;
  }
}

/** Sets `<html lang>` and `<html dir>`. A no-op where there is no document. */
export function applyDocumentLanguage(
  language: PortalLanguage,
  doc: Pick<Document, 'documentElement'> | undefined = typeof document === 'undefined' ? undefined : document,
): void {
  if (doc === undefined) return;
  doc.documentElement.lang = language;
  doc.documentElement.dir = directionOf(language);
}

function initOptions(language: PortalLanguage): InitOptions {
  return {
    resources: { he: { [PORTAL_NAMESPACE]: he }, en: { [PORTAL_NAMESPACE]: en } },
    lng: language,
    fallbackLng: DEFAULT_PORTAL_LANGUAGE,
    supportedLngs: [...PORTAL_LANGUAGES],
    defaultNS: PORTAL_NAMESPACE,
    ns: [PORTAL_NAMESPACE],
    // React already escapes rendered values.
    interpolation: { escapeValue: false },
    // The resources are inline, so initialisation completes synchronously.
    initAsync: false,
    react: { useSuspense: false },
  };
}

/**
 * A new, fully initialised instance in `language`. No document or storage side effect, so a test or a
 * server render can build one. Initialisation is synchronous because the resources are inline.
 */
export function createPortalI18n(language: PortalLanguage): I18nInstance {
  const instance = i18next.createInstance();
  void instance.init(initOptions(language));
  return instance;
}

/** The instance the mounted portal uses. Created un-initialised; `initPortalI18n` fills it in. */
export const portalI18n: I18nInstance = i18next.createInstance();

let initialised = false;

/**
 * Initialises `portalI18n` from the stored choice (Hebrew when there is none or it is unusable), sets
 * `<html lang dir>` and keeps them in step on every later change. Safe to call twice.
 */
export function initPortalI18n(storage: StorageLike | null = browserStorage()): I18nInstance {
  if (initialised) return portalI18n;
  initialised = true;
  const language = readStoredLanguage(storage);
  void portalI18n.init(initOptions(language));
  applyDocumentLanguage(language);
  portalI18n.on('languageChanged', (next: string) => {
    applyDocumentLanguage(resolveLanguage(next));
  });
  return portalI18n;
}

/** The language currently shown. */
export function getLanguage(instance: I18nInstance = portalI18n): PortalLanguage {
  return resolveLanguage(instance.language);
}

/**
 * Switches the language without a reload (R8.6). The change applies to the current page view first;
 * storing the choice afterwards may fail silently. Typed form values live in component state and are
 * untouched. `<html lang dir>` follows through the `languageChanged` listener set by `initPortalI18n`.
 */
export async function setLanguage(
  language: PortalLanguage,
  instance: I18nInstance = portalI18n,
  storage: StorageLike | null = browserStorage(),
): Promise<PortalLanguage> {
  const next = resolveLanguage(language);
  await instance.changeLanguage(next);
  writeStoredLanguage(next, storage);
  return next;
}

/** `useTranslation` for the `service` namespace. Bound to the instance given to `I18nextProvider`. */
export function usePortalTranslation() {
  return useTranslation(PORTAL_NAMESPACE);
}
