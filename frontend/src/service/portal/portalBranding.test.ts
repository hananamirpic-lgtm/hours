// Task 30.15 (R28.3, R28.4, R28.7): the company branding shows once above every signed-in portal screen,
// not on the sign-in screen, and a failed form configuration or logo request never changes a screen.
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
import { PortalApiError } from '../api/portalFetch';
import { brandingLogoKey, formConfigKey, getBrandingLogo } from '../api/ticketCreate';
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
const SIGNED_IN_PATHS = ['/', '/new', '/new/done', '/tickets/10001', '/nowhere'] as const;

const count = (html: string, pattern: RegExp): number => html.match(pattern)?.length ?? 0;

interface Options {
  path: string;
  language?: PortalLanguage;
  token?: boolean;
  profile?: CustomerProfile;
  /** Called with the client before the render, to put the form configuration and the logo into the cache. */
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

const withConfig = (branding: FormBranding) => (client: QueryClient) => {
  client.setQueryData(formConfigKey, CONFIG(branding));
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

afterEach(() => {
  customerTokenStore.clearToken();
});

describe('portal branding on the signed-in screens', () => {
  for (const language of ['he', 'en'] as const) {
    describe(language, () => {
      it('shows the company name once, above the page content, on every signed-in screen', () => {
        for (const path of SIGNED_IN_PATHS) {
          const html = render({ path, language, prepare: withConfig({ company_name: 'Acme Ltd', has_logo: false }) });
          expect(count(html, /class="portal-branding"/g), path).toBe(1);
          expect(html, path).toContain('<p class="portal-branding-name" dir="auto">Acme Ltd</p>');
          expect(html, path).not.toContain('<img');
          // No extra heading: the one h1 of the screen stays, and it comes after the branding.
          expect(count(html, /<h1[ >]/g), path).toBe(1);
          expect(html.indexOf('portal-branding'), path).toBeLessThan(html.indexOf('<h1'));
          // Inside <main>, so it is part of the page content and not of the header.
          expect(html.indexOf('<main'), path).toBeLessThan(html.indexOf('portal-branding'));
          expect(html.indexOf('portal-branding'), path).toBeLessThan(html.indexOf('</main>'));
        }
      });

      it('shows it once on /new, where the form frame used to show it', () => {
        const html = render({ path: '/new', language, prepare: withConfig({ company_name: 'Acme Ltd', has_logo: false }) });
        expect(count(html, /portal-branding-name/g)).toBe(1);
        expect(count(html, /Acme Ltd/g)).toBe(1);
        expect(html).toContain('<section class="portal-ticket-form"');
      });

      it('renders nothing, and leaves no space, when there is neither a name nor a logo', () => {
        const cases: ((client: QueryClient) => void)[] = [
          withConfig({ company_name: null, has_logo: false }),
          withConfig({ company_name: '   ', has_logo: false }),
          () => undefined, // the form configuration has not arrived
        ];
        for (const prepare of cases) {
          for (const path of SIGNED_IN_PATHS) {
            const html = render({ path, language, prepare });
            expect(html, path).not.toContain('portal-branding');
            expect(html, path).not.toContain('<img');
            expect(count(html, /<h1[ >]/g), path).toBe(1);
          }
        }
      });

      it('renders nothing when the configuration says there is a logo but gives no name, until the logo exists', () => {
        const html = render({ path: '/', language, prepare: withConfig({ company_name: null, has_logo: true }) });
        expect(html).not.toContain('portal-branding');
      });
    });
  }

  it('shows the name, and no image yet, when the configuration has a logo that is not loaded', () => {
    const html = render({
      path: '/',
      prepare: (client) => {
        withConfig({ company_name: 'Acme Ltd', has_logo: true })(client);
        client.setQueryData(brandingLogoKey, new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' }));
      },
    });
    expect(html).toContain('Acme Ltd</p>');
    expect(html).not.toContain('<img');
  });

  it('does not show it on the sign-in screen, even with the configuration in the cache', () => {
    for (const path of SIGNED_IN_PATHS) {
      const html = render({ path, token: false, prepare: withConfig({ company_name: 'Acme Ltd', has_logo: false }) });
      expect(html, path).toContain(`<h1 tabindex="-1" class="portal-page-heading">${enService.portal.signIn.title}</h1>`);
      expect(html, path).not.toContain('portal-branding');
      expect(html, path).not.toContain('Acme Ltd');
    }
  });

  it('does not show it on the forced change-password screen, which the form configuration does not answer', () => {
    const html = render({
      path: '/',
      profile: { ...PROFILE, must_change_password: true },
      prepare: withConfig({ company_name: 'Acme Ltd', has_logo: false }),
    });
    expect(html).toContain(`<h1 tabindex="-1" class="portal-page-heading">${enService.portal.changePassword.title}</h1>`);
    expect(html).not.toContain('portal-branding');
  });

  it('does not show it on the accessibility statement, which opens without a token', () => {
    const html = render({ path: '/accessibility', token: false, prepare: withConfig({ company_name: 'Acme Ltd', has_logo: false }) });
    expect(html).not.toContain('portal-branding');
  });

  it('leaves every screen as it was when the form configuration request failed', async () => {
    const state = await failedState(formConfigKey, () => Promise.reject(new PortalApiError(500, 'generic')));
    for (const path of ['/', '/tickets/10001', '/new/done', '/nowhere']) {
      const html = render({ path, prepare: withState(formConfigKey, state) });
      expect(html, path).not.toContain('portal-branding');
      expect(count(html, /<h1[ >]/g), path).toBe(1);
    }
    // The form reports its own load failure with its retry button, as before, and still shows no branding.
    const form = render({ path: '/new', prepare: withState(formConfigKey, state) });
    expect(form).toContain(enService.portal.form.loadError);
    expect(form).not.toContain('portal-branding');
  });

  it('shows the name only, and breaks nothing, when the logo request failed', async () => {
    const tokens = { getToken: () => 'customer-token', clearToken: () => undefined, expireSession: () => undefined };
    const original = globalThis.fetch;
    globalThis.fetch = (() => Promise.resolve(new Response('', { status: 404 }))) as typeof fetch;
    let state: QueryState<unknown, Error>;
    try {
      state = await failedState(brandingLogoKey, () => getBrandingLogo(undefined, tokens));
    } finally {
      globalThis.fetch = original;
    }
    for (const path of SIGNED_IN_PATHS) {
      const html = render({
        path,
        prepare: (client) => {
          withConfig({ company_name: 'Acme Ltd', has_logo: true })(client);
          withState(brandingLogoKey, state)(client);
        },
      });
      expect(html, path).toContain('<p class="portal-branding-name" dir="auto">Acme Ltd</p>');
      expect(html, path).not.toContain('<img');
      expect(count(html, /<h1[ >]/g), path).toBe(1);
    }
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
