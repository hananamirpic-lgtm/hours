import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import i18next from 'i18next';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setAccessTokenGetter } from '@/api/client';
import { accessibilityContactOf, featuresContactKey, featuresKeys, type FeaturesFetchResult } from '@/service/api/features';
import {
  PORTAL_READ_TIMEOUT_MS,
  PORTAL_SUBMIT_TIMEOUT_MS,
  PortalApiError,
  endpointOf,
  handleUnauthorized,
  isFrameworkNotFound,
  parseErrorBody,
  portalFetch,
  portalUrl,
} from '@/service/api/portalFetch';
import { PortalAnnouncerProvider } from '@/service/portal/Announcer';
import { LanguageSwitch } from '@/service/portal/LanguageSwitch';
import { PortalApp } from '@/service/portal/PortalApp';
import { isolateLtr } from '@/service/portal/PortalShell';
import {
  CUSTOMER_TOKEN_KEY,
  createTokenStore,
  customerTokenStore,
  isExpired,
  jwtExpiry,
  parseStoredToken,
  tokenExpiry,
  type TokenStore,
} from '@/service/portal/customerToken';
import {
  PORTAL_LANGUAGE_KEY,
  applyDocumentLanguage,
  createPortalI18n,
  directionOf,
  readStoredLanguage,
  resolveLanguage,
  setLanguage,
  writeStoredLanguage,
} from '@/service/portal/portalI18n';
import { reactToError, shouldRetryQuery } from '@/service/portal/portalQueryClient';
import { portalKeys, sessionView, type CustomerProfile } from '@/service/portal/session';
import type { StorageLike } from '@/service/portal/storage';
import { isPortalPath } from '@/service/portalPath';
import enService from '../../i18n/locales/en/service.json';
import heService from '../../i18n/locales/he/service.json';

// --- helpers ---------------------------------------------------------------------------------------

