/** A stub that task 24.4 replaces with the real page. */
import { useTranslation } from 'react-i18next';
import { useParams } from 'react-router-dom';
import { SERVICE_NAMESPACE } from '../namespace';
export function TicketDetailPage() {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const { ticketNumber } = useParams();
  return (
    <section className="page-body">
      <h1>{t('management.ticket.title', { number: ticketNumber })}</h1>
    </section>
  );
}