// Tasks 30.15 and 30.17 (R28.3, R28.4, R28.7): the company branding shows once, inside <main> and above
// the one h1, on EVERY portal screen: sign-in, forced change-password, list, new, done, detail, the
// accessibility statement and not-found. It is fed only by the two anonymous endpoints, never by the
// form configuration, and a failed request never changes a screen or signs the customer out.
// Server-rendered markup of the whole route table (no DOM, no testing library), with the portal queries
// preloaded into the cache. Effects do not run in a server render, so the object URL of a logo does not
// exist here; the markup with a logo URL is checked on `PortalBrandingView`, which `PortalBranding` renders.
import { QueryClient, QueryClientProvider, type QueryKey, type QueryState } from '@tanstack/react-query';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import { featuresContactKey, featuresKeys } from '../api/features';
import type { FormBranding, FormConfig } from '../api/formTypes';
import { GENERIC_CODE, PortalApiError } from '../api/portalFetch';
import {
  PUBLIC_BRANDING_QUERY_OPTIONS,
  getPublicBranding,
  getPublicBrandingLogo,
  publicBrandingKey,
  publicBrandingLogoKey,
} from '../api/publicBranding';
import { formConfigKey } from '../api/ticketCreate';
import enService from '../../i18n/locales/en/service.json';
import { customerTokenStore } from './customerToken';
import { PortalApp } from './PortalApp';
import { PortalBrandingView } from './PortalBranding';
import { createPortalI18n, type PortalLanguage } from './portalI18n';
import { portalKeys, type CustomerProfile } from './session';

const PROFILE: CustomerProfile = { full_name: 'Dana', phone: '0501234567', city: null, apartment: null, must_change_password: false };

const CONFIG = (branding: FormBranding): FormConfig => ({
  categories: [{ id: 'cat-1', name_he: 'אינסטלציה', name_en: 'Plumbing' }],
  labels: {},
  settings: { description_required: false, photo_upload_enabled: true },
  localities: [],
  settlements_required: false,
  branding,
});

/** Every screen that renders behind the session gate (the route table of `PortalApp`). */
const GATED_PATHS = ['/', '/new', '/new/done', '/tickets/10001', '/nowhere'] as const;
/** Every screen of the portal. */
const ALL_PATHS = [...GATED_PATHS, '/accessibility'] as const;

const count = (html: string, pattern: RegExp): number => html.match(pattern)?.length ?? 0;

interface Options {
  path: string;
  language?: PortalLanguage;
  token?: boolean;
  profile?: CustomerProfile;
  /** Called with the client before the render, to put the branding and the form configuration into the cache. */
  prepare?: (client: QueryClient) => void;
}

function render(options: Options): string {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(featuresKeys.all, 'on');
  client.setQueryData(featuresContactKey, null);
  client.setQueryData(portalKeys.me, options.profile ?? PROFILE);
  options.prepare?.(client);
  if (options.token !== false) customerTokenStore.setToken('render-token', Math.floor(Date.now() / 1000) + 3_600);
  try {
    return renderToStaticMarkup(
      createElement(
        I18nextProvider,
        { i18n: createPortalI18n(options.language ?? 'en') },
        createElement(
          QueryClientProvider,
          { client },
          createElement(StaticRouter, { location: `/service${options.path}`, basename: '/service' }, createElement(PortalApp)),
        ),
      ),
    );
  } finally {
    customerTokenStore.clearToken();
  }
}

/** The public branding answer in the cache. The form configuration, when asked for, carries other branding on purpose. */
const withBranding = (branding: FormBranding) => (client: QueryClient) => {
  client.setQueryData(publicBrandingKey, branding);
  client.setQueryData(formConfigKey, CONFIG({ company_name: 'Form Config Name', has_logo: false }));
};

/** The error state a query ends in when its request fails, taken from a throwaway client. */
async function failedState(queryKey: QueryKey, queryFn: () => Promise<unknown>): Promise<QueryState<unknown, Error>> {
  const source = new QueryClient();
  await source.fetchQuery({ queryKey, queryFn, retry: false }).catch(() => undefined);
  const state = source.getQueryState(queryKey);
  expect(state?.status).toBe('error');
  return state as QueryState<unknown, Error>;
}

/**
 * Puts a failed query into the cache. A server render treats an errored query as one that is about to be
 * fetched again on mount and reports it as pending, so `retryOnMount: false` keeps the error visible to the
 * components, which is the state a failure leaves in the browser.
 */
