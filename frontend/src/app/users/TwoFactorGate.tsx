/**
 * The 2FA obligation, surfaced in the console (Requirement 1.6).
 *
 * Two cases, told apart by the flags `GET /auth/me` returns:
 *
 * - **Required** (`is_2fa_enrolment_required`, an administrator who has not enrolled). The server
 *   answers 403 on every endpoint outside `/auth` until they enrol, so there is nothing useful the
 *   console can show behind this. The gate replaces the console with the enrolment flow — the one
 *   thing they can do — rather than letting them meet a wall of failed requests.
 *
 * - **Prompted** (`is_2fa_enrolment_prompted` without required, a site manager or accounting user).
 *   Enrolment is offered but not forced, so this is a dismissible banner above the console. Declining
 *   is allowed for them; the prompt returns next session.
 *
 * When neither flag is set — an enrolled user, or the employee role — the gate is transparent and
 * simply renders its children.
 */

import { useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import { useAuth } from '@/auth/AuthProvider';
import { Drawer } from '@/components/management/Drawer';
import { TwoFactorEnrolment } from '@/app/users/TwoFactorEnrolment';

export function TwoFactorGate({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const { user } = useAuth();
  const [dismissed, setDismissed] = useState(false);
  const [enrolling, setEnrolling] = useState(false);

  if (!user) {
    return <>{children}</>;
  }

  // Mandatory and outstanding: enrolment is the only thing that works, so show only that.
  if (user.is_2fa_enrolment_required) {
    return (
      <main className="auth">
        <div className="auth__card card">
          <header className="auth__header">
            <h1>{t('twoFactor.requiredTitle')}</h1>
          </header>
          <p className="subtitle">{t('twoFactor.requiredPrompt')}</p>
          <TwoFactorEnrolment />
        </div>
      </main>
    );
  }

  // Prompted but optional: a dismissible banner above the console.
  const showPrompt = user.is_2fa_enrolment_prompted && !dismissed;

  return (
    <>
      {showPrompt ? (
        <div className="banner banner--info" role="status">
          <span>{t('twoFactor.promptBanner')}</span>
          <span className="banner__actions">
            <button type="button" className="button button--small button--primary" onClick={() => setEnrolling(true)}>
              {t('twoFactor.enrolNow')}
            </button>
            <button type="button" className="button button--small" onClick={() => setDismissed(true)}>
              {t('twoFactor.later')}
            </button>
          </span>
        </div>
      ) : null}

      {children}

      {enrolling ? (
        <Drawer title={t('twoFactor.title')} onClose={() => setEnrolling(false)}>
          <TwoFactorEnrolment onDone={() => setEnrolling(false)} />
        </Drawer>
      ) : null}
    </>
  );
}
