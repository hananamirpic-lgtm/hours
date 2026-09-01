/**
 * A stand-in for a console screen that a later milestone fills in. It exists so the route tree and
 * navigation are real and walkable now, and so every route a role can see resolves to something
 * rather than a blank. Each page names itself through a translation key.
 */

import { useTranslation } from 'react-i18next';

export function PlaceholderPage({ titleKey }: { titleKey: string }) {
  const { t } = useTranslation();
  return (
    <section className="page-body">
      <h1>{t(titleKey)}</h1>
      <p className="subtitle">{t('common.comingSoon')}</p>
    </section>
  );
}
