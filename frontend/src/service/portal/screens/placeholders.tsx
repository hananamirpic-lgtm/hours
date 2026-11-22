// Placeholder screens (task 22.2). Each renders only its heading, taken from the `service` locale, so the
// route table, the session gate and the shell can be built and tested before the real screens exist.
//
// Task 23.x replaces them one at a time: build the real screen in its own file under `screens/`, then
// change the matching import in `PortalApp.tsx` and delete the placeholder here. The names below are the
// contract between this file and the route table.
import { useParams } from 'react-router-dom';
import { usePortalTranslation } from '../portalI18n';

/** Rendered by the session gate in place of any portal path while no valid token is stored (task 23.1). */
export function SignInScreen() {
  const { t } = usePortalTranslation();
  return <h1>{t('portal.signIn.title')}</h1>;
}

/** Rendered by the session gate in place of any portal path while `must_change_password` is set (task 23.1). */
export function ChangePasswordScreen() {
  const { t } = usePortalTranslation();
  return <h1>{t('portal.changePassword.title')}</h1>;
}

/** Route `/` (task 23.2). */
export function TicketListScreen() {
  const { t } = usePortalTranslation();
  return <h1>{t('portal.list.title')}</h1>;
}

/** Route `/new` (task 23.3). */
export function NewTicketScreen() {
  const { t } = usePortalTranslation();
  return <h1>{t('portal.form.title')}</h1>;
}

/** Route `/new/done` (task 23.3). */
export function ConfirmationScreen() {
  const { t } = usePortalTranslation();
  return <h1>{t('portal.confirmation.title')}</h1>;
}

/** Route `/tickets/:ticketNumber` (task 23.4). */
export function TicketDetailScreen() {
  const { t } = usePortalTranslation();
  const { ticketNumber } = useParams();
  return <h1>{t('portal.pageTitle.detail', { number: ticketNumber ?? '' })}</h1>;
}

/** Route `/accessibility`, outside the session gate (task 23.5). */
export function AccessibilityStatement() {
  const { t } = usePortalTranslation();
  return <h1>{t('a11y.statement.title')}</h1>;
}
