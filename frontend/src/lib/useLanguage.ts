/**
 * The active language as the `Language` union the formatters take.
 *
 * `i18n.language` is a plain string that could in principle be anything; this narrows it to `he` or
 * `en`, falling back to the default, so a screen can pass it straight to `formatCurrency` and friends
 * without each one re-checking.
 */

import { useTranslation } from 'react-i18next';

import { DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, type Language } from '@/i18n';

export const useLanguage = (): Language => {
  const { i18n } = useTranslation();
  const current = i18n.language;
  return (SUPPORTED_LANGUAGES as readonly string[]).includes(current)
    ? (current as Language)
    : DEFAULT_LANGUAGE;
};
