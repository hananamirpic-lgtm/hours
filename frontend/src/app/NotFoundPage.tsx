import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { useAuth } from '@/auth/AuthProvider';
import { homePathFor } from '@/app/navigation';

export function NotFoundPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const home = user ? homePathFor(user.role) : '/';

  return (
    <section className="page-body">
      <h1>{t('notFound.title')}</h1>
      <p className="subtitle">{t('notFound.body')}</p>
      <Link className="button" to={home}>
        {t('notFound.home')}
      </Link>
    </section>
  );
}