function memoryStorage(initial: Record<string, string> = {}): StorageLike & { data: Map<string, string> } {
  const data = new Map(Object.entries(initial));
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

function throwingStorage(): StorageLike {
  const blocked = (): never => {
    throw new Error('storage is blocked');
  };
  return { getItem: blocked, setItem: blocked, removeItem: blocked };
}

/** A storage that reads fine but refuses to write, like a full quota. */
function readOnlyStorage(): StorageLike {
  const inner = memoryStorage();
  return {
    getItem: (key) => inner.getItem(key),
    setItem: () => {
      throw new Error('quota exceeded');
    },
    removeItem: (key) => inner.removeItem(key),
  };
}

function base64Url(text: string): string {
  return btoa(text).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
}

function jwtWith(payload: Record<string, unknown>): string {
  return ['header', base64Url(JSON.stringify(payload)), 'signature'].join('.');
}

const NOW = 1_800_000_000;

// --- token store -----------------------------------------------------------------------------------

describe('customer token store', () => {
  it('sets, gets and clears a token, storing {token, exp} under hours.service.customerToken', () => {
    const storage = memoryStorage();
    const store = createTokenStore({ storage, now: () => NOW, events: null });
    expect(store.getToken()).toBeNull();
    store.setToken('abc', NOW + 100);
    expect(store.getToken()).toBe('abc');
    expect(CUSTOMER_TOKEN_KEY).toBe('hours.service.customerToken');
    expect(JSON.parse(storage.data.get(CUSTOMER_TOKEN_KEY) as string)).toEqual({ token: 'abc', exp: NOW + 100 });
    store.clearToken();
    expect(store.getToken()).toBeNull();
    expect(storage.data.has(CUSTOMER_TOKEN_KEY)).toBe(false);
  });

  it('reads a token written earlier by another page load', () => {
    const storage = memoryStorage({ [CUSTOMER_TOKEN_KEY]: JSON.stringify({ token: 'old', exp: NOW + 5 }) });
    expect(createTokenStore({ storage, now: () => NOW, events: null }).getToken()).toBe('old');
  });

  it('returns null and removes the entry when the token has expired, at the expiry second and after', () => {
    for (const exp of [NOW - 1, NOW]) {
      const storage = memoryStorage({ [CUSTOMER_TOKEN_KEY]: JSON.stringify({ token: 'dead', exp }) });
      const store = createTokenStore({ storage, now: () => NOW, events: null });
      expect(store.getToken()).toBeNull();
      expect(storage.data.has(CUSTOMER_TOKEN_KEY)).toBe(false);
    }
    expect(isExpired(NOW + 1, NOW)).toBe(false);
    expect(isExpired(NOW, NOW)).toBe(true);
  });

  it.each([
    ['not JSON', 'not json'],
    ['a JSON string', '"abc"'],
    ['null', 'null'],
    ['an array', '[]'],
    ['no exp', JSON.stringify({ token: 'abc' })],
    ['no token', JSON.stringify({ exp: NOW + 10 })],
    ['an empty token', JSON.stringify({ token: '', exp: NOW + 10 })],
    ['a string exp', JSON.stringify({ token: 'abc', exp: String(NOW + 10) })],
    ['a zero exp', JSON.stringify({ token: 'abc', exp: 0 })],
    ['a numeric token', JSON.stringify({ token: 5, exp: NOW + 10 })],
  ])('returns null and removes the entry when the stored value is malformed: %s', (_name, raw) => {
    const storage = memoryStorage({ [CUSTOMER_TOKEN_KEY]: raw });
    const store = createTokenStore({ storage, now: () => NOW, events: null });
    expect(store.getToken()).toBeNull();
    expect(storage.data.has(CUSTOMER_TOKEN_KEY)).toBe(false);
    expect(parseStoredToken(raw)).toBeNull();
  });

  it('falls back to memory when storage throws on every call', () => {
    const store = createTokenStore({ storage: throwingStorage(), now: () => NOW, events: null });
    expect(store.getToken()).toBeNull();
    expect(() => store.setToken('mem', NOW + 50)).not.toThrow();
    expect(store.getToken()).toBe('mem');
    expect(() => store.clearToken()).not.toThrow();
    expect(store.getToken()).toBeNull();
  });

  it('falls back to memory when there is no storage, or the storage accessor throws', () => {
    for (const storage of [
      null,
      (): StorageLike => {
        throw new Error('SecurityError');
      },
    ]) {
      const store = createTokenStore({ storage, now: () => NOW, events: null });
      store.setToken('mem', NOW + 50);
      expect(store.getToken()).toBe('mem');
    }
  });

  it('keeps the token in memory when only the write fails, and still drops it when it expires', () => {
    let now = NOW;
    const store = createTokenStore({ storage: readOnlyStorage(), now: () => now, events: null });
    store.setToken('mem', NOW + 50);
    expect(store.getToken()).toBe('mem');
    now = NOW + 50;
    expect(store.getToken()).toBeNull();
  });

  it('notifies subscribers on set, clear and expireSession, and expireSession leaves a notice until the next sign-in', () => {
    const store = createTokenStore({ storage: memoryStorage(), now: () => NOW, events: null });
    const listener = vi.fn();
    const unsubscribe = store.subscribe(listener);
    store.setToken('a', NOW + 100);
    store.clearToken();
    expect(listener).toHaveBeenCalledTimes(2);
    expect(store.getSessionExpired()).toBe(false);
    store.setToken('b', NOW + 100);
    store.expireSession();
    expect(store.getToken()).toBeNull();
    expect(store.getSessionExpired()).toBe(true);
    store.setToken('c', NOW + 100);
    expect(store.getSessionExpired()).toBe(false);
    unsubscribe();
    const calls = listener.mock.calls.length;
    store.clearToken();
    expect(listener).toHaveBeenCalledTimes(calls);
  });

  it('wakes subscribers when the token runs out while the page is open, with the session-expired notice', () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(NOW * 1000);
      const store = createTokenStore({ storage: memoryStorage(), events: null });
      const listener = vi.fn();
      store.subscribe(listener);
      store.setToken('short', NOW + 10);
      listener.mockClear();
      vi.advanceTimersByTime(9_000);
      expect(store.getToken()).toBe('short');
      vi.advanceTimersByTime(2_000);
      expect(listener).toHaveBeenCalled();
      expect(store.getToken()).toBeNull();
      expect(store.getSessionExpired()).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('derives exp from the JWT payload, else from expires_in, else gives up', () => {
    expect(jwtExpiry(jwtWith({ exp: NOW + 28_800 }))).toBe(NOW + 28_800);
    expect(jwtExpiry('not-a-jwt')).toBeNull();
    expect(jwtExpiry(jwtWith({ sub: 'x' }))).toBeNull();
    expect(jwtExpiry('a.@@@.c')).toBeNull();
    expect(tokenExpiry(jwtWith({ exp: NOW + 5 }), 28_800, NOW)).toBe(NOW + 5);
    expect(tokenExpiry('opaque', 28_800, NOW)).toBe(NOW + 28_800);
    expect(tokenExpiry('opaque', 0, NOW)).toBeNull();
    expect(tokenExpiry('opaque', undefined, NOW)).toBeNull();
    const store = createTokenStore({ storage: memoryStorage(), now: () => NOW, events: null });
    expect(store.setTokenFromResponse('opaque', 'soon')).toBe(false);
    expect(store.getToken()).toBeNull();
    expect(store.setTokenFromResponse('opaque', 100)).toBe(true);
    expect(store.getToken()).toBe('opaque');
  });
});

// --- language --------------------------------------------------------------------------------------

describe('portal language', () => {
  it.each([
    [null, 'he'],
    [undefined, 'he'],
    ['', 'he'],
    ['fr', 'he'],
    ['EN', 'he'],
    [' en', 'he'],
    [5, 'he'],
    ['he', 'he'],
    ['en', 'en'],
  ] as const)('resolveLanguage(%j) is %s', (stored, expected) => {
    expect(resolveLanguage(stored)).toBe(expected);
  });

  it('reads the stored choice and shows Hebrew for empty, invalid, missing or throwing storage', () => {
    expect(PORTAL_LANGUAGE_KEY).toBe('hours.service.language');
    expect(readStoredLanguage(memoryStorage({ [PORTAL_LANGUAGE_KEY]: 'en' }))).toBe('en');
    expect(readStoredLanguage(memoryStorage({ [PORTAL_LANGUAGE_KEY]: 'he' }))).toBe('he');
    expect(readStoredLanguage(memoryStorage())).toBe('he');
    expect(readStoredLanguage(memoryStorage({ [PORTAL_LANGUAGE_KEY]: '' }))).toBe('he');
    expect(readStoredLanguage(memoryStorage({ [PORTAL_LANGUAGE_KEY]: 'fr' }))).toBe('he');
    expect(readStoredLanguage(throwingStorage())).toBe('he');
    expect(readStoredLanguage(null)).toBe('he');
  });

  it('never throws when storing the choice fails', () => {
    const storage = memoryStorage();
    expect(writeStoredLanguage('en', storage)).toBe(true);
    expect(storage.data.get(PORTAL_LANGUAGE_KEY)).toBe('en');
    expect(writeStoredLanguage('en', throwingStorage())).toBe(false);
    expect(writeStoredLanguage('en', null)).toBe(false);
  });

  it('sets lang and dir on the document: he is rtl, en is ltr', () => {
    const doc = { documentElement: { lang: '', dir: '' } } as unknown as Document;
    applyDocumentLanguage('he', doc);
    expect([doc.documentElement.lang, doc.documentElement.dir]).toEqual(['he', 'rtl']);
    applyDocumentLanguage('en', doc);
    expect([doc.documentElement.lang, doc.documentElement.dir]).toEqual(['en', 'ltr']);
    expect(directionOf('he')).toBe('rtl');
    expect(directionOf('en')).toBe('ltr');
    expect(() => applyDocumentLanguage('he', undefined)).not.toThrow();
  });

  it('uses its own i18next instance and leaves the global one alone', () => {
    const portal = createPortalI18n('he');
    expect(portal).not.toBe(i18next);
    expect(i18next.isInitialized).toBeFalsy();
    expect(portal.t('portal.signIn.title')).toBe(heService.portal.signIn.title);
    expect(createPortalI18n('en').t('portal.signIn.title')).toBe(enService.portal.signIn.title);
  });

  it('switches language, stores it, and still switches when storage fails', async () => {
    const instance = createPortalI18n('he');
    const storage = memoryStorage();
    await setLanguage('en', instance, storage);
    expect(instance.language).toBe('en');
    expect(storage.data.get(PORTAL_LANGUAGE_KEY)).toBe('en');
    await expect(setLanguage('he', instance, throwingStorage())).resolves.toBe('he');
    expect(instance.language).toBe('he');
  });
});

// --- 401 handling and error parsing ----------------------------------------------------------------

describe('handleUnauthorized', () => {
  it.each([
    // endpoint, code, clearToken, showSessionExpired
    ['other', 'invalid_token', true, true],
    ['other', 'not_authenticated', true, true],
    ['other', 'invalid_credentials', true, true],
    ['change-password', 'invalid_credentials', false, false],
    ['change-password', 'invalid_token', true, true],
    ['change-password', 'not_authenticated', true, true],
    ['sign-in', 'invalid_credentials', false, false],
    ['sign-in', 'generic', false, false],
    ['sign-out', 'invalid_token', true, false],
  ] as const)('%s with %s: clear=%s, expired message=%s', (endpoint, code, clearToken, showSessionExpired) => {
    expect(handleUnauthorized(code, { endpoint })).toEqual({ clearToken, showSessionExpired });
  });

  it('classifies endpoints by path', () => {
    expect(endpointOf('/auth/sign-in')).toBe('sign-in');
    expect(endpointOf('/auth/change-password')).toBe('change-password');
    expect(endpointOf('/auth/sign-out')).toBe('sign-out');
    expect(endpointOf('/auth/me')).toBe('other');
    expect(endpointOf('/tickets?page=2')).toBe('other');
    expect(endpointOf('/auth/sign-in/')).toBe('sign-in');
  });
});

describe('parseErrorBody', () => {
  it('reads detail.error.code and fields', () => {
    expect(
      parseErrorBody({ detail: { error: { code: 'invalid_description', fields: { description: 'invalid_description', city: 'invalid_city' } } } }),
    ).toEqual({ code: 'invalid_description', fields: { description: 'invalid_description', city: 'invalid_city' } });
  });

  it('also reads an envelope lifted to the top level', () => {
    expect(parseErrorBody({ error: { code: 'rate_limited' } })).toEqual({ code: 'rate_limited', fields: {} });
  });

  it.each([
    [undefined],
    [null],
    ['text'],
    [[]],
    [{}],
    [{ detail: 'Not Found' }],
    [{ detail: [{ loc: ['body'], msg: 'bad', type: 'value_error' }] }],
    [{ detail: { error: {} } }],
    [{ detail: { error: { code: 5 } } }],
    [{ detail: { error: { code: '' } } }],
  ])('gives code generic for %j', (body) => {
    expect(parseErrorBody(body)).toEqual({ code: 'generic', fields: {} });
  });

  it('drops fields entries that are not strings', () => {
    expect(parseErrorBody({ detail: { error: { code: 'x', fields: { a: 'ok', b: 3, c: '' } } } }).fields).toEqual({ a: 'ok' });
  });
});

// --- portalFetch -----------------------------------------------------------------------------------

function tokensOf(initial: string | null): TokenStore {
  const store = createTokenStore({ storage: memoryStorage(), now: () => NOW, events: null });
  if (initial !== null) store.setToken(initial, NOW + 1_000);
  return store;
}

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } });
}

