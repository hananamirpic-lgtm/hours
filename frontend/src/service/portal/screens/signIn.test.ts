import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { describe, expect, it } from 'vitest';
import { PortalApiError } from '@/service/api/portalFetch';
import { PortalAnnouncerProvider } from '@/service/portal/Announcer';
import { CUSTOMER_TOKEN_KEY, createTokenStore } from '@/service/portal/customerToken';
import { createPortalI18n, type PortalLanguage } from '@/service/portal/portalI18n';
import { portalKeys, type CustomerProfile } from '@/service/portal/session';
import type { StorageLike } from '@/service/portal/storage';
import { AuthField } from '@/service/portal/screens/AuthField';
import {
  EMPTY_FEEDBACK,
  announcementText,
  bindTranslate,
  describedBy,
  errorLine,
  fieldErrorKey,
  failureOf,
  withoutFieldErrors,
} from '@/service/portal/screens/authFeedback';
import { completeAuthentication, parseAuthTokenResponse } from '@/service/portal/screens/authSession';
import { SignInForm, SignInScreen } from '@/service/portal/screens/SignInScreen';
import {
  EMPTY_SIGN_IN_VALUES,
  SIGN_IN_FIELDS,
  SIGN_IN_FIELD_IDS,
  SIGN_IN_IDS,
  buildSignInBody,
  cleanPhoneInput,
  mapSignInError,
} from '@/service/portal/screens/signInModel';
import enService from '../../../i18n/locales/en/service.json';
import heService from '../../../i18n/locales/he/service.json';

const LANGUAGES: readonly PortalLanguage[] = ['he', 'en'];

function api(status: number, code: string, retryAfter: string | null = null): PortalApiError {
  return new PortalApiError(status, code, {}, retryAfter);
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#x27;');
}

/** The text of a key in a language, escaped the way React escapes it into markup. */
function text(language: PortalLanguage, key: string, count?: number): string {
  const i18n = createPortalI18n(language);
  return escapeHtml(count === undefined ? i18n.t(key) : i18n.t(key, { count }));
}

function render(language: PortalLanguage, element: ReturnType<typeof createElement>): string {
  return renderToStaticMarkup(createElement(I18nextProvider, { i18n: createPortalI18n(language) }, element));
}

/** The `<input ...>` tag with this id. */
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
  values: EMPTY_SIGN_IN_VALUES,
  feedback: EMPTY_FEEDBACK,
  pending: false,
  sessionExpired: false,
  onChange: () => undefined,
  onSubmit: () => undefined,
};

// --- request body ----------------------------------------------------------------------------------

describe('sign-in request body', () => {
  it('sends the phone and the password and nothing else', () => {
    expect(buildSignInBody({ phone: '0501234567', password: '1234' })).toEqual({ phone: '0501234567', password: '1234' });
  });

  it('trims the phone and drops directional marks pasted from right-to-left text, but leaves its spelling alone', () => {
    expect(cleanPhoneInput('  050-123 4567 ')).toBe('050-123 4567');
    expect(cleanPhoneInput('\u200f0501234567\u200e')).toBe('0501234567');
    expect(cleanPhoneInput('\u2066+972 50-123-4567\u2069')).toBe('+972 50-123-4567');
    expect(cleanPhoneInput('   ')).toBe('');
  });

  it('sends the password exactly as typed, whitespace included, because the server judges it', () => {
    expect(buildSignInBody({ phone: '0501234567', password: ' 12 ' }).password).toBe(' 12 ');
    expect(buildSignInBody({ phone: '', password: '' })).toEqual({ phone: '', password: '' });
  });
});

// --- error mapping ---------------------------------------------------------------------------------

