// The catch-all of the portal route table. It shows no ticket data, and it sits behind the session gate,
// so a signed-out visitor sees the sign-in screen at an unknown path, as at every other path (R8.2).
import { Link } from 'react-router-dom';
import { PageHeading } from '../PageHeading';
import { usePortalTranslation } from '../portalI18n';

export function NotFoundScreen() {
  const { t } = usePortalTranslation();
  return (
    <section>
      <PageHeading pageKey="notFound">{t('portal.notFound.title')}</PageHeading>
      <p>{t('portal.notFound.body')}</p>
      <p>
        <Link to="/" className="portal-button">
          {t('portal.notFound.back')}
        </Link>
      </p>
    </section>
  );
}