type FetchCall = [string, RequestInit & { headers: Record<string, string> }];

function stubFetch(impl: (url: string, init: RequestInit) => Promise<Response>) {
  const fn = vi.fn(impl);
  vi.stubGlobal('fetch', fn);
  return fn;
}

/** A fetch that never answers and rejects when its signal aborts, as a real one does. */
function hangingFetch() {
  return stubFetch(
    (_url, init) =>
      new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('The operation was aborted.', 'AbortError')));
      }),
  );
}

describe('portalFetch', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    setAccessTokenGetter(() => null);
  });

  it('calls {base}/service{path} with no cookies, no cache, and a Bearer customer token', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(200, { items: [] }));
    const result = await portalFetch<{ items: unknown[] }>('/tickets?page=1', { tokens: tokensOf('cust-token') });
    expect(result).toEqual({ items: [] });
    const [url, init] = fetchMock.mock.calls[0] as unknown as FetchCall;
    expect(url).toBe('/api/service/tickets?page=1');
    expect(portalUrl('tickets')).toBe('/api/service/tickets');
    expect(init.method).toBe('GET');
    expect(init.credentials).toBe('omit');
    expect(init.cache).toBe('no-store');
    expect(init.headers.Authorization).toBe('Bearer cust-token');
  });

  it('sends no Authorization header without a token, or when anonymous', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(200, {}));
    await portalFetch('/form-config', { tokens: tokensOf(null) });
    await portalFetch('/auth/sign-in', { method: 'POST', json: { phone: '0501234567', password: '1234' }, anonymous: true, tokens: tokensOf('x') });
    const [first, second] = fetchMock.mock.calls as unknown as FetchCall[];
    expect(first?.[1].headers.Authorization).toBeUndefined();
    expect(second?.[1].headers.Authorization).toBeUndefined();
    expect(second?.[1].headers['Content-Type']).toBe('application/json');
    expect(second?.[1].body).toBe(JSON.stringify({ phone: '0501234567', password: '1234' }));
  });

  it('sends a FormData body untouched, without a Content-Type header the browser must set', async () => {
    const fetchMock = stubFetch(async () => jsonResponse(201, { ticket_number: 10001 }));
    const form = new FormData();
    form.set('description', 'leak');
    await portalFetch('/tickets', { method: 'POST', body: form, kind: 'submit', tokens: tokensOf('t') });
    const [, init] = fetchMock.mock.calls[0] as unknown as FetchCall;
    expect(init.body).toBe(form);
    expect(init.headers['Content-Type']).toBeUndefined();
  });

  it('never sends the staff access token, and the staff client never sees the customer token', async () => {
    setAccessTokenGetter(() => 'STAFF-ACCESS-TOKEN');
    const fetchMock = stubFetch(async () => jsonResponse(200, {}));
    customerTokenStore.setToken('CUSTOMER-TOKEN', Math.floor(Date.now() / 1000) + 1_000);
    try {
      await portalFetch('/auth/me');
    } finally {
      customerTokenStore.clearToken();
    }
    const [, init] = fetchMock.mock.calls[0] as unknown as FetchCall;
    expect(init.headers.Authorization).toBe('Bearer CUSTOMER-TOKEN');
    expect(JSON.stringify(init.headers)).not.toContain('STAFF-ACCESS-TOKEN');
  });

  it('turns a JSON error into a PortalApiError with status, code, fields and Retry-After', async () => {
    stubFetch(async () =>
      jsonResponse(
        422,
        { detail: { error: { code: 'invalid_description', fields: { description: 'invalid_description' } } } },
        { 'Retry-After': '90' },
      ),
    );
    const error = await portalFetch('/tickets', { method: 'POST', tokens: tokensOf('t') }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(PortalApiError);
    expect(error).toMatchObject({ status: 422, code: 'invalid_description', fields: { description: 'invalid_description' }, retryAfter: '90' });
  });

  it('reads Retry-After on a 429 and leaves it null when absent', async () => {
    stubFetch(async () => jsonResponse(429, { detail: { error: { code: 'too_many_attempts' } } }, { 'Retry-After': '30' }));
    expect(await portalFetch('/x', { tokens: tokensOf('t') }).catch((e: unknown) => e)).toMatchObject({ status: 429, code: 'too_many_attempts', retryAfter: '30' });
    stubFetch(async () => jsonResponse(500, { detail: { error: { code: 'service_unavailable' } } }));
    expect(await portalFetch('/x', { tokens: tokensOf('t') }).catch((e: unknown) => e)).toMatchObject({ status: 500, retryAfter: null });
  });

  it('turns an error that is not JSON into code generic, whatever the body is', async () => {
    for (const body of ['<html>Bad Gateway</html>', '', 'null', '[1,2]']) {
      stubFetch(async () => new Response(body, { status: 502, headers: { 'Content-Type': 'text/html' } }));
      const error = await portalFetch('/tickets', { tokens: tokensOf('t') }).catch((e: unknown) => e);
      expect(error).toMatchObject({ status: 502, code: 'generic', fields: {}, retryAfter: null });
    }
  });

  it('turns a network failure into status 0 with code network', async () => {
    stubFetch(async () => {
      throw new TypeError('Failed to fetch');
    });
    expect(await portalFetch('/tickets', { tokens: tokensOf('t') }).catch((e: unknown) => e)).toMatchObject({ status: 0, code: 'network' });
  });

  it('turns a success body that is not JSON into code generic', async () => {
    stubFetch(async () => new Response('<html>proxy</html>', { status: 200 }));
    expect(await portalFetch('/tickets', { tokens: tokensOf('t') }).catch((e: unknown) => e)).toMatchObject({ status: 200, code: 'generic' });
  });

  it('resolves to undefined for 204', async () => {
    stubFetch(async () => new Response(null, { status: 204 }));
    await expect(portalFetch('/auth/sign-out', { method: 'POST', tokens: tokensOf('t') })).resolves.toBeUndefined();
  });

  it('aborts a read after 10 seconds and a submit after 120 seconds', async () => {
    expect(PORTAL_READ_TIMEOUT_MS).toBe(10_000);
    expect(PORTAL_SUBMIT_TIMEOUT_MS).toBe(120_000);
    vi.useFakeTimers();
    hangingFetch();

    const read = portalFetch('/tickets', { tokens: tokensOf('t') }).catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(9_999);
    let settled = false;
    void read.then(() => {
      settled = true;
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(await read).toMatchObject({ status: 0, code: 'timeout' });

    const submit = portalFetch('/tickets', { method: 'POST', kind: 'submit', tokens: tokensOf('t') }).catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(10_000);
    settled = false;
    void submit.then(() => {
      settled = true;
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(110_000);
    expect(await submit).toMatchObject({ status: 0, code: 'timeout' });
  });

  it('reports a caller abort as code aborted, not timeout', async () => {
    hangingFetch();
    const controller = new AbortController();
    const pending = portalFetch('/tickets', { tokens: tokensOf('t'), signal: controller.signal }).catch((e: unknown) => e);
    controller.abort();
    expect(await pending).toMatchObject({ status: 0, code: 'aborted' });
  });

  describe('on 401', () => {
    const unauthorized = (code: string) => async () => jsonResponse(401, { detail: { error: { code } } });

    it('clears the token and raises the session-expired notice on an ordinary request', async () => {
      stubFetch(unauthorized('invalid_token'));
      const tokens = tokensOf('t');
      await portalFetch('/tickets', { tokens }).catch(() => undefined);
      expect(tokens.getToken()).toBeNull();
      expect(tokens.getSessionExpired()).toBe(true);
    });

    it('keeps the token on a change-password invalid_credentials', async () => {
      stubFetch(unauthorized('invalid_credentials'));
      const tokens = tokensOf('t');
      const error = await portalFetch('/auth/change-password', { method: 'POST', tokens }).catch((e: unknown) => e);
      expect(error).toMatchObject({ status: 401, code: 'invalid_credentials' });
      expect(tokens.getToken()).toBe('t');
      expect(tokens.getSessionExpired()).toBe(false);
    });

    it('clears the token on any other change-password 401', async () => {
      stubFetch(unauthorized('invalid_token'));
      const tokens = tokensOf('t');
      await portalFetch('/auth/change-password', { method: 'POST', tokens }).catch(() => undefined);
      expect(tokens.getToken()).toBeNull();
      expect(tokens.getSessionExpired()).toBe(true);
    });

    it('leaves the token and the notice alone on the sign-in endpoint itself', async () => {
      stubFetch(unauthorized('invalid_credentials'));
      const tokens = tokensOf('t');
      await portalFetch('/auth/sign-in', { method: 'POST', anonymous: true, tokens }).catch(() => undefined);
      expect(tokens.getToken()).toBe('t');
      expect(tokens.getSessionExpired()).toBe(false);
    });

    it('does not clear a newer token when a stale request is answered 401', async () => {
      const tokens = tokensOf('old');
      stubFetch(async () => {
        tokens.setToken('new', NOW + 1_000);
        return jsonResponse(401, { detail: { error: { code: 'invalid_token' } } });
      });
      await portalFetch('/tickets', { tokens }).catch(() => undefined);
      expect(tokens.getToken()).toBe('new');
    });

    it('does not touch the token for other statuses', async () => {
      stubFetch(async () => jsonResponse(403, { detail: { error: { code: 'password_change_required' } } }));
      const tokens = tokensOf('t');
      await portalFetch('/tickets', { tokens }).catch(() => undefined);
      expect(tokens.getToken()).toBe('t');
    });
  });

  it('recognises the framework 404 (no code) and not an API 404', () => {
    expect(isFrameworkNotFound(new PortalApiError(404, 'generic'))).toBe(true);
    expect(isFrameworkNotFound(new PortalApiError(404, 'ticket_not_found'))).toBe(false);
    expect(isFrameworkNotFound(new PortalApiError(500, 'generic'))).toBe(false);
    expect(isFrameworkNotFound(new Error('x'))).toBe(false);
  });
});

// --- query client rules ----------------------------------------------------------------------------

describe('portal query rules', () => {
  it('retries a network failure, timeout and 5xx once, and a 4xx never', () => {
    expect(shouldRetryQuery(0, new PortalApiError(0, 'network'))).toBe(true);
    expect(shouldRetryQuery(0, new PortalApiError(503, 'service_unavailable'))).toBe(true);
    expect(shouldRetryQuery(1, new PortalApiError(503, 'service_unavailable'))).toBe(false);
    for (const status of [400, 401, 403, 404, 422, 429]) {
      expect(shouldRetryQuery(0, new PortalApiError(status, 'x'))).toBe(false);
    }
  });

  it('asks the flag again after a framework 404 and the profile again after password_change_required', () => {
    const client = new QueryClient();
    const spy = vi.spyOn(client, 'invalidateQueries').mockResolvedValue();
    reactToError(client, new PortalApiError(404, 'generic'));
    expect(spy).toHaveBeenLastCalledWith({ queryKey: featuresKeys.all });
    reactToError(client, new PortalApiError(403, 'password_change_required'));
    expect(spy).toHaveBeenLastCalledWith({ queryKey: portalKeys.me });
    spy.mockClear();
    reactToError(client, new PortalApiError(404, 'ticket_not_found'));
    reactToError(client, new PortalApiError(500, 'generic'));
    reactToError(client, new Error('x'));
    expect(spy).not.toHaveBeenCalled();
  });
});

describe('sessionView', () => {
  it.each([
    [{ hasToken: false, profileStatus: 'pending', mustChangePassword: undefined }, 'sign-in'],
    [{ hasToken: false, profileStatus: 'success', mustChangePassword: false }, 'sign-in'],
    [{ hasToken: true, profileStatus: 'pending', mustChangePassword: undefined }, 'loading'],
    [{ hasToken: true, profileStatus: 'error', mustChangePassword: undefined }, 'error'],
    [{ hasToken: true, profileStatus: 'success', mustChangePassword: true }, 'change-password'],
    [{ hasToken: true, profileStatus: 'success', mustChangePassword: false }, 'signed-in'],
  ] as const)('%j gives %s', (input, expected) => {
    expect(sessionView(input)).toBe(expected);
  });
});

describe('small pure helpers', () => {
  it('isPortalPath is true only for /service and /service/...', () => {
    for (const path of ['/service', '/service/', '/service/new', '/service/tickets/10001']) expect(isPortalPath(path)).toBe(true);
    for (const path of ['/', '/services', '/service-x', '/serviceable', '/tickets', '/m/service', '/login', '']) expect(isPortalPath(path)).toBe(false);
  });

  it('accessibilityContactOf returns the trimmed contact only for an on answer', () => {
    const answer = (status: number, value: unknown): FeaturesFetchResult => ({ kind: 'response', status, body: { ok: true, value } });
    expect(accessibilityContactOf(answer(200, { service_tickets: true, accessibility_contact: ' 03-1234567 ' }))).toBe('03-1234567');
    expect(accessibilityContactOf(answer(200, { service_tickets: true, accessibility_contact: '  ' }))).toBeNull();
    expect(accessibilityContactOf(answer(200, { service_tickets: true }))).toBeNull();
    expect(accessibilityContactOf(answer(200, { service_tickets: true, accessibility_contact: 5 }))).toBeNull();
    expect(accessibilityContactOf(answer(200, { service_tickets: false, accessibility_contact: 'a@b.co' }))).toBeNull();
    expect(accessibilityContactOf(answer(404, null))).toBeNull();
    expect(accessibilityContactOf({ kind: 'network-error' })).toBeNull();
  });

  it('isolateLtr wraps text in LRI and PDI', () => {
    expect(isolateLtr('03-1')).toBe('\u2066' + '03-1' + '\u2069');
  });
});

// --- rendering (no DOM: react-dom/server) ----------------------------------------------------------

const PROFILE: CustomerProfile = {
  full_name: 'Test Customer',
  phone: '0501234567',
  city: null,
  apartment: null,
  must_change_password: false,
};

interface RenderOptions {
  path: string;
  language?: 'he' | 'en';
  flag?: 'on' | 'off';
  contact?: string | null;
  profile?: CustomerProfile | null;
}

function renderApp(options: RenderOptions): string {
  const client = new QueryClient();
  client.setQueryData(featuresKeys.all, options.flag ?? 'on');
  client.setQueryData(featuresContactKey, options.contact === undefined ? '03-5551234' : options.contact);
  if (options.profile) client.setQueryData(portalKeys.me, options.profile);
  return renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n: createPortalI18n(options.language ?? 'he') },
      createElement(
        QueryClientProvider,
        { client },
        createElement(StaticRouter, { location: `/service${options.path}`, basename: '/service' }, createElement(PortalApp)),
      ),
    ),
  );
}

