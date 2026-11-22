/** A stub that task 24.2 replaces with the real page. */
import { useTranslation } from 'react-i18next';
import { SERVICE_NAMESPACE } from '../namespace';

export function DashboardPage() {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  return (
    <section className="page-body">
      <h1>{t('management.dashboard.title')}</h1>
    </section>
  );
}