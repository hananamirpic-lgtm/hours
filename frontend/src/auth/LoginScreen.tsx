/**
 * Sign-in screen, including the second-factor step.
 *
 * The 2FA flow is driven by the server, not guessed by the client: the first submission omits the
 * code, and if the account has 2FA enabled the API answers `totp_required`. That is the signal to
 * reveal the code field and ask again. So a user without 2FA signs in in one step, and a user with it
 * sees the code field exactly when it is needed and never before.
 *
 * Every message is a translation key, and a failure is rendered from the machine `code` the API
 * returns — the backend is locale-neutral by design, so the words live here.
 */

import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

import { ApiError } from '@/api/client';
import { useAuth } from '@/auth/AuthProvider';
import { LanguageToggle } from '@/components/LanguageToggle';

const errorKey = (error: unknown): string => {
  if (error instanceof ApiError && error.code) {
    return `auth.error.${error.code}`;
  }
  return 'auth.error.unreachable';
};

export function LoginScreen() {
  const { t } = useTranslation();
  const { signIn } = useAuth();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [totpCode, setTotpCode] = useState('');
  const [totpRequired, setTotpRequired] = useState(false);
  const [errorMessageKey, setErrorMessageKey] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setErrorMessageKey(null);
    try {
      await signIn({
        username,
        password,
        ...(totpRequired && totpCode ? { totp_code: totpCode } : {}),
      });
    } catch (error) {
      if (error instanceof ApiError && error.code === 'totp_required') {
        setTotpRequired(true);
        setErrorMessageKey(null);
      } else {
        setErrorMessageKey(errorKey(error));
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="auth">
      <div className="auth__card card">
        <header className="auth__header">
          <h1>{t('app.title')}</h1>
          <LanguageToggle />
        </header>
        <p className="subtitle">{t('auth.signInPrompt')}</p>

        <form className="form" onSubmit={onSubmit}>
          <label className="field">
            <span className="field__label">{t('auth.username')}</span>
            <input
              className="input"
              type="text"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
            />
          </label>

          <label className="field">
            <span className="field__label">{t('auth.password')}</span>
            <input
              className="input"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>

          {totpRequired ? (
            <label className="field">
              <span className="field__label">{t('auth.totpCode')}</span>
              <input
                className="input numeric"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6}"
                maxLength={6}
                value={totpCode}
                onChange={(event) => setTotpCode(event.target.value.replace(/\D/g, ''))}
                required
                autoFocus
              />
            </label>
          ) : null}

          {errorMessageKey ? (
            <p className="form__error" role="alert">
              {t(errorMessageKey)}
            </p>
          ) : null}

          <button className="button button--primary" type="submit" disabled={submitting}>
            {submitting ? t('auth.signingIn') : t('auth.signIn')}
          </button>
        </form>
      </div>
    </main>
  );
}
