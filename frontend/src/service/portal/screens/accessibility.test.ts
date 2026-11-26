import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { describe, expect, it } from 'vitest';
import en from '../../../i18n/locales/en/service.json';
import he from '../../../i18n/locales/he/service.json';
import { featuresContactKey } from '../../api/features';
import { PageHeading } from '../PageHeading';
import {
  PAGE_KEYS,
  documentTitle,
  isDialogOpen,
  isPageKey,
  pageForPath,
  pageViewKey,
  routePath,
  shouldMoveFocus,
  type Translate,
} from '../pageStructure';
import { applyDocumentLanguage, createPortalI18n, type PortalLanguage } from '../portalI18n';
import { AccessibilityStatementScreen } from './AccessibilityStatementScreen';

const LOCALES = { he, en } as const;
const LANGUAGES: PortalLanguage[] = ['he', 'en'];

function translator(language: PortalLanguage): Translate {
  const i18n = createPortalI18n(language);
  return (key, options) => String(options === undefined ? i18n.t(key) : i18n.t(key, options));
}

/** The expected title, built from the raw locale JSON without going through i18next. */
function expectedTitle(language: PortalLanguage, pageKey: string, number?: string): string {
  const titles = LOCALES[language].portal.pageTitle as Record<string, string>;
  const page = (titles[pageKey] ?? '').replace('{{number}}', number ?? '');
  return titles.template.replace('{{page}}', page);
}

describe('documentTitle', () => {
  for (const language of LANGUAGES) {
    it(`builds "<page> — <app>" for every page key in ${language}`, () => {
      const t = translator(language);
      for (const key of PAGE_KEYS) {
        const params = key === 'detail' ? { number: '1042' } : undefined;
        expect(documentTitle(key, params, t)).toBe(expectedTitle(language, key, params?.number));
      }
    });

    it(`puts the ticket number in the detail title in ${language}`, () => {
      expect(documentTitle('detail', { number: '1042' }, translator(language))).toContain('1042');
    });

    it(`falls back to the app name for an unknown key in ${language}`, () => {
      const t = translator(language);
      expect(documentTitle('no-such-page', undefined, t)).toBe(LOCALES[language].portal.appName);
      expect(documentTitle('', undefined, t)).toBe(LOCALES[language].portal.appName);
    });
  }

  it('ends with the app name in each language', () => {
    expect(documentTitle('list', undefined, translator('en'))).toBe('My tickets — Service tickets');
    expect(documentTitle('list', undefined, translator('he'))).toBe('הקריאות שלי — קריאות שירות');
  });

  it('has a title entry in both locale files for every page key', () => {
    for (const language of LANGUAGES) {
      const titles = LOCALES[language].portal.pageTitle as Record<string, string>;
      for (const key of PAGE_KEYS) expect(titles[key], `${language}:${key}`).toBeTruthy();
    }
  });

  it('recognises page keys', () => {
    expect(isPageKey('accessibility')).toBe(true);
    expect(isPageKey('portal.pageTitle.list')).toBe(false);
  });
});

describe('pageForPath', () => {
  it.each([
    ['/', 'list'],
    ['/new', 'newTicket'],
    ['/new/', 'newTicket'],
    ['/new/done', 'confirmation'],
    ['/accessibility', 'accessibility'],
    ['/service', 'list'],
    ['/service/', 'list'],
    ['/service/new', 'newTicket'],
    ['/service/accessibility?x=1#top', 'accessibility'],
    ['/nowhere', 'notFound'],
    ['/tickets', 'notFound'],
    ['/tickets/1/extra', 'notFound'],
    ['/services', 'notFound'],
  ])('maps %s to %s', (path, pageKey) => {
    expect(pageForPath(path).pageKey).toBe(pageKey);
  });

  it('maps a ticket path to the detail page with the number', () => {
    expect(pageForPath('/tickets/1042')).toEqual({ pageKey: 'detail', params: { number: '1042' } });
    expect(pageForPath('/service/tickets/1042')).toEqual({ pageKey: 'detail', params: { number: '1042' } });
    expect(pageForPath('/tickets/%E0%A4%A')).toEqual({ pageKey: 'detail', params: { number: '%E0%A4%A' } });
  });

  it('maps only keys that exist', () => {
    for (const path of ['/', '/new', '/new/done', '/tickets/7', '/accessibility', '/x']) {
      expect(isPageKey(pageForPath(path).pageKey)).toBe(true);
    }
  });

  it('normalises route paths', () => {
    expect(routePath('')).toBe('/');
    expect(routePath('/service')).toBe('/');
    expect(routePath('/service/new/')).toBe('/new');
    expect(routePath('new')).toBe('/new');
  });
});

describe('focus rule', () => {
  const a = pageViewKey('/', 'list');
  const b = pageViewKey('/new', 'newTicket');

  it('does not move focus on the first view', () => {
    expect(shouldMoveFocus(null, a, false)).toBe(false);
  });

  it('moves focus when the path or the screen changes', () => {
    expect(shouldMoveFocus(a, b, false)).toBe(true);
    expect(shouldMoveFocus(pageViewKey('/', 'signIn'), a, false)).toBe(true);
  });

  it('does not move focus when nothing changed (a background reload or a language change)', () => {
    expect(shouldMoveFocus(a, pageViewKey('/service/', 'list'), false)).toBe(false);
  });

  it('does not move focus while a dialog is open', () => {
    expect(shouldMoveFocus(a, b, true)).toBe(false);
  });

  it('detects an open dialog through the selector', () => {
    const seen: string[] = [];
    const root = {
      querySelector: (selector: string) => {
        seen.push(selector);
        return seen.length === 1 ? ({} as Element) : null;
      },
    };
    expect(isDialogOpen(root)).toBe(true);
    expect(isDialogOpen(root)).toBe(false);
    expect(seen[0]).toContain('[role="dialog"]');
    expect(seen[0]).toContain('dialog[open]');
  });
});

