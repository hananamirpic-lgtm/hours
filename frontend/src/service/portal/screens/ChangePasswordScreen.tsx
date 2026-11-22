// The forced change-password screen (R6.2, R6.8, R6.9, R6.10, R22.8, R23.1, R23.2, R23.13, R23.14).
//
// The session gate renders it in place of every gated portal path while `must_change_password` is set,
// so a bookmarked path keeps its URL and comes back after the change. Two parts:
//
//   ChangePasswordForm   pure presentation, so every error state can be rendered in a test.
//   ChangePasswordScreen the container: input state, the request, focus and the alert live region.
//
// The customer stays signed in on this screen for a wrong current password (401 `invalid_credentials`),
// a rejected new password (422) and a rate limit (429); the 401 handling of the portal client leaves the
// token alone for exactly that code. Success stores the returned token, and the session gate then shows
// the ticket list with no new sign-in.
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState, type FormEvent } from 'react';
import { portalFetch } from '../../api/portalFetch';
import { useAnnouncer } from '../Announcer';
import { customerTokenStore } from '../customerToken';
import { directionOf, resolveLanguage, usePortalTranslation } from '../portalI18n';
import { AuthField } from './AuthField';
import {
  EMPTY_FEEDBACK,
  announcementText,
  bindTranslate,
  errorLine,
  fieldErrorKey,
  withoutFieldErrors,
  type AuthFeedback,
} from './authFeedback';
import { completeAuthentication } from './authSession';
import {
  CHANGE_PASSWORD_FIELD_IDS,
  CHANGE_PASSWORD_IDS,
  EMPTY_CHANGE_PASSWORD_VALUES,
  buildChangePasswordBody,
  mapChangePasswordError,
  type ChangePasswordBody,
  type ChangePasswordField,
  type ChangePasswordValues,
} from './changePasswordModel';
import { useAuthFocus } from './useAuthFocus';

export interface ChangePasswordFormProps {
  values: ChangePasswordValues;
  feedback: AuthFeedback;
  /** True while the request is in flight: the submit button is disabled. */
  pending: boolean;
  onChange: (field: ChangePasswordField, value: string) => void;
  onSubmit: () => void;
}

export function ChangePasswordForm({ values, feedback, pending, onChange, onSubmit }: ChangePasswordFormProps) {
  const { t, i18n } = usePortalTranslation();
  const language = resolveLanguage(i18n.language);
  const translate = bindTranslate(t);
  const requiredText = ` ${t('a11y.required')}`;

  const currentKey = fieldErrorKey(feedback, 'current_password');
  const newKey = fieldErrorKey(feedback, 'new_password');
  const formError = feedback.formMessage === null ? null : errorLine(translate, feedback.formMessage, null);
  const formErrorId = formError === null ? null : CHANGE_PASSWORD_IDS.formError;

  const handleSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (!pending) onSubmit();
  };

  return (
    <section lang={language} dir={directionOf(language)}>
      <h1>{t('portal.changePassword.title')}</h1>
      <p>{t('portal.changePassword.intro')}</p>
      <form noValidate aria-busy={pending} onSubmit={handleSubmit}>
        {formError !== null && (
          <p id={CHANGE_PASSWORD_IDS.formError} className="portal-notice portal-notice--error">
            {formError}
          </p>
        )}
        <AuthField
          id={CHANGE_PASSWORD_IDS.current_password}
          name="current_password"
          label={t('portal.changePassword.currentPassword')}
          requiredText={requiredText}
          hint={t('portal.changePassword.passwordHint')}
          hintId={CHANGE_PASSWORD_IDS.currentHint}
          errorId={CHANGE_PASSWORD_IDS.currentError}
          error={currentKey === null ? null : errorLine(translate, currentKey, feedback.waitTime)}
          formErrorId={formErrorId}
          type="password"
          autoComplete="current-password"
          maxLength={4}
          value={values.current_password}
          onChange={(value) => onChange('current_password', value)}
        />
        <AuthField
          id={CHANGE_PASSWORD_IDS.new_password}
          name="new_password"
          label={t('portal.changePassword.newPassword')}
          requiredText={requiredText}
          hint={t('portal.changePassword.passwordHint')}
          hintId={CHANGE_PASSWORD_IDS.newHint}
          errorId={CHANGE_PASSWORD_IDS.newError}
          error={newKey === null ? null : errorLine(translate, newKey, null)}
          formErrorId={formErrorId}
          type="password"
          autoComplete="new-password"
          maxLength={4}
          value={values.new_password}
          onChange={(value) => onChange('new_password', value)}
        />
        <button id={CHANGE_PASSWORD_IDS.submit} type="submit" className="portal-button" disabled={pending}>
          {pending ? t('portal.changePassword.submitting') : t('portal.changePassword.submit')}
        </button>
      </form>
    </section>
  );
}

export function ChangePasswordScreen() {
  const { i18n } = usePortalTranslation();
  const queryClient = useQueryClient();
  const { alert } = useAnnouncer();
  const [values, setValues] = useState<ChangePasswordValues>(EMPTY_CHANGE_PASSWORD_VALUES);
  const [edited, setEdited] = useState<readonly string[]>([]);

  const mutation = useMutation({
    mutationFn: async (body: ChangePasswordBody): Promise<void> => {
      // The stored token is sent. A 401 other than `invalid_credentials` drops it (R8.11); that one keeps it.
      const response = await portalFetch<unknown>('/auth/change-password', { method: 'POST', json: body });
      // A success status that is not a usable token response: the password may have changed, so the old
      // token is dead. The generic error is all the customer can act on; signing in again fixes it.
      if (!completeAuthentication(response, customerTokenStore, queryClient)) throw new Error('unusable change-password response');
    },
  });

  const failure = useMemo(
    () => (mutation.isError ? mapChangePasswordError(mutation.error) : null),
    [mutation.isError, mutation.error],
  );
  const feedback = failure === null ? EMPTY_FEEDBACK : withoutFieldErrors(failure, edited);
  const { noteSubmit } = useAuthFocus(CHANGE_PASSWORD_FIELD_IDS, CHANGE_PASSWORD_IDS.submit, failure);

  const submit = (): void => {
    if (mutation.isPending) return;
    noteSubmit();
    setEdited([]);
    mutation.mutate(buildChangePasswordBody(values), {
      onError: (error) => {
        const result = mapChangePasswordError(error);
        // `i18n.t` reads the language at this moment, so a switch during the request is respected.
        if (result.announce) alert(announcementText(result, bindTranslate(i18n.t.bind(i18n))));
      },
    });
  };

  return (
    <ChangePasswordForm
      values={values}
      feedback={feedback}
      pending={mutation.isPending}
      onChange={(field, value) => {
        setValues((previous) => ({ ...previous, [field]: value }));
        setEdited((previous) => (previous.includes(field) ? previous : [...previous, field]));
      }}
      onSubmit={submit}
    />
  );
}
