/**
 * What the two management guards render for each decision, without the auth provider, so a test can render
 * every branch with `renderToStaticMarkup`. `RequireTicketStaff` and `RequireConfigStaff` pick the decision
 * from the session and pass it here.
 */
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Navigate, type Location } from 'react-router-dom';
import type { UserRole } from '@/api/types';
import { homePathFor } from '@/app/navigation';
import type { ConfigStaffDecision, TicketStaffDecision } from './guards';
import { SERVICE_NAMESPACE } from './namespace';
import { NotAuthorizedPage } from './NotAuthorizedPage';

export interface TicketStaffViewProps {
  decision: TicketStaffDecision;
  /** The role of the signed-in staff user, when there is one. Chooses the not-authorized variant. */
  role: UserRole | undefined;
  /** The page the visitor asked for, handed to the sign-in so it can return there. */
  from: Location;
  children: ReactNode;
}

export function TicketStaffView({ decision, role, from, children }: TicketStaffViewProps) {
  switch (decision) {
    case 'wait':
      // No text: this is on screen for a fraction of a second while the session resolves.
      return <div className="full-page" aria-busy="true" />;
    case 'sign-in':
      return <Navigate to="/login" replace state={{ from }} />;
    case 'not-authorized':
      return role === undefined ? (
        <NotAuthorizedPage variant="customer" />
      ) : (
        <NotAuthorizedPage variant="staff" homePath={homePathFor(role)} />
      );
    case 'allow':
      return <>{children}</>;
  }
}

/**
 * The "not available" panel a technician sees on a configuration URL (R19.16). A heading and a sentence
 * only: no values read from the API, no form, no button, no link.
 */
export function ConfigUnavailable() {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  return (
    <section className="page-body" aria-labelledby="management-config-unavailable-title">
      <h1 id="management-config-unavailable-title">{t('management.configUnavailable.title')}</h1>
      <p>{t('management.configUnavailable.body')}</p>
    </section>
  );
}

export function ConfigStaffView({ decision, children }: { decision: ConfigStaffDecision; children: ReactNode }) {
  return decision === 'allow' ? <>{children}</> : <ConfigUnavailable />;
}