describe('mapSignInError', () => {
  it('shows one identical text for every 401, whatever the cause (unknown phone, inactive, wrong password)', () => {
    const results = [
      mapSignInError(api(401, 'invalid_credentials')),
      mapSignInError(api(401, 'invalid_credentials', '30')),
      mapSignInError(api(401, 'invalid_token')),
      mapSignInError(api(401, 'generic')),
    ];
    for (const result of results) {
      expect(result).toEqual({
        fieldErrors: {},
        formMessage: 'portal.signIn.error',
        waitTime: null,
        focus: null,
        announce: true,
      });
    }
  });

  it('puts a 422 invalid_phone beside the phone field and moves focus there, with nothing announced', () => {
    expect(mapSignInError(api(422, 'invalid_phone'))).toEqual({
      fieldErrors: { phone: 'errors.invalid_phone' },
      formMessage: null,
      waitTime: null,
      focus: 'phone',
      announce: false,
    });
  });

  it.each([
    ['too_many_attempts', '5', { unit: 'seconds', value: 5 }],
    ['too_many_attempts', '60', { unit: 'seconds', value: 60 }],
    ['too_many_attempts', '61', { unit: 'minutes', value: 2 }],
    ['too_many_attempts', '900', { unit: 'minutes', value: 15 }],
    ['too_many_attempts', null, { unit: 'none', value: 0 }],
    ['too_many_attempts', 'soon', { unit: 'none', value: 0 }],
    ['rate_limited', '5', { unit: 'seconds', value: 5 }],
    ['rate_limited', '60', { unit: 'seconds', value: 60 }],
    ['rate_limited', '61', { unit: 'minutes', value: 2 }],
    ['rate_limited', null, { unit: 'none', value: 0 }],
  ])('429 %s with Retry-After %s shows the message, then the wait time, announced, focus kept', (code, retryAfter, wait) => {
    expect(mapSignInError(api(429, code, retryAfter))).toEqual({
      fieldErrors: {},
      formMessage: `errors.${code}`,
      waitTime: wait,
      focus: null,
      announce: true,
    });
  });

  it.each([
    ['a network failure', new PortalApiError(0, 'network'), 'errors.generic'],
    ['a timeout', new PortalApiError(0, 'timeout'), 'errors.generic'],
    ['a 500 with an unreadable body', api(500, 'generic'), 'errors.generic'],
    ['a 502 from a proxy', api(502, 'generic'), 'errors.generic'],
    ['a 503 service_unavailable', api(503, 'service_unavailable'), 'errors.service_unavailable'],
    ['a code with no text', api(500, 'brand_new_code'), 'errors.generic'],
    ['a value that is not an API error', new Error('boom'), 'errors.generic'],
  ])('%s shows a form message, announced, with no wait time and no focus move', (_name, error, key) => {
    expect(mapSignInError(error)).toEqual({
      fieldErrors: {},
      formMessage: key,
      waitTime: null,
      focus: null,
      announce: true,
    });
  });

  it('maps a 422 other than invalid_phone to a form message instead of blaming the phone', () => {
    expect(mapSignInError(api(422, 'unknown_field')).formMessage).toBe('errors.unknown_field');
    expect(mapSignInError(api(422, 'invalid_request')).formMessage).toBe('errors.generic');
  });

  it('chooses the focus target from the field order: the phone before the password', () => {
    expect(SIGN_IN_FIELDS).toEqual(['phone', 'password']);
    expect(mapSignInError(api(422, 'invalid_phone')).focus).toBe('phone');
    expect(SIGN_IN_FIELD_IDS.phone).toBe(SIGN_IN_IDS.phone);
    expect(SIGN_IN_FIELD_IDS.password).toBe(SIGN_IN_IDS.password);
  });

  it('only reads status, code and Retry-After from the error', () => {
    expect(failureOf(api(429, 'rate_limited', '7'))).toEqual({ status: 429, code: 'rate_limited', retryAfter: '7' });
    expect(failureOf('nope')).toEqual({ status: 0, code: 'generic', retryAfter: null });
  });

  it('uses translation keys that exist, with text, in both languages', () => {
    const feedbacks = [
      mapSignInError(api(401, 'invalid_credentials')),
      mapSignInError(api(422, 'invalid_phone')),
      mapSignInError(api(429, 'too_many_attempts', '5')),
      mapSignInError(api(429, 'rate_limited', '5')),
      mapSignInError(new PortalApiError(0, 'network')),
    ];
    const keys = feedbacks.flatMap((f) => [...Object.values(f.fieldErrors), ...(f.formMessage === null ? [] : [f.formMessage])]);
    for (const language of LANGUAGES) {
      const i18n = createPortalI18n(language);
      for (const key of [...keys, 'a11y.required', 'a11y.errorPrefix', 'portal.waitTime.none']) {
        expect(i18n.exists(key), `${language} ${key}`).toBe(true);
        expect(i18n.t(key).length, `${language} ${key}`).toBeGreaterThan(0);
      }
    }
  });
});

