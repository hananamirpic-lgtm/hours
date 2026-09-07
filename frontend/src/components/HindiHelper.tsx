/**
 * A Hindi helper line for the employee (mobile) interface.
 *
 * The employee app offers two interface languages, Hebrew and English. When the employee chooses
 * English, every English string on the mobile app shows its Hindi translation on the line beneath —
 * a reading aid for workers who read Hindi, with the interface and all input staying in English.
 * When the language is Hebrew (or anything but English) this renders nothing, so Hebrew is never
 * accompanied by Hindi. It is never used in the console.
 *
 * The Hindi text comes from the `hi` i18next bundle keyed by the *same* key as the English string, so
 * interpolation (names, sites, dates) works exactly as it does for the visible English. `textKey` may
 * be a single key or a fallback list (matching a `t([...])` call site); `params` carries the same
 * interpolation values passed to the English `t(...)`. A key with no Hindi entry renders nothing.
 *
 * The line is marked `lang="hi"` and `dir="ltr"` and bidi-isolated, so the Devanagari renders
 * correctly even inside a Hebrew (RTL) document, and it uses logical CSS only.
 */
import i18next from 'i18next';
import { useTranslation } from 'react-i18next';

export function HindiHelper({
  textKey,
  params,
}: {
  textKey: string | string[];
  params?: Record<string, unknown>;
}) {
  const { i18n } = useTranslation();
  if (i18n.language !== 'en') {
    return null;
  }
  // Only render when the Hindi bundle actually has this key, so an untranslated key (or a console
  // key an employee cannot reach) shows nothing rather than falling back to the English text and
  // duplicating the visible line. `exists` checks the `hi` bundle directly; a fallback list resolves
  // to its first present key, matching how the visible `t([...])` chose its string.
  const keys = Array.isArray(textKey) ? textKey : [textKey];
  const presentKey = keys.find((key) => i18next.exists(key, { lng: 'hi' }));
  if (!presentKey) {
    return null;
  }
  const text = i18next.t(presentKey, { ...params, lng: 'hi' });
  return (
    <span className="hindi-helper" lang="hi" dir="ltr">
      {text}
    </span>
  );
}