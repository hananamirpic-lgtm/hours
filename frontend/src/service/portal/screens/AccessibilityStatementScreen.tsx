// The accessibility statement page (R8.4, R8.10, R23.10; design "Routes" and "Accessibility measures").
//
// It sits behind `FlagGate` only, outside the session gate (see `PortalApp.tsx`), so it opens without a
// Customer_Token. It states the IS 5568 level AA target, what the portal provides, the known
// limitations, the contact for accessibility requests (from `/api/features`, through
// `useAccessibilityContact`) and the date of its last update (from the locale file). Headings: one
// `<h1>`, then one `<h2>` per section. It links to no Hours console screen.
import { useAccessibilityContact } from '../../api/features';
import { PageHeading } from '../PageHeading';
import { usePortalTranslation } from '../portalI18n';
import { isolateLtr } from '../PortalShell';
import './accessibility.css';

const FEATURE_KEYS = [
  'a11y.statement.feature1',
  'a11y.statement.feature2',
  'a11y.statement.feature3',
  'a11y.statement.feature4',
] as const;

const LIMITATION_KEYS = ['a11y.statement.limitation1', 'a11y.statement.limitation2'] as const;

export function AccessibilityStatementScreen() {
  const { t } = usePortalTranslation();
  const contact = useAccessibilityContact();

  return (
    <div className="a11y-statement">
      <PageHeading pageKey="accessibility">{t('a11y.statement.title')}</PageHeading>
      <p>{t('a11y.statement.intro')}</p>

      <section aria-labelledby="a11y-target-title">
        <h2 id="a11y-target-title">{t('a11y.statement.targetTitle')}</h2>
        <p>{t('a11y.statement.target')}</p>
      </section>

      <section aria-labelledby="a11y-features-title">
        <h2 id="a11y-features-title">{t('a11y.statement.featuresTitle')}</h2>
        <ul>
          {FEATURE_KEYS.map((key) => (
            <li key={key}>{t(key)}</li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="a11y-limitations-title">
        <h2 id="a11y-limitations-title">{t('a11y.statement.limitationsTitle')}</h2>
        <ul>
          {LIMITATION_KEYS.map((key) => (
            <li key={key}>{t(key)}</li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="a11y-contact-title">
        <h2 id="a11y-contact-title">{t('a11y.statement.contactTitle')}</h2>
        <p>
          {contact === null
            ? t('a11y.statement.contactMissing')
            : t('a11y.statement.contact', { contact: isolateLtr(contact) })}
        </p>
      </section>

      <p className="a11y-statement__updated">
        {t('a11y.statement.lastUpdated', { date: isolateLtr(t('a11y.statement.lastUpdatedDate')) })}
      </p>
    </div>
  );
}
