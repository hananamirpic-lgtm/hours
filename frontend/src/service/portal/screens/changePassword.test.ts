import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { describe, expect, it } from 'vitest';
import { PortalApiError, handleUnauthorized } from '@/service/api/portalFetch';
import { PortalAnnouncerProvider } from '@/service/portal/Announcer';
import { CUSTOMER_TOKEN_KEY, createTokenStore } from '@/service/portal/customerToken';
import { createPortalI18n, type PortalLanguage } from '@/service/portal/portalI18n';
import { portalKeys, sessionView, type CustomerProfile } from '@/service/portal/session';
import type { StorageLike } from '@/service/portal/storage';
import { EMPTY_FEEDBACK, announcementText, bindTranslate } from '@/service/portal/screens/authFeedback';
import { completeAuthentication } from '@/service/portal/screens/authSession';
import { ChangePasswordForm, ChangePasswordScreen } from '@/service/portal/screens/ChangePasswordScreen';
import {
  CHANGE_PASSWORD_FIELDS,
  CHANGE_PASSWORD_FIELD_IDS,
  CHANGE_PASSWORD_IDS,
  EMPTY_CHANGE_PASSWORD_VALUES,
  buildChangePasswordBody,
  mapChangePasswordError,
} from '@/service/portal/screens/changePasswordModel';

const LANGUAGES: readonly PortalLanguage[] = ['he', 'en'];

function api(status: number, code: string, retryAfter: string | null = null): PortalApiError {
  return new PortalApiError(status, code, {}, retryAfter);
}

function escapeHtml(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#x27;');
}

function text(language: PortalLanguage, key: string, count?: number): string {
  const i18n = createPortalI18n(language);
  return escapeHtml(count === undefined ? i18n.t(key) : i18n.t(key, { count }));
}

function render(language: PortalLanguage, element: ReturnType<typeof createElement>): string {
  // The page heading reads the location, so every render sits in a router.
  return renderToStaticMarkup(
    createElement(I18nextProvider, { i18n: createPortalI18n(language) }, createElement(StaticRouter, { location: '/' }, element)),
  );
}

function inputTag(markup: string, id: string): string {
  const match = new RegExp(`<input[^>]*\\sid="${id}"[^>]*>`).exec(markup);
  if (match === null) throw new Error(`no input with id ${id}`);
  return match[0];
}

function memoryStorage(): StorageLike & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => {
      data.set(key, value);
    },
    removeItem: (key) => {
      data.delete(key);
    },
  };
}

const formProps = {
  values: EMPTY_CHANGE_PASSWORD_VALUES,
  feedback: EMPTY_FEEDBACK,
  pending: false,
  onChange: () => undefined,
  onSubmit: () => undefined,
};

// --- request body ----------------------------------------------------------------------------------

describe('change-password request body', () => {
  it('sends the current and the new password and nothing else, exactly as typed', () => {
    expect(buildChangePasswordBody({ current_password: '1234', new_password: '5678' })).toEqual({
      current_password: '1234',
      new_password: '5678',
    });
    expect(buildChangePasswordBody({ current_password: ' 1 ', new_password: '' })).toEqual({ current_password: ' 1 ', new_password: '' });
  });
});

// --- error mapping ---------------------------------------------------------------------------------

