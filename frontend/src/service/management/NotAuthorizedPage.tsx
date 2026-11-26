/**
 * The not-authorized page of the Management_Interface (Requirement 1.13).
 *
 * Shown to a signed-in staff user whose role is not `admin`, `operations_admin` or `technician`, and to a
 * visitor who holds only a customer token. It holds a title, a sentence and one link, and nothing read
 * from the Ticket_API: no ticket data and no customer data.
 *
 * It imports no auth provider and does not load the `service` bundles itself (the guard that renders it
 * does, through `../i18n`), so a test can render it with any i18next instance.
 */
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { PORTAL_PATH } from '../portalPath';
import { SERVICE_NAMESPACE } from './namespace';

export interface NotAuthorizedPageProps {
  /**
   * `staff`: a signed-in staff user of another role, who gets a link to the home of their own role.
   * `customer`: no staff session, only a customer token, who gets a link to the customer portal.
   */
  variant: 'staff' | 'customer';
  /** The route the staff link goes to. Ignored for `customer`. */
  homePath?: string;
}

export function NotAuthorizedPage({ variant, homePath = '/' }: NotAuthorizedPageProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  return (
    <main className="auth">
      <section className="auth__card card" aria-labelledby="management-not-authorized-title">
        <h1 id="management-not-authorized-title">{t('management.notAuthorized.title')}</h1>
        <p>{t('management.notAuthorized.body')}</p>
        {variant === 'customer' ? (
          // The portal is a separate entry point of the same app, so this is a full navigation.
          <a className="button" href={PORTAL_PATH}>
            {t('management.notAuthorized.portal')}
          </a>
        ) : (
          <Link className="button" to={homePath}>
            {t('management.notAuthorized.home')}
          </Link>
        )}
      </section>
    </main>
  );
}
