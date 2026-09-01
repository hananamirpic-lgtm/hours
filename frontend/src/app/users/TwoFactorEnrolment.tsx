/**
 * Two-factor enrolment (Requirement 1.6).
 *
 * A two-step flow, matching the server: `POST /auth/2fa/setup` issues a secret and its provisioning
 * URI without enabling anything, and `POST /auth/2fa/verify` proves a code and switches 2FA on. So an
 * abandoned enrolment costs nothing — the user's existing sign-in is untouched until a code succeeds.
 *
 * The secret is a credential, so it is shown for the user to enter into their authenticator and is
 * never persisted or sent anywhere but back to our own verify endpoint. There is no QR image: the app
 * ships no QR-*generation* library (only the scanner), and rendering the secret through a third-party
 * QR service would hand the credential to that service. Manual entry of the base32 secret and the
 * `otpauth://` URI is the safe path, and every authenticator app supports it.
 *
 * On success the caller's `/auth/me` is refetched, which is what drops the enrolment prompt and, for
 * an administrator, unblocks the rest of the API (Requirement 1.6).
 */

import { useMutation } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

import { setupTwoFactor, verifyTwoFactor } from '@/api/auth';
import type { TotpSetupResponse } from '@/api/types';
import { useAuth } from '@/auth/AuthProvider';
import { toError } from '@/lib/apiError';

export function TwoFactorEnrolment({ onDone }: { onDone?: () => void }) {
  const { t } = useTranslation();
  const { refetchUser } = useAuth();
  const [enrolment, setEnrolment] = useState<TotpSetupResponse | null>(null);
  const [code, setCode] = useState('');
  const [errorKey, setErrorKey] = useState<string | null>(null);

  const begin = useMutation({
    mutationFn: setupTwoFactor,
    onSuccess: (data) => {
      setEnrolment(data);
      setErrorKey(null);
    },
    onError: (error) => setErrorKey(toError(error).key),
  });

  const complete = useMutation({
    mutationFn: () => verifyTwoFactor(code),
    onSuccess: async () => {
      await refetchUser();
      onDone?.();
    },
    onError: (error) => setErrorKey(toError(error).key),
  });

  const onVerify = (event: FormEvent) => {
    event.preventDefault();
    setErrorKey(null);
    complete.mutate();
  };

  if (enrolment === null) {
    return (
      <div className="two-factor">
        <p className="subtitle">{t('twoFactor.intro')}</p>
        {errorKey ? (
          <p className="feedback feedback--error" role="alert">
            {t(errorKey)}
          </p>
        ) : null}
        <button
          type="button"
          className="button button--primary"
          disabled={begin.isPending}
          onClick={() => begin.mutate()}
        >
          {begin.isPending ? t('common.loading') : t('twoFactor.begin')}
        </button>
      </div>
    );
  }

  return (
    <form className="two-factor form" onSubmit={onVerify}>
      <p className="subtitle">{t('twoFactor.scanInstruction')}</p>

      <div className="field">
        <span className="field__label">{t('twoFactor.secret')}</span>
        <code className="two-factor__secret">{enrolment.secret}</code>
      </div>

      <details className="two-factor__uri">
        <summary>{t('twoFactor.showUri')}</summary>
        <code className="two-factor__uri-value">{enrolment.provisioning_uri}</code>
      </details>

      <label className="field">
        <span className="field__label">{t('twoFactor.code')}</span>
        <input
          className="input numeric"
          type="text"
          inputMode="numeric"
          autoComplete="one-time-code"
          pattern="[0-9]{6}"
          maxLength={6}
          value={code}
          onChange={(event) => setCode(event.target.value.replace(/\D/g, ''))}
          required
          autoFocus
        />
      </label>

      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey)}
        </p>
      ) : null}

      <div className="form__actions">
        <button
          type="submit"
          className="button button--primary"
          disabled={complete.isPending || code.length !== 6}
        >
          {complete.isPending ? t('common.saving') : t('twoFactor.verify')}
        </button>
      </div>
    </form>
  );
}