describe('mapChangePasswordError', () => {
  it('puts a wrong current password beside the current-password field, focus on it, nothing announced', () => {
    expect(mapChangePasswordError(api(401, 'invalid_credentials'))).toEqual({
      fieldErrors: { current_password: 'portal.changePassword.currentPasswordWrong' },
      formMessage: null,
      waitTime: null,
      focus: 'current_password',
      announce: false,
    });
  });

  it.each(['invalid_password_format', 'password_not_changed'])(
    'puts a 422 %s beside the new-password field and moves focus there',
    (code) => {
      expect(mapChangePasswordError(api(422, code))).toEqual({
        fieldErrors: { new_password: `errors.${code}` },
        formMessage: null,
        waitTime: null,
        focus: 'new_password',
        announce: false,
      });
    },
  );

  it.each([
    ['too_many_attempts', '5', { unit: 'seconds', value: 5 }],
    ['too_many_attempts', '60', { unit: 'seconds', value: 60 }],
    ['too_many_attempts', '61', { unit: 'minutes', value: 2 }],
    ['too_many_attempts', null, { unit: 'none', value: 0 }],
    ['rate_limited', '5', { unit: 'seconds', value: 5 }],
    ['rate_limited', '61', { unit: 'minutes', value: 2 }],
    ['rate_limited', null, { unit: 'none', value: 0 }],
  ])('429 %s with Retry-After %s: message and wait time beside the current-password field, announced, focus kept', (code, retryAfter, wait) => {
    expect(mapChangePasswordError(api(429, code, retryAfter))).toEqual({
      fieldErrors: { current_password: `errors.${code}` },
      formMessage: null,
      waitTime: wait,
      focus: null,
      announce: true,
    });
  });

  it.each([
    ['a network failure', new PortalApiError(0, 'network'), 'errors.generic'],
    ['a timeout', new PortalApiError(0, 'timeout'), 'errors.generic'],
    ['a 500 with an unreadable body', api(500, 'generic'), 'errors.generic'],
    ['a 503 service_unavailable', api(503, 'service_unavailable'), 'errors.service_unavailable'],
    ['a 422 with another code', api(422, 'invalid_request'), 'errors.generic'],
    ['a stale 401 invalid_token', api(401, 'invalid_token'), 'errors.invalid_token'],
    ['a value that is not an API error', new Error('boom'), 'errors.generic'],
  ])('%s shows a form message, announced, with no field marked and no focus move', (_name, error, key) => {
    expect(mapChangePasswordError(error)).toEqual({
      fieldErrors: {},
      formMessage: key,
      waitTime: null,
      focus: null,
      announce: true,
    });
  });

  it('keeps the customer signed in for exactly the cases R6.9 names, and drops the token for another 401', () => {
    // The portal client applies this decision before the screen sees the error.
    expect(handleUnauthorized('invalid_credentials', { endpoint: 'change-password' })).toEqual({ clearToken: false, showSessionExpired: false });
    expect(handleUnauthorized('invalid_token', { endpoint: 'change-password' })).toEqual({ clearToken: true, showSessionExpired: true });
  });

  it('walks the fields in reading order: current password, then the new one', () => {
    expect(CHANGE_PASSWORD_FIELDS).toEqual(['current_password', 'new_password']);
    expect(CHANGE_PASSWORD_FIELD_IDS.current_password).toBe(CHANGE_PASSWORD_IDS.current_password);
    expect(CHANGE_PASSWORD_FIELD_IDS.new_password).toBe(CHANGE_PASSWORD_IDS.new_password);
  });

  it('uses translation keys that exist, with text, in both languages', () => {
    const feedbacks = [
      mapChangePasswordError(api(401, 'invalid_credentials')),
      mapChangePasswordError(api(422, 'invalid_password_format')),
      mapChangePasswordError(api(422, 'password_not_changed')),
      mapChangePasswordError(api(429, 'too_many_attempts', '5')),
      mapChangePasswordError(api(429, 'rate_limited', '5')),
      mapChangePasswordError(new PortalApiError(0, 'timeout')),
    ];
    const keys = feedbacks.flatMap((f) => [...Object.values(f.fieldErrors), ...(f.formMessage === null ? [] : [f.formMessage])]);
    for (const language of LANGUAGES) {
      const i18n = createPortalI18n(language);
      for (const key of keys) {
        expect(i18n.exists(key), `${language} ${key}`).toBe(true);
        expect(i18n.t(key).length, `${language} ${key}`).toBeGreaterThan(0);
      }
    }
  });

  it.each(LANGUAGES)('announces the 429 message and the wait time in %s', (language) => {
    const i18n = createPortalI18n(language);
    const t = bindTranslate((key: string, options?: { count: number }) => (options === undefined ? i18n.t(key) : i18n.t(key, options)));
    expect(announcementText(mapChangePasswordError(api(429, 'too_many_attempts', '30')), t)).toBe(
      `${i18n.t('errors.too_many_attempts')} ${i18n.t('portal.waitTime.seconds', { count: 30 })}`,
    );
  });
});

// --- success ---------------------------------------------------------------------------------------