// --- feedback helpers --------------------------------------------------------------------------------

describe('feedback helpers', () => {
  it('describedBy joins the present ids in order and is undefined when none is present', () => {
    expect(describedBy(['err', 'form', 'hint'])).toBe('err form hint');
    expect(describedBy([false, null, 'hint', undefined, ''])).toBe('hint');
    expect(describedBy([false, null])).toBeUndefined();
  });

  it('drops the error of an edited field, and the wait time with the last message it belonged to', () => {
    const feedback = mapSignInError(api(422, 'invalid_phone'));
    expect(withoutFieldErrors(feedback, [])).toBe(feedback);
    expect(withoutFieldErrors(feedback, ['password']).fieldErrors).toEqual({ phone: 'errors.invalid_phone' });
    expect(withoutFieldErrors(feedback, ['phone']).fieldErrors).toEqual({});
    const limited = mapSignInError(api(429, 'rate_limited', '5'));
    expect(withoutFieldErrors(limited, ['phone']).waitTime).toEqual({ unit: 'seconds', value: 5 });
  });

  it('reads one field error', () => {
    const feedback = mapSignInError(api(422, 'invalid_phone'));
    expect(fieldErrorKey(feedback, 'phone')).toBe('errors.invalid_phone');
    expect(fieldErrorKey(feedback, 'password')).toBeNull();
    expect(fieldErrorKey(EMPTY_FEEDBACK, 'toString')).toBeNull();
  });

  it.each(LANGUAGES)('writes the announcement and the visible line in %s: message, then the wait time', (language) => {
    const t = bindTranslate((key: string, options?: { count: number }) => {
      const i18n = createPortalI18n(language);
      return options === undefined ? i18n.t(key) : i18n.t(key, options);
    });
    const feedback = mapSignInError(api(429, 'too_many_attempts', '61'));
    const i18n = createPortalI18n(language);
    const expected = `${i18n.t('errors.too_many_attempts')} ${i18n.t('portal.waitTime.minutes', { count: 2 })}`;
    expect(announcementText(feedback, t)).toBe(expected);
    expect(errorLine(t, 'errors.too_many_attempts', feedback.waitTime)).toBe(`${i18n.t('a11y.errorPrefix')} ${expected}`);
    expect(announcementText(mapSignInError(api(401, 'invalid_credentials')), t)).toBe(i18n.t('portal.signIn.error'));
  });

  it('says "try again later", with no number, when there is no usable Retry-After', () => {
    for (const language of LANGUAGES) {
      const i18n = createPortalI18n(language);
      const t = bindTranslate((key: string, options?: { count: number }) =>
        options === undefined ? i18n.t(key) : i18n.t(key, options),
      );
      const line = announcementText(mapSignInError(api(429, 'rate_limited', null)), t);
      expect(line).toBe(`${i18n.t('errors.rate_limited')} ${i18n.t('portal.waitTime.none')}`);
      expect(line).not.toMatch(/\d/);
    }
  });

  it('uses the plural forms of both languages for seconds and minutes', () => {
    const he = createPortalI18n('he');
    const en = createPortalI18n('en');
    expect(he.t('portal.waitTime.seconds', { count: 1 })).toBe(heService.portal.waitTime.seconds_one);
    expect(he.t('portal.waitTime.seconds', { count: 5 })).toBe('נסו שוב בעוד 5 שניות.');
    expect(he.t('portal.waitTime.minutes', { count: 2 })).toBe(heService.portal.waitTime.minutes_two);
    expect(en.t('portal.waitTime.seconds', { count: 1 })).toBe(enService.portal.waitTime.seconds_one);
    expect(en.t('portal.waitTime.seconds', { count: 60 })).toBe('Try again in 60 seconds.');
    expect(en.t('portal.waitTime.minutes', { count: 15 })).toBe('Try again in 15 minutes.');
  });
});

// --- success ---------------------------------------------------------------------------------------

