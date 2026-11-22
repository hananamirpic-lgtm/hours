/** A stub that task 24.6 replaces with the real page. */
import { useTranslation } from 'react-i18next';
import { SERVICE_NAMESPACE } from '../namespace';

export function ReportsPage() {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  return (
    <section className="page-body">
      <h1>{t('management.reports.title')}</h1>
    </section>
  );
}