interface RenderOptions {
  language: PortalLanguage;
  /** `undefined` leaves the contact entry unset, as before the first `/api/features` answer. */
  contact?: string | null | undefined;
}

function renderInPortal(language: PortalLanguage, contact: string | null | undefined, element: ReturnType<typeof createElement>): string {
  const client = new QueryClient();
  if (contact !== undefined) client.setQueryData(featuresContactKey, contact);
  return renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n: createPortalI18n(language) },
      createElement(
        QueryClientProvider,
        { client },
        createElement(StaticRouter, { location: '/service/accessibility', basename: '/service' }, element),
      ),
    ),
  );
}

const renderStatement = (options: RenderOptions): string =>
  renderInPortal(options.language, options.contact, createElement(AccessibilityStatementScreen));

/** What React writes for a text value, so expected strings compare with the markup. */
const esc = (text: string): string =>
  text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#x27;');

const count = (html: string, pattern: RegExp): number => (html.match(pattern) ?? []).length;

describe('AccessibilityStatementScreen', () => {
  for (const language of LANGUAGES) {
    const strings = LOCALES[language].a11y.statement;

    describe(language, () => {
      const withContact = renderStatement({ language, contact: '03-5551234' });

      it('has exactly one h1 with the title, and h2 sections after it', () => {
        expect(count(withContact, /<h1[\s>]/g)).toBe(1);
        expect(withContact).toContain(`>${esc(strings.title)}</h1>`);
        expect(count(withContact, /<h2[\s>]/g)).toBe(4);
        expect(withContact.indexOf('<h1')).toBeLessThan(withContact.indexOf('<h2'));
        for (const title of [strings.targetTitle, strings.featuresTitle, strings.limitationsTitle, strings.contactTitle]) {
          expect(withContact).toContain(`>${esc(title)}</h2>`);
        }
        expect(withContact).not.toMatch(/<h[3-6][\s>]/);
      });

      it('states the IS 5568 level AA target', () => {
        expect(withContact).toContain(esc(strings.target));
        expect(strings.target).toContain('5568');
        expect(strings.target).toContain('AA');
      });

      it('lists the features and the known limitations', () => {
        expect(count(withContact, /<ul[\s>]/g)).toBe(2);
        for (const text of [strings.feature1, strings.feature2, strings.feature3, strings.feature4]) {
          expect(withContact).toContain(`<li>${esc(text)}</li>`);
        }
        for (const text of [strings.limitation1, strings.limitation2]) {
          expect(withContact).toContain(`<li>${esc(text)}</li>`);
        }
      });

      it('shows the contact from /api/features, isolated left-to-right', () => {
        const sentence = strings.contact.replace('{{contact}}', '\u206603-5551234\u2069');
        expect(withContact).toContain(esc(sentence));
        expect(withContact).not.toContain(esc(strings.contactMissing));
      });

      it('shows the contact-missing text when the contact is empty or not yet known', () => {
        for (const contact of [null, undefined]) {
          const html = renderStatement({ language, contact });
          expect(html).toContain(esc(strings.contactMissing));
          expect(html).not.toContain('03-5551234');
        }
      });

      it('shows the last-update date from the locale file', () => {
        expect(strings.lastUpdatedDate).not.toBe('');
        expect(withContact).toContain(esc(strings.lastUpdated.replace('{{date}}', `\u2066${strings.lastUpdatedDate}\u2069`)));
      });

      it('links to no screen of the Hours console', () => {
        const hrefs = [...withContact.matchAll(/href="([^"]*)"/g)].map((match) => match[1] ?? '');
        for (const href of hrefs) expect(href.startsWith('/service')).toBe(true);
        expect(withContact).not.toMatch(/<button/);
      });

      it('sets the document language and direction for the language', () => {
        const doc = { documentElement: { lang: '', dir: '' } } as unknown as Document;
        applyDocumentLanguage(language, doc);
        expect(doc.documentElement.lang).toBe(language);
        expect(doc.documentElement.dir).toBe(language === 'he' ? 'rtl' : 'ltr');
      });
    });
  }

  it('is written in each language, not in the other', () => {
    const hebrew = renderStatement({ language: 'he', contact: 'a@b.il' });
    const english = renderStatement({ language: 'en', contact: 'a@b.il' });
    expect(hebrew).toContain('ת&quot;י 5568');
    expect(english).toContain('IS 5568');
    expect(english).not.toMatch(/[\u0590-\u05ff]/);
  });

  it('does not use the session gate or any token', () => {
    // Rendered with no token provider at all: the screen needs only the flag-gate data and i18n.
    expect(() => renderStatement({ language: 'en', contact: null })).not.toThrow();
  });
});

describe('PageHeading', () => {
  for (const language of LANGUAGES) {
    it(`renders an h1 with tabindex -1 in ${language}`, () => {
      const html = renderInPortal(language, null, createElement(PageHeading, { pageKey: 'list', children: 'Heading text' }));
      expect(count(html, /<h1[\s>]/g)).toBe(1);
      expect(html).toMatch(/<h1[^>]*tabindex="-1"/);
      expect(html).toContain('>Heading text</h1>');
    });
  }
});