describe('completeAuthentication (sign-in success)', () => {
  const NOW = 1_800_000_000;
  const response = { access_token: 'opaque-token', token_type: 'bearer', expires_in: 28_800, must_change_password: false };

  function setup() {
    const storage = memoryStorage();
    const tokens = createTokenStore({ storage, now: () => NOW, events: null });
    const client = new QueryClient();
    return { storage, tokens, client };
  }

  it('stores the token with exp = now + expires_in and invalidates the profile query', () => {
    const { storage, tokens, client } = setup();
    const profile: CustomerProfile = { full_name: 'A', phone: '0501234567', city: null, apartment: null, must_change_password: false };
    client.setQueryData(portalKeys.me, profile);
    expect(completeAuthentication(response, tokens, client)).toBe(true);
    expect(tokens.getToken()).toBe('opaque-token');
    expect(JSON.parse(storage.data.get(CUSTOMER_TOKEN_KEY) as string)).toEqual({ token: 'opaque-token', exp: NOW + 28_800 });
    expect(client.getQueryState(portalKeys.me)?.isInvalidated).toBe(true);
  });

  it('puts the flag of the response into a cached profile, so the gate moves on at once', () => {
    const { tokens, client } = setup();
    const profile: CustomerProfile = { full_name: 'A', phone: '0501234567', city: null, apartment: null, must_change_password: true };
    client.setQueryData(portalKeys.me, profile);
    completeAuthentication(response, tokens, client);
    expect(client.getQueryData<CustomerProfile>(portalKeys.me)?.must_change_password).toBe(false);
    completeAuthentication({ ...response, must_change_password: true }, tokens, client);
    expect(client.getQueryData<CustomerProfile>(portalKeys.me)?.must_change_password).toBe(true);
  });

  it('creates no profile entry when there is none', () => {
    const { tokens, client } = setup();
    completeAuthentication(response, tokens, client);
    expect(client.getQueryData(portalKeys.me)).toBeUndefined();
  });

  it.each([
    ['null', null],
    ['a string', 'ok'],
    ['an empty object', {}],
    ['an empty token', { ...response, access_token: '' }],
    ['a numeric token', { ...response, access_token: 5 }],
    ['no flag', { access_token: 'x', expires_in: 10 }],
    ['no usable lifetime', { access_token: 'x', must_change_password: false }],
    ['a zero lifetime', { access_token: 'x', expires_in: 0, must_change_password: false }],
  ])('stores nothing and returns false for %s', (_name, value) => {
    const { tokens, client } = setup();
    expect(completeAuthentication(value, tokens, client)).toBe(false);
    expect(tokens.getToken()).toBeNull();
  });

  it('parses only a response with a token and a boolean flag', () => {
    expect(parseAuthTokenResponse(response)).toEqual({ access_token: 'opaque-token', expires_in: 28_800, must_change_password: false });
    expect(parseAuthTokenResponse({ ...response, must_change_password: 'false' })).toBeNull();
  });
});

// --- markup ----------------------------------------------------------------------------------------