function renderWithToken(options: RenderOptions): string {
  customerTokenStore.setToken('render-token', Math.floor(Date.now() / 1000) + 3_600);
  try {
    return renderApp(options);
  } finally {
    customerTokenStore.clearToken();
  }
}

describe('portal shell', () => {
  beforeEach(() => {
    customerTokenStore.clearToken();
  });

  it('renders the skip link, main landmark, footer link and both live regions (Hebrew)', () => {
    const html = renderApp({ path: '/accessibility' });
    expect(html).toContain(`<a class="portal-skip-link" href="#main">${heService.a11y.skipToContent}</a>`);
    expect(html).toContain('<main id="main" tabindex="-1"');
    expect(html).toContain('<header');
    expect(html).toContain('<footer');
    expect(html).toContain(`<a href="/service/accessibility">${heService.portal.footer.accessibilityLink}</a>`);
    expect(html).toMatch(/<div role="status" aria-live="polite" aria-atomic="true"[^>]*><\/div>/);
    expect(html).toMatch(/<div role="alert" aria-live="assertive" aria-atomic="true"[^>]*><\/div>/);
    expect(html).toContain(`<h1>${heService.a11y.statement.title}</h1>`);
  });

  it('renders in English too, with the contact from the feature answer isolated left-to-right', () => {
    const html = renderApp({ path: '/accessibility', language: 'en' });
    expect(html).toContain(enService.a11y.skipToContent);
    expect(html).toContain(enService.portal.footer.accessibilityLink);
    expect(html).toContain(`Accessibility requests: ${isolateLtr('03-5551234')}`);
  });

  it('shows no contact line when the feature answer has no contact', () => {
    expect(renderApp({ path: '/accessibility', contact: null })).not.toContain('Accessibility requests');
    expect(renderApp({ path: '/accessibility', contact: null })).not.toContain(heService.portal.footer.accessibilityContact.split('{{')[0] as string);
  });

  it('shows no link or control to a Hours console screen', () => {
    const hrefs = Array.from(renderApp({ path: '/accessibility' }).matchAll(/href="([^"]*)"/g), (m) => m[1]);
    expect(hrefs.sort()).toEqual(['#main', '/service/accessibility']);
  });

  it('shows the language switch on the accessibility page and the sign-in screen, in both languages', () => {
    for (const path of ['/accessibility', '/', '/tickets/10001']) {
      expect(renderApp({ path })).toContain('lang="en" aria-label="English"');
      expect(renderApp({ path, language: 'en' })).toContain('lang="he" aria-label="עברית"');
    }
  });

  it('renders the sign-in screen in place of every gated path while there is no token', () => {
    for (const path of ['/', '/new', '/new/done', '/tickets/10001', '/nowhere']) {
      const html = renderApp({ path });
      expect(html).toContain(`<h1>${heService.portal.signIn.title}</h1>`);
      expect(html).not.toContain(heService.portal.list.title);
      expect(html).not.toContain(heService.portal.account.signOut);
    }
  });

  it('renders the gated routes for a signed-in customer and offers sign out', () => {
    const cases: [string, string][] = [
      ['/', heService.portal.list.title],
      ['/new', heService.portal.form.title],
      ['/new/done', heService.portal.confirmation.title],
      ['/tickets/10001', heService.portal.pageTitle.detail.replace('{{number}}', '10001')],
    ];
    for (const [path, heading] of cases) {
      const html = renderWithToken({ path, profile: PROFILE });
      expect(html).toContain(`<h1>${heading}</h1>`);
      expect(html).toContain(heService.portal.account.signOut);
    }
  });

  it('renders the not-found screen, with no ticket data, for an unknown path', () => {
    const html = renderWithToken({ path: '/nowhere/at/all', profile: PROFILE });
    expect(html).toContain(`<h1>${heService.portal.notFound.title}</h1>`);
    expect(html).not.toContain(heService.portal.list.title);
    expect(html).not.toContain('10001');
  });

  it('renders the change-password screen in place of every gated path while must_change_password is set', () => {
    for (const path of ['/', '/new', '/tickets/10001', '/nowhere']) {
      const html = renderWithToken({ path, profile: { ...PROFILE, must_change_password: true } });
      expect(html).toContain(`<h1>${heService.portal.changePassword.title}</h1>`);
      expect(html).not.toContain(heService.portal.list.title);
    }
  });

  it('keeps the accessibility statement reachable during a forced password change and without a token', () => {
    const html = renderWithToken({ path: '/accessibility', profile: { ...PROFILE, must_change_password: true } });
    expect(html).toContain(`<h1>${heService.a11y.statement.title}</h1>`);
    expect(renderApp({ path: '/accessibility' })).toContain(`<h1>${heService.a11y.statement.title}</h1>`);
  });

  it('shows a loading line, not the sign-in screen, while the profile of a stored token is outstanding', () => {
    const loading = renderWithToken({ path: '/', profile: null });
    expect(loading).toContain(`<p>${heService.portal.loading}</p>`);
    expect(loading).not.toContain(heService.portal.signIn.title);
  });

  it('puts the flag gate first: with the flag off no portal screen, link or language switch renders', () => {
    for (const path of ['/', '/accessibility', '/tickets/10001']) {
      const html = renderWithToken({ path, flag: 'off', profile: PROFILE });
      expect(html).toContain('Service tickets are not available right now.');
      expect(html).not.toContain('portal-skip-link');
      expect(html).not.toContain('<footer');
      expect(html).not.toContain('<form');
      expect(html).not.toContain('lang="en" aria-label');
      expect(html).not.toContain(heService.portal.list.title);
    }
  });
});

