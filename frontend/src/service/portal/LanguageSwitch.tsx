// The language switch (R8.4, R8.6, R23.9; design "Language, direction, display").
//
// One button. Its visible text and `aria-label` are the name of the language it switches TO, written in
// that language (`portal.language.switchTo`: "English" while Hebrew is shown, "עברית" while English is
// shown), and `lang` on the element names that language, so a screen reader pronounces it correctly.
//
// It is rendered at the same place with the same element type in both directions, so React updates the
// text and `lang` in place: it is not remounted and keyboard focus stays on it. Switching changes the
// language without a reload, stores the choice (silently ignoring a storage failure) and writes
// `a11y.live.languageChanged`, in the new language, into the polite live region.
import { useTranslation } from 'react-i18next';
import { useAnnouncer } from './Announcer';
import { PORTAL_NAMESPACE, otherLanguage, resolveLanguage, setLanguage } from './portalI18n';

export function LanguageSwitch() {
  const { t, i18n } = useTranslation(PORTAL_NAMESPACE);
  const { announce } = useAnnouncer();
  const target = otherLanguage(resolveLanguage(i18n.language));
  const name = t('portal.language.switchTo');

  const onClick = (): void => {
    void setLanguage(target, i18n).then(() => {
      announce(i18n.t('a11y.live.languageChanged'));
    });
  };

  return (
    <button type="button" className="portal-button portal-button--quiet" lang={target} aria-label={name} onClick={onClick}>
      {name}
    </button>
  );
}