describe.each(LANGUAGES)('SignInForm markup in %s', (language) => {
  const dir = language === 'he' ? 'rtl' : 'ltr';
  const markup = render(language, createElement(SignInForm, formProps));

  it('follows the language: lang and dir on the screen, the heading and the labels in that language', () => {
    expect(markup).toContain(`<section lang="${language}" dir="${dir}">`);
    expect(markup).toContain(`<h1>${text(language, 'portal.signIn.title')}</h1>`);
    expect(markup).toContain(text(language, 'portal.signIn.phone'));
    expect(markup).toContain(text(language, 'portal.signIn.password'));
    expect(markup).toContain(text(language, 'portal.signIn.submit'));
  });

  it('has a visible label tied to each input, with the "(required)" text and no placeholder', () => {
    const required = text(language, 'a11y.required');
    for (const [id, key] of [
      [SIGN_IN_IDS.phone, 'portal.signIn.phone'],
      [SIGN_IN_IDS.password, 'portal.signIn.password'],
    ] as const) {
      expect(markup).toContain(`<label for="${id}">${text(language, key)}<span class="portal-required"> ${required}</span></label>`);
    }
    expect(markup).not.toContain('placeholder');
    expect(markup.match(/<label /g)).toHaveLength(2);
    expect(inputTag(markup, SIGN_IN_IDS.phone)).toContain('aria-required="true"');
    expect(inputTag(markup, SIGN_IN_IDS.password)).toContain('aria-required="true"');
  });

  it('gives the phone input type tel, numeric input mode, left-to-right direction and a hint', () => {
    const tag = inputTag(markup, SIGN_IN_IDS.phone);
    expect(tag).toContain('type="tel"');
    expect(tag).toContain('inputMode="numeric"');
    expect(tag).toContain('dir="ltr"');
    expect(tag).toContain('autoComplete="username"');
    expect(tag).not.toContain('maxLength');
    expect(tag).toContain(`aria-describedby="${SIGN_IN_IDS.phoneHint}"`);
    expect(markup).toContain(`<span id="${SIGN_IN_IDS.phoneHint}" class="portal-hint">${text(language, 'portal.signIn.phoneHint')}</span>`);
  });

  it('gives the password input a masked type, numeric input mode, four characters and current-password', () => {
    const tag = inputTag(markup, SIGN_IN_IDS.password);
    expect(tag).toContain('type="password"');
    expect(tag).toContain('inputMode="numeric"');
    expect(tag).toContain('maxLength="4"');
    expect(tag).toContain('autoComplete="current-password"');
    expect(tag).toContain(`aria-describedby="${SIGN_IN_IDS.passwordHint}"`);
  });

  it('shows no error markup, no invalid field and no session-expired notice by default', () => {
    expect(markup).not.toContain('aria-invalid');
    expect(markup).not.toContain('portal-error');
    expect(markup).not.toContain('portal-notice');
  });

  it('keeps the typed values and enables the submit button when idle', () => {
    const typed = render(language, createElement(SignInForm, { ...formProps, values: { phone: '0501234567', password: '1234' } }));
    expect(inputTag(typed, SIGN_IN_IDS.phone)).toContain('value="0501234567"');
    expect(inputTag(typed, SIGN_IN_IDS.password)).toContain('value="1234"');
    expect(typed).toMatch(/<button[^>]*type="submit"(?![^>]*disabled)[^>]*>/);
  });

  it('disables the submit button and says it is working while the request is pending', () => {
    const pending = render(language, createElement(SignInForm, { ...formProps, pending: true }));
    expect(pending).toMatch(/<button[^>]*type="submit"[^>]*disabled=""[^>]*>/);
    expect(pending).toContain(text(language, 'portal.signIn.submitting'));
    expect(pending).toContain('aria-busy="true"');
  });

  it('shows the session-expired notice when asked', () => {
    const expired = render(language, createElement(SignInForm, { ...formProps, sessionExpired: true }));
    expect(expired).toContain(`id="${SIGN_IN_IDS.sessionExpired}"`);
    expect(expired).toContain(text(language, 'portal.signIn.sessionExpired'));
  });

  it('wires a credential error: one text, linked from both inputs, no field marked invalid, no role of its own', () => {
    const failed = render(language, createElement(SignInForm, { ...formProps, feedback: mapSignInError(api(401, 'invalid_credentials')) }));
    expect(failed).toContain(`<p id="${SIGN_IN_IDS.formError}" class="portal-notice portal-notice--error">`);
    expect(failed).toContain(text(language, 'portal.signIn.error'));
    expect(failed).toContain(text(language, 'a11y.errorPrefix'));
    for (const id of [SIGN_IN_IDS.phone, SIGN_IN_IDS.password]) {
      expect(inputTag(failed, id)).toMatch(new RegExp(`aria-describedby="${SIGN_IN_IDS.formError} `));
    }
    expect(failed).not.toContain('aria-invalid');
    // The alert live region is the one place the message is announced; a second role would read it twice.
    expect(failed).not.toContain('role=');
  });

  it('gives the three credential failures the same markup', () => {
    const markups = [api(401, 'invalid_credentials'), api(401, 'invalid_credentials', '9'), api(401, 'invalid_token')].map((error) =>
      render(language, createElement(SignInForm, { ...formProps, feedback: mapSignInError(error) })),
    );
    expect(new Set(markups).size).toBe(1);
  });

  it('wires a phone error: aria-invalid, the message first in aria-describedby, the message element present', () => {
    const failed = render(language, createElement(SignInForm, { ...formProps, feedback: mapSignInError(api(422, 'invalid_phone')) }));
    const phone = inputTag(failed, SIGN_IN_IDS.phone);
    expect(phone).toContain('aria-invalid="true"');
    expect(phone).toContain(`aria-describedby="${SIGN_IN_IDS.phoneError} ${SIGN_IN_IDS.phoneHint}"`);
    expect(failed).toContain(`<p id="${SIGN_IN_IDS.phoneError}" class="portal-error">`);
    expect(failed).toContain(text(language, 'errors.invalid_phone'));
    expect(inputTag(failed, SIGN_IN_IDS.password)).not.toContain('aria-invalid');
    expect(failed).not.toContain(SIGN_IN_IDS.formError);
  });

  it.each([
    ['too_many_attempts', '5', 'seconds', 5],
    ['too_many_attempts', '61', 'minutes', 2],
    ['rate_limited', '60', 'seconds', 60],
  ] as const)('shows %s with Retry-After %s: the message, then the wait time in the same message', (code, retryAfter, unit, count) => {
    const failed = render(language, createElement(SignInForm, { ...formProps, feedback: mapSignInError(api(429, code, retryAfter)) }));
    const message = text(language, `errors.${code}`);
    const wait = text(language, `portal.waitTime.${unit}`, count);
    expect(failed).toContain(`${message} ${wait}</p>`);
    expect(failed).toContain(`id="${SIGN_IN_IDS.formError}"`);
  });

  it('shows "try again later" for a 429 with no Retry-After', () => {
    const failed = render(language, createElement(SignInForm, { ...formProps, feedback: mapSignInError(api(429, 'rate_limited')) }));
    expect(failed).toContain(`${text(language, 'errors.rate_limited')} ${text(language, 'portal.waitTime.none')}</p>`);
  });

  it('shows a network failure as the generic message', () => {
    const failed = render(language, createElement(SignInForm, { ...formProps, feedback: mapSignInError(new PortalApiError(0, 'network')) }));
    expect(failed).toContain(text(language, 'errors.generic'));
  });
});

