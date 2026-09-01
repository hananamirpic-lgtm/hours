import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

import en from './locales/en/common.json';
import he from './locales/he/common.json';

export const SUPPORTED_LANGUAGES = ['he', 'en'] as const;
export type Language = (typeof SUPPORTED_LANGUAGES)[number];

export const DEFAULT_LANGUAGE: Language = 'he';
const STORAGE_KEY = 'hours.language';

const isLanguage = (value: string | null): value is Language =>
  value !== null && (SUPPORTED_LANGUAGES as readonly string[]).includes(value);

/**
 * Language is read from local storage for now. It moves to the users table with the
 * localization task, where the preference becomes per account rather than per browser.
 */
export const readStoredLanguage = (): Language => {
  const stored = typeof localStorage === 'undefined' ? null : localStorage.getItem(STORAGE_KEY);
  return isLanguage(stored) ? stored : DEFAULT_LANGUAGE;
};

/** Keeps `lang` and `dir` on the document in step with the active language. */
export const applyDocumentLanguage = (language: Language): void => {
  document.documentElement.lang = language;
  document.documentElement.dir = language === 'he' ? 'rtl' : 'ltr';
};

export const changeLanguage = async (language: Language): Promise<void> => {
  await i18next.changeLanguage(language);
  localStorage.setItem(STORAGE_KEY, language);
  applyDocumentLanguage(language);
};

const initialLanguage = readStoredLanguage();

void i18next.use(initReactI18next).init({
  resources: {
    he: { common: he },
    en: { common: en },
  },
  lng: initialLanguage,
  fallbackLng: DEFAULT_LANGUAGE,
  defaultNS: 'common',
  ns: ['common'],
  // React already escapes rendered values.
  interpolation: { escapeValue: false },
});

applyDocumentLanguage(initialLanguage);

export default i18next;