describe('completeAuthentication (change-password success)', () => {
  const NOW = 1_800_000_000;
  const response = { access_token: 'new-token', token_type: 'bearer', expires_in: 28_800, must_change_password: false };

  it('replaces the stored token, clears the flag in the cached profile and invalidates it, so the gate shows the list', () => {
    const storage = memoryStorage();
    const tokens = createTokenStore({ storage, now: () => NOW, events: null });
    tokens.setToken('old-token', NOW + 100);
    const client = new QueryClient();
    const profile: CustomerProfile = { full_name: 'A', phone: '0501234567', city: null, apartment: null, must_change_password: true };
    client.setQueryData(portalKeys.me, profile);

    expect(sessionView({ hasToken: true, profileStatus: 'success', mustChangePassword: true })).toBe('change-password');
    expect(completeAuthentication(response, tokens, client)).toBe(true);

    expect(tokens.getToken()).toBe('new-token');
    expect(JSON.parse(storage.data.get(CUSTOMER_TOKEN_KEY) as string)).toEqual({ token: 'new-token', exp: NOW + 28_800 });
    const cached = client.getQueryData<CustomerProfile>(portalKeys.me);
    expect(cached?.must_change_password).toBe(false);
    expect(client.getQueryState(portalKeys.me)?.isInvalidated).toBe(true);
    expect(sessionView({ hasToken: true, profileStatus: 'success', mustChangePassword: cached?.must_change_password })).toBe('signed-in');
  });

  it('keeps the old token when the response is unusable', () => {
    const tokens = createTokenStore({ storage: memoryStorage(), now: () => NOW, events: null });
    tokens.setToken('old-token', NOW + 100);
    expect(completeAuthentication({ ok: true }, tokens, new QueryClient())).toBe(false);
    expect(tokens.getToken()).toBe('old-token');
  });
});

// --- markup ----------------------------------------------------------------------------------------

describe.each(LANGUAGES)('ChangePasswordForm markup in %s', (language) => {
  const dir = language === 'he' ? 'rtl' : 'ltr';
  const markup = render(language, createElement(ChangePasswordForm, formProps));

  it('follows the language: lang and dir on the screen, heading, intro and labels in that language', () => {
    expect(markup).toContain(`<section lang="${language}" dir="${dir}">`);
    expect(markup).toContain(`<h1 tabindex="-1" class="portal-page-heading">${text(language, 'portal.changePassword.title')}</h1>`);
    expect(markup).toContain(text(language, 'portal.changePassword.intro'));
    expect(markup).toContain(text(language, 'portal.changePassword.submit'));
  });

  it('has a visible label tied to each input, with the "(required)" text and no placeholder', () => {
    const required = text(language, 'a11y.required');
    for (const [id, key] of [
      [CHANGE_PASSWORD_IDS.current_password, 'portal.changePassword.currentPassword'],
      [CHANGE_PASSWORD_IDS.new_password, 'portal.changePassword.newPassword'],
    ] as const) {
      expect(markup).toContain(`<label for="${id}">${text(language, key)}<span class="portal-required"> ${required}</span></label>`);
      expect(inputTag(markup, id)).toContain('aria-required="true"');
    }
    expect(markup).not.toContain('placeholder');
    expect(markup.match(/<label /g)).toHaveLength(2);
  });

  it('gives both inputs a masked type, numeric input mode, four characters and a hint', () => {
    const current = inputTag(markup, CHANGE_PASSWORD_IDS.current_password);
    const next = inputTag(markup, CHANGE_PASSWORD_IDS.new_password);
    for (const tag of [current, next]) {
      expect(tag).toContain('type="password"');
      expect(tag).toContain('inputMode="numeric"');
      expect(tag).toContain('maxLength="4"');
    }
    expect(current).toContain('autoComplete="current-password"');
    expect(next).toContain('autoComplete="new-password"');
    expect(current).toContain(`aria-describedby="${CHANGE_PASSWORD_IDS.currentHint}"`);
    expect(next).toContain(`aria-describedby="${CHANGE_PASSWORD_IDS.newHint}"`);
    expect(markup).toContain(text(language, 'portal.changePassword.passwordHint'));
  });

  it('shows no error markup and no invalid field by default, and an enabled submit button', () => {
    expect(markup).not.toContain('aria-invalid');
    expect(markup).not.toContain('portal-error');
    expect(markup).toMatch(/<button[^>]*type="submit"(?![^>]*disabled)[^>]*>/);
  });

  it('disables the submit button and says it is working while pending', () => {
    const pending = render(language, createElement(ChangePasswordForm, { ...formProps, pending: true }));
    expect(pending).toMatch(/<button[^>]*type="submit"[^>]*disabled=""[^>]*>/);
    expect(pending).toContain(text(language, 'portal.changePassword.submitting'));
  });

  it('wires a wrong current password: aria-invalid, message first in aria-describedby, new password untouched', () => {
    const failed = render(language, createElement(ChangePasswordForm, { ...formProps, feedback: mapChangePasswordError(api(401, 'invalid_credentials')) }));
    const current = inputTag(failed, CHANGE_PASSWORD_IDS.current_password);
    expect(current).toContain('aria-invalid="true"');
    expect(current).toContain(`aria-describedby="${CHANGE_PASSWORD_IDS.currentError} ${CHANGE_PASSWORD_IDS.currentHint}"`);
    expect(failed).toContain(`<p id="${CHANGE_PASSWORD_IDS.currentError}" class="portal-error">`);
    expect(failed).toContain(text(language, 'portal.changePassword.currentPasswordWrong'));
    expect(inputTag(failed, CHANGE_PASSWORD_IDS.new_password)).not.toContain('aria-invalid');
    expect(failed).not.toContain('role=');
  });

  it.each(['invalid_password_format', 'password_not_changed'])('wires a 422 %s beside the new-password field', (code) => {
    const failed = render(language, createElement(ChangePasswordForm, { ...formProps, feedback: mapChangePasswordError(api(422, code)) }));
    const next = inputTag(failed, CHANGE_PASSWORD_IDS.new_password);
    expect(next).toContain('aria-invalid="true"');
    expect(next).toContain(`aria-describedby="${CHANGE_PASSWORD_IDS.newError} ${CHANGE_PASSWORD_IDS.newHint}"`);
    expect(failed).toContain(`<p id="${CHANGE_PASSWORD_IDS.newError}" class="portal-error">`);
    expect(failed).toContain(text(language, `errors.${code}`));
    expect(inputTag(failed, CHANGE_PASSWORD_IDS.current_password)).not.toContain('aria-invalid');
  });

  it.each([
    ['too_many_attempts', '5', 'seconds', 5],
    ['too_many_attempts', '61', 'minutes', 2],
    ['rate_limited', '60', 'seconds', 60],
  ] as const)('shows a 429 %s with Retry-After %s beside the current-password field, message then wait time', (code, retryAfter, unit, count) => {
    const failed = render(language, createElement(ChangePasswordForm, { ...formProps, feedback: mapChangePasswordError(api(429, code, retryAfter)) }));
    expect(failed).toContain(`${text(language, `errors.${code}`)} ${text(language, `portal.waitTime.${unit}`, count)}</p>`);
    expect(inputTag(failed, CHANGE_PASSWORD_IDS.current_password)).toContain(`aria-describedby="${CHANGE_PASSWORD_IDS.currentError} `);
  });

  it('shows a network failure as the generic form message, linked from both inputs', () => {
    const failed = render(language, createElement(ChangePasswordForm, { ...formProps, feedback: mapChangePasswordError(new PortalApiError(0, 'network')) }));
    expect(failed).toContain(`<p id="${CHANGE_PASSWORD_IDS.formError}" class="portal-notice portal-notice--error">`);
    expect(failed).toContain(text(language, 'errors.generic'));
    for (const id of [CHANGE_PASSWORD_IDS.current_password, CHANGE_PASSWORD_IDS.new_password]) {
      expect(inputTag(failed, id)).toContain(`aria-describedby="${CHANGE_PASSWORD_IDS.formError} `);
    }
  });
});

