/**
 * The first-use password-change obligation, surfaced in either shell (Requirement 1).
 *
 * Mirrors the "required" branch of {@link TwoFactorGate}: when `GET /auth/me` reports
 * `must_change_password`, the server answers 403 on every endpoint outside `/auth`, so there is
 * nothing useful behind this gate to show. The gate replaces its children with a mandatory
 * change-password screen — the one thing the caller can do — rather than letting them meet a wall of
 * failed requests.
 *
 * Unlike the 2FA prompt there is no dismissible, optional case: the obligation is either owed or it is
 * not. A non-administrator created by an administrator owes it until they pick their own password; an
 * administrator never owes it (the server keeps `must_change_password` false for the admin role, and
 * the gate below trusts that flag). When the flag is clear the gate is transparent and renders its
 * children unchanged, so it is safe to wrap both the console and the mobile trees with it.
 *
 * On success the caller's `/auth/me` is refetched, which clears the flag and drops the gate.
 */

import { useMutation } from '@tanstack/react-query';
import { useState, type FormEvent, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import { changePassword } from '@/api/auth';
import { useAuth } from '@/auth/AuthProvider';
import { toError } from '@/lib/apiError';

const MIN_PASSWORD_LENGTH = 8;

export function ChangePasswordGate({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const { user } = useAuth();

  if (!user || !user.must_change_password) {
    return <>{children}</>;
  }

  return (
    <main className="auth">
      <div className="auth__card card">
        <header className="auth__header">
          <h1>{t('changePassword.title')}</h1>
        </header>
        <p className="subtitle">{t('changePassword.prompt')}</p>
        <ChangePasswordForm />
      </div>
    </main>
  );
}

/**
 * The change-password form. Client-side it checks the new password against the confirmation, against
 * the current password, and against the minimum length before submitting, showing a field-level hint
 * from i18n so the common mistakes never make a round trip. The server is still authoritative: its
 * `current_password_incorrect` and `new_password_must_differ` codes are rendered through
 * {@link toError} in the reader's language.
 */
function ChangePasswordForm() {
  const { t } = useTranslation();
  const { refetchUser } = useAuth();

  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [localErrorKey, setLocalErrorKey] = useState<string | null>(null);
  const [serverErrorKey, setServerErrorKey] = useState<string | null>(null);

  const submit = useMutation({
    mutationFn: () => changePassword(currentPassword, newPassword),
    onSuccess: async () => {
      await refetchUser();
    },
    onError: (error) => setServerErrorKey(toError(error).key),
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    setServerErrorKey(null);

    if (newPassword.length < MIN_PASSWORD_LENGTH) {
      setLocalErrorKey('changePassword.tooShort');
      return;
    }
    if (newPassword !== confirmPassword) {
      setLocalErrorKey('changePassword.mismatch');
      return;
    }
    if (newPassword === currentPassword) {
      setLocalErrorKey('changePassword.mustDiffer');
      return;
    }
    setLocalErrorKey(null);
    submit.mutate();
  };

  const errorKey = localErrorKey ?? serverErrorKey;

  return (
    <form className="form" onSubmit={onSubmit}>
      <label className="field">
        <span className="field__label">{t('changePassword.currentPassword')}</span>
        <input
          className="input"
          type="password"
          autoComplete="current-password"
          value={currentPassword}
          onChange={(event) => setCurrentPassword(event.target.value)}
          required
          autoFocus
        />
      </label>

      <label className="field">
        <span className="field__label">{t('changePassword.newPassword')}</span>
        <input
          className="input"
          type="password"
          autoComplete="new-password"
          value={newPassword}
          onChange={(event) => setNewPassword(event.target.value)}
          required
        />
        <span className="field__hint">{t('changePassword.tooShort')}</span>
      </label>

      <label className="field">
        <span className="field__label">{t('changePassword.confirmPassword')}</span>
        <input
          className="input"
          type="password"
          autoComplete="new-password"
          value={confirmPassword}
          onChange={(event) => setConfirmPassword(event.target.value)}
          required
        />
      </label>

      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey)}
        </p>
      ) : null}

      <div className="form__actions">
        <button type="submit" className="button button--primary" disabled={submit.isPending}>
          {submit.isPending ? t('changePassword.submitting') : t('changePassword.submit')}
        </button>
      </div>
    </form>
  );
}