describe('LanguageSwitch', () => {
  const renderSwitch = (language: 'he' | 'en'): string =>
    renderToStaticMarkup(
      createElement(
        I18nextProvider,
        { i18n: createPortalI18n(language) },
        createElement(PortalAnnouncerProvider, null, createElement(LanguageSwitch)),
      ),
    );

  it('names the language it switches to, in that language, with lang on the element', () => {
    const toEnglish = renderSwitch('he');
    expect(toEnglish).toContain('lang="en"');
    expect(toEnglish).toContain('aria-label="English"');
    expect(toEnglish).toContain('>English</button>');
    const toHebrew = renderSwitch('en');
    expect(toHebrew).toContain('lang="he"');
    expect(toHebrew).toContain('aria-label="עברית"');
    expect(toHebrew).toContain('>עברית</button>');
  });

  it('is one native button with the same markup in both directions except the language name', () => {
    const shape = (html: string): string =>
      (/<button[\s\S]*?<\/button>/.exec(html)?.[0] ?? '').replace(/lang="..?"/, 'lang=""').replace(/aria-label="[^"]*"/, 'aria-label=""').replace(/>[^<]*</, '><');
    expect(shape(renderSwitch('he'))).toBe(shape(renderSwitch('en')));
    expect(renderSwitch('he').match(/<button/g)).toHaveLength(1);
    expect(renderSwitch('he')).toContain('type="button"');
  });

  it('has Hebrew and English live-region texts for a language change', () => {
    expect(heService.a11y.live.languageChanged).toContain('עברית');
    expect(enService.a11y.live.languageChanged).toContain('English');
  });
});