describe('ChangePasswordScreen container markup', () => {
  it.each(LANGUAGES)('renders the empty form inside the providers in %s', (language) => {
    const markup = render(
      language,
      createElement(
        QueryClientProvider,
        { client: new QueryClient() },
        createElement(PortalAnnouncerProvider, null, createElement(ChangePasswordScreen)),
      ),
    );
    expect(markup).toContain(`<h1 tabindex="-1" class="portal-page-heading">${text(language, 'portal.changePassword.title')}</h1>`);
    expect(inputTag(markup, CHANGE_PASSWORD_IDS.current_password)).toContain('value=""');
    expect(markup.match(/role="alert"/g)).toHaveLength(1);
  });
});

describe('change-password sources', () => {
  const source = readFileSync(fileURLToPath(new URL('./ChangePasswordScreen.tsx', import.meta.url)), 'utf8');
  const model = readFileSync(fileURLToPath(new URL('./changePasswordModel.ts', import.meta.url)), 'utf8');

  it('never logs, and never puts a password in a URL or in storage', () => {
    for (const code of [source, model]) {
      expect(code).not.toMatch(/console\./);
      expect(code).not.toMatch(/location\.(search|hash|href)|history\.(push|replace)State|URLSearchParams/);
      expect(code).not.toMatch(/localStorage|sessionStorage/);
    }
  });

  it('sends the passwords in the request body to the change-password endpoint with the stored token', () => {
    expect(source).toContain("'/auth/change-password'");
    expect(source).not.toContain('anonymous');
  });
});
