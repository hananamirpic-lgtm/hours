// The sign-in screen (R5.10, R5.12, R8.2, R8.3, R8.8, R22.8, R23.1, R23.2, R23.9, R23.13, R23.14).
//
// The session gate renders it in place of any gated portal path while there is no valid token, so the URL
// is not changed. Two parts:
//
//   SignInForm    pure presentation. Props in, markup out, so every error state can be rendered in a test.
//   SignInScreen  the container: input state, the sign-in request, focus and the alert live region.
//
// Failures. One text (`portal.signIn.error`) covers an unknown phone, an inactive customer and a wrong or
// malformed password, so the screen does not reveal which one it was. A 429 adds the wait time. A message
// that belongs to no single field is written to the alert live region and focus stays where it is; a
// failing field takes focus and its message is read as that field's description.
// Typed values survive every failure. The submit button is disabled only while the request is pending.
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState, type FormEvent } from 'react';
import { portalFetch } from '../../api/portalFetch';
import { useAnnouncer } from '../Announcer';
import { customerTokenStore, useSessionExpired } from '../customerToken';
import { PageHeading } from '../PageHeading';
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
  EMPTY_SIGN_IN_VALUES,
  SIGN_IN_FIELD_IDS,
  SIGN_IN_IDS,
  buildSignInBody,
  mapSignInError,
  type SignInBody,
  type SignInField,
  type SignInValues,
} from './signInModel';
import { useAuthFocus } from './useAuthFocus';

export interface SignInFormProps {
  values: SignInValues;
  feedback: AuthFeedback;
  /** True while the request is in flight: the submit button is disabled. */
  pending: boolean;
  /** True when a 401 or the end of the 8 hours sent the customer here (R8.11). */
  sessionExpired: boolean;
  onChange: (field: SignInField, value: string) => void;
  onSubmit: () => void;
}

export function SignInForm({ values, feedback, pending, sessionExpired, onChange, onSubmit }: SignInFormProps) {
  const { t, i18n } = usePortalTranslation();
  const language = resolveLanguage(i18n.language);
  const translate = bindTranslate(t);
  const requiredText = ` ${t('a11y.required')}`;

  const phoneKey = fieldErrorKey(feedback, 'phone');
  const formError = feedback.formMessage === null ? null : errorLine(translate, feedback.formMessage, feedback.waitTime);

  const handleSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (!pending) onSubmit();
  };

  return (
    <section lang={language} dir={directionOf(language)}>
      <PageHeading pageKey="signIn">{t('portal.signIn.title')}</PageHeading>
      <p>{t('portal.signIn.intro')}</p>
      {sessionExpired && (
        <p id={SIGN_IN_IDS.sessionExpired} className="portal-notice portal-notice--warning">
          {t('portal.signIn.sessionExpired')}
        </p>
      )}
      <form noValidate aria-busy={pending} onSubmit={handleSubmit}>
        {formError !== null && (
          <p id={SIGN_IN_IDS.formError} className="portal-notice portal-notice--error">
            {formError}
          </p>
        )}
        <AuthField
          id={SIGN_IN_IDS.phone}
          name="phone"
          label={t('portal.signIn.phone')}
          requiredText={requiredText}
          hint={t('portal.signIn.phoneHint')}
          hintId={SIGN_IN_IDS.phoneHint}
          errorId={SIGN_IN_IDS.phoneError}
          error={phoneKey === null ? null : errorLine(translate, phoneKey, null)}
          formErrorId={formError === null ? null : SIGN_IN_IDS.formError}
          type="tel"
          autoComplete="username"
          dir="ltr"
          value={values.phone}
          onChange={(value) => onChange('phone', value)}
        />
        <AuthField
          id={SIGN_IN_IDS.password}
          name="password"
          label={t('portal.signIn.password')}
          requiredText={requiredText}
          hint={t('portal.signIn.passwordHint')}
          hintId={SIGN_IN_IDS.passwordHint}
          errorId={`${SIGN_IN_IDS.password}-error`}
          error={null}
          formErrorId={formError === null ? null : SIGN_IN_IDS.formError}
          type="password"
          autoComplete="current-password"
          maxLength={4}
          value={values.password}
          onChange={(value) => onChange('password', value)}
        />
        <button id={SIGN_IN_IDS.submit} type="submit" className="portal-button" disabled={pending}>
          {pending ? t('portal.signIn.submitting') : t('portal.signIn.submit')}
        </button>
      </form>
    </section>
  );
}

export function SignInScreen() {
  const { i18n } = usePortalTranslation();
  const queryClient = useQueryClient();
  const { alert } = useAnnouncer();
  const sessionExpired = useSessionExpired();
  const [values, setValues] = useState<SignInValues>(EMPTY_SIGN_IN_VALUES);
  const [edited, setEdited] = useState<readonly string[]>([]);

  const mutation = useMutation({
    mutationFn: async (body: SignInBody): Promise<void> => {
      const response = await portalFetch<unknown>('/auth/sign-in', { method: 'POST', json: body, anonymous: true });
      // A success status that is not a usable token response is a failure the customer can only retry.
      if (!completeAuthentication(response, customerTokenStore, queryClient)) throw new Error('unusable sign-in response');
    },
  });

  // The failure as the API returned it. A new object per failure, so the focus effect runs once for each.
  const failure = useMemo(
    () => (mutation.isError ? mapSignInError(mutation.error) : null),
    [mutation.isError, mutation.error],
  );
  const feedback = failure === null ? EMPTY_FEEDBACK : withoutFieldErrors(failure, edited);
  const { noteSubmit } = useAuthFocus(SIGN_IN_FIELD_IDS, SIGN_IN_IDS.submit, failure);

  const submit = (): void => {
    if (mutation.isPending) return;
    noteSubmit();
    setEdited([]);
    mutation.mutate(buildSignInBody(values), {
      onError: (error) => {
        const result = mapSignInError(error);
        // `i18n.t` reads the language at this moment, so a switch during the request is respected.
        if (result.announce) alert(announcementText(result, bindTranslate(i18n.t.bind(i18n))));
      },
    });
  };

  return (
    <SignInForm
      values={values}
      feedback={feedback}
      pending={mutation.isPending}
      sessionExpired={sessionExpired}
      onChange={(field, value) => {
        setValues((previous) => ({ ...previous, [field]: value }));
        setEdited((previous) => (previous.includes(field) ? previous : [...previous, field]));
      }}
      onSubmit={submit}
    />
  );
}