// --- isolation from the staff app (static import check) --------------------------------------------

describe('portal isolation from the staff app', () => {
  const srcRoot = resolve(import.meta.dirname, '../..');
  const portalDir = join(srcRoot, 'service', 'portal');

  function sourceFiles(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) return sourceFiles(full);
      return /\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [full] : [];
    });
  }

  function specifiers(file: string): string[] {
    const text = readFileSync(file, 'utf8');
    return Array.from(text.matchAll(/(?:from|import)\s+['"]([^'"]+)['"]/g), (m) => m[1] as string);
  }

  const portalFiles = [...sourceFiles(portalDir), join(srcRoot, 'service', 'api', 'portalFetch.ts')];

  const forbidden = (spec: string): boolean =>
    /(^|\/)i18n(\/index)?$/.test(spec) ||
    spec.includes('global.css') ||
    spec.startsWith('@/auth') ||
    spec.startsWith('@/api') ||
    spec.startsWith('@/lib') ||
    spec.startsWith('@/app') ||
    spec.startsWith('@/App') ||
    spec.startsWith('@/styles') ||
    spec.startsWith('@/service/management') ||
    /(^|\/)auth\//.test(spec) ||
    /AuthProvider|tokenStore|apiFetch/.test(spec);

  it('finds the portal source files', () => {
    expect(portalFiles.length).toBeGreaterThan(8);
  });

  it('imports no staff i18n, global.css, staff auth, apiFetch or staff query client', () => {
    const offenders = portalFiles.flatMap((file) =>
      specifiers(file)
        .filter(forbidden)
        .map((spec) => `${relative(srcRoot, file)} imports ${spec}`),
    );
    expect(offenders).toEqual([]);
  });

  it('imports portal.css only from mountPortal.tsx', () => {
    const importers = portalFiles.filter((file) => specifiers(file).some((spec) => spec.endsWith('portal.css')));
    expect(importers.map((file) => relative(srcRoot, file).replace(/\\/g, '/'))).toEqual(['service/portal/mountPortal.tsx']);
  });

  it('is loaded by main.tsx only through a dynamic import, and the staff entry does not import it', () => {
    const main = readFileSync(join(srcRoot, 'main.tsx'), 'utf8');
    expect(main).toContain("import('@/service/portal/mountPortal')");
    expect(main).not.toMatch(/from\s+['"]@\/service\/portal\//);
    expect(main).not.toMatch(/from\s+['"]@\/(App|auth|i18n|lib)/);
    const staff = readFileSync(join(srcRoot, 'mountStaff.tsx'), 'utf8');
    expect(staff).not.toContain('service/portal');
  });
});