const withState = (queryKey: QueryKey, state: QueryState<unknown, Error>) => (client: QueryClient) => {
  client.setQueryDefaults(queryKey, { retryOnMount: false });
  client.getQueryCache().build(client, { queryKey }).setState(state);
};

/** Runs `run` with `fetch` replaced; records every call. */
async function withFetch<T>(answer: () => Response | Promise<Response>, run: (calls: [string, RequestInit][]) => Promise<T>): Promise<T> {
  const original = globalThis.fetch;
  const calls: [string, RequestInit][] = [];
  globalThis.fetch = ((url: string, init: RequestInit) => {
    calls.push([url, init]);
    return Promise.resolve().then(answer);
  }) as typeof fetch;
  try {
    return await run(calls);
  } finally {
    globalThis.fetch = original;
  }
}

afterEach(() => {
  customerTokenStore.clearToken();
  customerTokenStore.dismissSessionExpired();
});

describe('portal branding on every screen', () => {
  for (const language of ['he', 'en'] as const) {
    describe(language, () => {
      it('shows the company name once, inside <main>, above the one h1, on every screen, signed in or not', () => {
        for (const path of ALL_PATHS) {
          for (const token of [true, false]) {
            const html = render({ path, language, token, prepare: withBranding({ company_name: 'Acme Ltd', has_logo: false }) });
            const where = `${path} token=${String(token)}`;
            expect(count(html, /class="portal-branding"/g), where).toBe(1);
            expect(html, where).toContain('<p class="portal-branding-name" dir="auto">Acme Ltd</p>');
            expect(html, where).not.toContain('<img');
            // The form configuration does not feed the header.
            expect(html, where).not.toContain('Form Config Name');
            // No extra heading: the one h1 of the screen stays, and it comes after the branding.
            expect(count(html, /<h1[ >]/g), where).toBe(1);
            expect(html.indexOf('portal-branding'), where).toBeLessThan(html.indexOf('<h1'));
            expect(html.indexOf('<main'), where).toBeLessThan(html.indexOf('portal-branding'));
            expect(html.indexOf('portal-branding'), where).toBeLessThan(html.indexOf('</main>'));
          }
        }
      });

      it('shows it once on /new, where the form frame used to show it', () => {
        const html = render({ path: '/new', language, prepare: withBranding({ company_name: 'Acme Ltd', has_logo: false }) });
        expect(count(html, /portal-branding-name/g)).toBe(1);
        expect(count(html, /Acme Ltd/g)).toBe(1);
        expect(html).toContain('<section class="portal-ticket-form"');
      });

      it('renders nothing, and leaves no space, when there is neither a name nor a logo', () => {
        const cases: ((client: QueryClient) => void)[] = [
          withBranding({ company_name: null, has_logo: false }),
          withBranding({ company_name: '   ', has_logo: false }),
          () => undefined, // the branding has not arrived
        ];
        for (const prepare of cases) {
          for (const path of ALL_PATHS) {
            for (const token of [true, false]) {
              const html = render({ path, language, token, prepare });
              expect(html, path).not.toContain('portal-branding');
              expect(html, path).not.toContain('<img');
              expect(count(html, /<h1[ >]/g), path).toBe(1);
            }
          }
        }
      });

      it('renders nothing when the branding says there is a logo but gives no name, until the logo exists', () => {
        const html = render({ path: '/', language, prepare: withBranding({ company_name: null, has_logo: true }) });
        expect(html).not.toContain('portal-branding');
      });
    });
  }

  it('shows the name, and no image yet, when the branding has a logo that is not loaded', () => {
    const html = render({
      path: '/',
      prepare: (client) => {
        withBranding({ company_name: 'Acme Ltd', has_logo: true })(client);
        client.setQueryData(publicBrandingLogoKey, new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' }));
      },
    });
    expect(html).toContain('Acme Ltd</p>');
    expect(html).not.toContain('<img');
  });

  it('shows it on the sign-in screen, above its heading', () => {
    for (const path of GATED_PATHS) {
      const html = render({ path, token: false, prepare: withBranding({ company_name: 'Acme Ltd', has_logo: false }) });
      expect(html, path).toContain(`<h1 tabindex="-1" class="portal-page-heading">${enService.portal.signIn.title}</h1>`);
      expect(count(html, /portal-branding-name/g), path).toBe(1);
      expect(html.indexOf('portal-branding'), path).toBeLessThan(html.indexOf('<h1'));
    }
  });

  it('shows it on the forced change-password screen, above its heading', () => {
    const html = render({
      path: '/',
      profile: { ...PROFILE, must_change_password: true },
      prepare: withBranding({ company_name: 'Acme Ltd', has_logo: false }),
    });
    expect(html).toContain(`<h1 tabindex="-1" class="portal-page-heading">${enService.portal.changePassword.title}</h1>`);
    expect(count(html, /portal-branding-name/g)).toBe(1);
    expect(html.indexOf('portal-branding')).toBeLessThan(html.indexOf('<h1'));
  });

  it('shows it on the accessibility statement and the not-found screen', () => {
    const accessibility = render({ path: '/accessibility', token: false, prepare: withBranding({ company_name: 'Acme Ltd', has_logo: false }) });
    expect(count(accessibility, /portal-branding-name/g)).toBe(1);
    const notFound = render({ path: '/nowhere', prepare: withBranding({ company_name: 'Acme Ltd', has_logo: false }) });
    expect(count(notFound, /portal-branding-name/g)).toBe(1);
  });

  it('does not take its content from the form configuration', () => {
    const html = render({
      path: '/',
      prepare: (client) => {
        client.setQueryData(formConfigKey, CONFIG({ company_name: 'Form Config Name', has_logo: true }));
      },
    });
    expect(html).not.toContain('portal-branding');
    expect(html).not.toContain('Form Config Name');
  });

  it('leaves every screen as it was when the branding request failed (401, 403, 429, 503, network)', async () => {
    const failures = [
      new PortalApiError(401, GENERIC_CODE),
      new PortalApiError(403, GENERIC_CODE),
      new PortalApiError(429, 'rate_limited', {}, '30'),
      new PortalApiError(503, 'photo_storage_unavailable'),
      new PortalApiError(0, 'network'),
    ];
    for (const failure of failures) {
      const state = await failedState(publicBrandingKey, () => Promise.reject(failure));
      for (const path of ALL_PATHS) {
        for (const token of [true, false]) {
          const html = render({ path, token, prepare: withState(publicBrandingKey, state) });
          expect(html, `${path} ${failure.status}`).not.toContain('portal-branding');
          expect(html, `${path} ${failure.status}`).not.toContain('<section role="alert"');
          expect(count(html, /<h1[ >]/g), path).toBe(1);
        }
      }
    }
  });

  it('shows the name only, and breaks nothing, when the logo request failed', async () => {
    for (const status of [401, 403, 404, 429, 503]) {
      const state = await withFetch(
        () => new Response('', { status }),
        () => failedState(publicBrandingLogoKey, () => getPublicBrandingLogo()),
      );
      for (const path of ALL_PATHS) {
        const html = render({
          path,
          token: false,
          prepare: (client) => {
            withBranding({ company_name: 'Acme Ltd', has_logo: true })(client);
            withState(publicBrandingLogoKey, state)(client);
          },
        });
        expect(html, `${path} ${status}`).toContain('<p class="portal-branding-name" dir="auto">Acme Ltd</p>');
        expect(html, `${path} ${status}`).not.toContain('<img');
        expect(count(html, /<h1[ >]/g), path).toBe(1);
      }
    }
  });

  it('keeps the queries under the portal root with the five minute cache and no retry', () => {
    expect(publicBrandingKey[0]).toBe('portal');
    expect(publicBrandingLogoKey[0]).toBe('portal');
    expect(PUBLIC_BRANDING_QUERY_OPTIONS).toEqual({ retry: false, refetchOnWindowFocus: false, staleTime: 300_000 });
  });
});

describe('the public branding requests', () => {
  const PNG = () => new Response(new Uint8Array([137, 80, 78, 71]), { status: 200, headers: { 'Content-Type': 'image/png' } });
  const headersOf = (init: RequestInit): Record<string, string> => init.headers as Record<string, string>;

  it('reads the name and the logo flag from /service/public/branding with no Authorization header', async () => {
    customerTokenStore.setToken('stored-token', Math.floor(Date.now() / 1000) + 3_600);
    await withFetch(
      () => new Response(JSON.stringify({ company_name: 'Acme', has_logo: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
      async (calls) => {
        await expect(getPublicBranding()).resolves.toEqual({ company_name: 'Acme', has_logo: true });
        const [url, init] = calls[0]!;
        expect(url).toBe('/api/service/public/branding');
        expect(init.credentials).toBe('omit');
        expect(Object.keys(headersOf(init)).map((name) => name.toLowerCase())).not.toContain('authorization');
      },
    );
  });

  it('reads the logo from /service/public/branding/logo as an image Blob with no Authorization header', async () => {
    customerTokenStore.setToken('stored-token', Math.floor(Date.now() / 1000) + 3_600);
    await withFetch(PNG, async (calls) => {
      const blob = await getPublicBrandingLogo();
      expect(blob.type).toBe('image/png');
      expect(blob.size).toBe(4);
      const [url, init] = calls[0]!;
      expect(url).toBe('/api/service/public/branding/logo');
      expect(init.credentials).toBe('omit');
      expect(Object.keys(headersOf(init)).map((name) => name.toLowerCase())).not.toContain('authorization');
    });
  });

  it('turns every failure into a PortalApiError and never signs the customer out (401, 403, 429, 503, network)', async () => {
    const token = 'stored-token';
    customerTokenStore.setToken(token, Math.floor(Date.now() / 1000) + 3_600);
    for (const status of [401, 403, 429, 503]) {
      for (const read of [() => getPublicBranding(), () => getPublicBrandingLogo()]) {
        const error: unknown = await withFetch(
          () => new Response(JSON.stringify({ detail: { error: { code: 'rate_limited' } } }), { status, headers: { 'Retry-After': '7' } }),
          () => read().catch((caught: unknown) => caught),
        );
        expect(error).toBeInstanceOf(PortalApiError);
        expect((error as PortalApiError).status).toBe(status);
        expect(customerTokenStore.getToken()).toBe(token);
        expect(customerTokenStore.getSessionExpired()).toBe(false);
      }
    }
    for (const read of [() => getPublicBranding(), () => getPublicBrandingLogo()]) {
      const error: unknown = await withFetch(
        () => Promise.reject(new TypeError('Failed to fetch')),
        () => read().catch((caught: unknown) => caught),
      );
      expect(error).toBeInstanceOf(PortalApiError);
      expect(error).toMatchObject({ status: 0, code: 'network' });
      expect(customerTokenStore.getToken()).toBe(token);
      expect(customerTokenStore.getSessionExpired()).toBe(false);
    }
  });

  it('rejects a logo body that is not an image or is empty, and a branding body that is not JSON', async () => {
    await withFetch(
      () => new Response('<html></html>', { status: 200, headers: { 'Content-Type': 'text/html' } }),
      async () => {
        await expect(getPublicBrandingLogo()).rejects.toMatchObject({ code: GENERIC_CODE });
        await expect(getPublicBranding()).rejects.toMatchObject({ code: GENERIC_CODE });
      },
    );
    await withFetch(
      () => new Response('', { status: 200, headers: { 'Content-Type': 'image/png' } }),
      async () => {
        await expect(getPublicBrandingLogo()).rejects.toMatchObject({ code: GENERIC_CODE });
      },
    );
  });

  it('reads a branding body with unusable members as no name and no logo', async () => {
    await withFetch(
      () => new Response(JSON.stringify({ company_name: '   ', has_logo: 'yes' }), { status: 200 }),
      async () => {
        await expect(getPublicBranding()).resolves.toEqual({ company_name: null, has_logo: false });
      },
    );
  });
});

describe('PortalBrandingView', () => {
  const renderView = (language: PortalLanguage, branding: FormBranding | undefined, logoUrl: string | null): string =>
    renderToStaticMarkup(
      createElement(
        I18nextProvider,
        { i18n: createPortalI18n(language) },
        createElement(PortalBrandingView, { branding, logoUrl }),
      ),
    );

  it('shows the logo with the company name as its alt text, and the name as plain text', () => {
    const html = renderView('en', { company_name: 'Acme <b>Ltd</b>', has_logo: true }, 'blob:http://x/1');
    expect(html).toContain('<img class="portal-branding-logo" src="blob:http://x/1" alt="Acme &lt;b&gt;Ltd&lt;/b&gt;"/>');
    expect(html).toContain('<p class="portal-branding-name" dir="auto">Acme &lt;b&gt;Ltd&lt;/b&gt;</p>');
    expect(html).not.toMatch(/<h[1-6]/);
  });

  it('shows a logo without a name with an empty alt text', () => {
    const html = renderView('he', { company_name: null, has_logo: true }, 'blob:http://x/1');
    expect(html).toContain('src="blob:http://x/1" alt=""');
    expect(html).not.toContain('portal-branding-name');
  });

  it('shows the name alone when the logo is not available', () => {
    const html = renderView('en', { company_name: 'Acme Ltd', has_logo: true }, null);
    expect(html).toContain('Acme Ltd</p>');
    expect(html).not.toContain('<img');
  });

  it('renders an empty string when there is nothing to show', () => {
    expect(renderView('he', undefined, null)).toBe('');
    expect(renderView('en', { company_name: null, has_logo: false }, 'blob:stale')).toBe('');
  });
});