describe('SignInScreen container markup', () => {
  it.each(LANGUAGES)('renders the empty form inside the providers in %s, with only the two live regions as roles', (language) => {
    const markup = render(
      language,
      createElement(
        QueryClientProvider,
        { client: new QueryClient() },
        createElement(PortalAnnouncerProvider, null, createElement(SignInScreen)),
      ),
    );
    expect(markup).toContain(`<h1>${text(language, 'portal.signIn.title')}</h1>`);
    expect(inputTag(markup, SIGN_IN_IDS.phone)).toContain('value=""');
    expect(markup).toMatch(/<button[^>]*type="submit"(?![^>]*disabled)[^>]*>/);
    expect(markup.match(/role="alert"/g)).toHaveLength(1);
    expect(markup.match(/role="status"/g)).toHaveLength(1);
  });
});

describe('AuthField', () => {
  it('lists the form-level message between the field message and the hint', () => {
    const markup = renderToStaticMarkup(
      createElement(AuthField, {
        id: 'f',
        name: 'f',
        label: 'Label',
        requiredText: ' (r)',
        hint: 'Hint',
        hintId: 'f-hint',
        errorId: 'f-error',
        error: 'Bad',
        formErrorId: 'form-error',
        type: 'password',
        autoComplete: 'off',
        value: '',
        onChange: () => undefined,
      }),
    );
    expect(markup).toContain('aria-describedby="f-error form-error f-hint"');
    expect(markup).toContain('aria-invalid="true"');
  });
});

// --- what the screen must never do -----------------------------------------------------------------

describe('sign-in sources', () => {
  const read = (name: string): string => readFileSync(fileURLToPath(new URL(`./${name}`, import.meta.url)), 'utf8');

  it.each(['SignInScreen.tsx', 'signInModel.ts', 'authFeedback.ts', 'authSession.ts', 'AuthField.tsx', 'useAuthFocus.ts'])(
    'never logs, and never puts credentials in a URL or in storage: %s',
    (name) => {
      const source = read(name);
      expect(source).not.toMatch(/console\./);
      expect(source).not.toMatch(/location\.(search|hash|href)|history\.(push|replace)State|URLSearchParams/);
      expect(source).not.toMatch(/localStorage|sessionStorage/);
    },
  );

  it('sends the sign-in anonymously with the body only', () => {
    const source = read('SignInScreen.tsx');
    expect(source).toContain("'/auth/sign-in'");
    expect(source).toContain('anonymous: true');
  });
});
