// Tests of the branding header and the settlement selection window (task 30.8; R28.3, R28.4, R28.7,
// R30.2, R30.3, R30.5, R30.6, R30.8, R23.12): the pure model, the form configuration parsing, the logo
// request, the request body, and server-rendered markup in both languages. No DOM and no testing library:
// behavior that needs a browser (focus return, Escape, the focus trap, object URLs) is covered by the
// pure functions it calls and by `useFocusTrap`'s own tests and the Playwright suite of task 28.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Fragment, createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { describe, expect, it } from 'vitest';
import enService from '../../../i18n/locales/en/service.json';
import heService from '../../../i18n/locales/he/service.json';
import {
  normalizeFormConfig,
  parseBranding,
  parseLocalities,
  type FormCategory,
  type FormConfig,
  type FormLocality,
  type FormSettings,
} from '../../api/formTypes';
import { GENERIC_CODE, PortalApiError } from '../../api/portalFetch';
import { formConfigKey, getBrandingLogo } from '../../api/ticketCreate';
import { createPortalI18n, type PortalLanguage } from '../portalI18n';
import { portalKeys, type CustomerProfile } from '../session';
import { BrandingHeader } from './BrandingHeader';
import { SETTLEMENT_DIALOG_IDS, SETTLEMENT_VALUE_ID, SettlementControl, SettlementDialog } from './SettlementField';
import { TicketForm, TicketFormFrame, TicketFormScreen, type TicketFormProps } from './TicketFormScreen';
import {
  type BrandingView,
  type SettlementChoice,
  brandingView,
  filterSettlements,
  findSettlement,
  matchesText,
  normalizeSearch,
  placeText,
  prefillLocalityId,
  settlementChoice,
  settlementName,
} from './settlementModel';
import {
  buildFormData,
  categoryOptions,
  errorFieldOf,
  initialValues,
  resolveLabels,
  withoutFieldError,
  type FormValues,
} from './ticketFormModel';

const locales = { en: enService, he: heService };

// --- fixtures and helpers --------------------------------------------------------------------------

const SETTINGS: FormSettings = { description_required: false, photo_upload_enabled: true };
const CATEGORIES: FormCategory[] = [{ id: 'cat-1', name_he: 'אינסטלציה', name_en: 'Plumbing' }];
const LOCALITIES: FormLocality[] = [
  { id: 'l-haifa', name_he: 'חיפה', name_en: 'Haifa' },
  { id: 'l-hadera', name_he: 'חדרה', name_en: 'Hadera' },
  { id: 'l-akko', name_he: 'עכו', name_en: 'Acre' },
  { id: 'l-nahariya', name_he: 'נהריה', name_en: 'Nahariya' },
];
const CHOICE = { required: true, localities: LOCALITIES };
const ME: CustomerProfile = { full_name: 'Dana', phone: '0501234567', city: 'Haifa', apartment: '12', must_change_password: false };
const noop = (): void => undefined;

function i18nFor(language: PortalLanguage) {
  const instance = createPortalI18n(language);
  return instance;
}

function render(language: PortalLanguage, element: ReactElement): string {
  return renderToStaticMarkup(
    createElement(I18nextProvider, { i18n: i18nFor(language) }, createElement(StaticRouter, { location: '/new' }, element)),
  );
}

function fallbacksOf(language: PortalLanguage) {
  return { ...(language === 'he' ? heService : enService).fields };
}

const EMPTY: FormValues = initialValues({ city: null, apartment: null, phone: '0501234567' }, CHOICE);

function formProps(language: PortalLanguage, overrides: Partial<TicketFormProps> = {}): TicketFormProps {
  return {
    values: EMPTY,
    fieldErrors: {},
    notice: null,
    pending: false,
    categories: categoryOptions(CATEGORIES, language),
    labels: resolveLabels(undefined, language, fallbacksOf(language)),
    settings: SETTINGS,
    settlements: CHOICE,
    photo: null,
    photoInputKey: 0,
    onChange: noop,
    onPhotoChange: noop,
    onSubmit: noop,
    onFocusField: noop,
    ...overrides,
  };
}

function renderForm(language: PortalLanguage, overrides: Partial<TicketFormProps> = {}): string {
  return render(language, createElement(TicketFormFrame, { children: createElement(TicketForm, formProps(language, overrides)) }));
}

function renderControl(
  language: PortalLanguage,
  props: Partial<Parameters<typeof SettlementControl>[0]> = {},
): string {
  return render(
    language,
    createElement(SettlementControl, {
      localities: LOCALITIES,
      language,
      value: '',
      labelId: 'ticket-form-city-label',
      invalid: false,
      describedBy: undefined,
      onChange: noop,
      ...props,
    }),
  );
}

/** The opening tag of the element with this id. */
function tagWithId(html: string, id: string): string {
  const match = new RegExp(`<[a-z]+ [^>]*\\bid="${id}"[^>]*>`).exec(html);
  if (match === null) throw new Error(`no element with id ${id}`);
  return match[0];
}

function countOf(html: string, pattern: RegExp): number {
  return html.match(pattern)?.length ?? 0;
}

const texts = { he: heService, en: enService } as const;

// --- model: names and the place of a ticket --------------------------------------------------------

describe('settlementName and placeText', () => {
  it('names a settlement in the current language, falling back to the other when blank', () => {
    const haifa = LOCALITIES[0]!;
    expect(settlementName(haifa, 'en')).toBe('Haifa');
    expect(settlementName(haifa, 'he')).toBe('חיפה');
    expect(settlementName({ name_he: 'עכו', name_en: '  ' }, 'en')).toBe('עכו');
    expect(settlementName({ name_he: '', name_en: 'Acre' }, 'he')).toBe('Acre');
  });

  it('shows the settlement name when the response carries one, else the stored city text (R30.5)', () => {
    expect(placeText(LOCALITIES[0], 'חיפה', 'en')).toBe('Haifa');
    expect(placeText(LOCALITIES[0], 'חיפה', 'he')).toBe('חיפה');
    expect(placeText(null, 'Old Town', 'en')).toBe('Old Town');
    expect(placeText(undefined, 'Old Town', 'he')).toBe('Old Town');
    expect(placeText(undefined, null, 'en')).toBe('');
  });
});

// --- model: configuration --------------------------------------------------------------------------

describe('settlementChoice', () => {
  it('reads the list and the required flag from the configuration', () => {
    expect(settlementChoice({ localities: LOCALITIES, settlements_required: true })).toEqual({ required: true, localities: LOCALITIES });
  });
  it('treats missing members as no list and free text', () => {
    expect(settlementChoice({})).toEqual({ required: false, localities: [] });
    expect(settlementChoice({ settlements_required: false, localities: [] }).required).toBe(false);
  });
});

describe('form configuration parsing', () => {
  const base: FormConfig = { categories: CATEGORIES, labels: {}, settings: SETTINGS };

  it('tolerates an answer with no new members: empty list, free text, no branding', () => {
    const config = normalizeFormConfig(base);
    expect(config.localities).toEqual([]);
    expect(config.settlements_required).toBe(false);
    expect(config.branding).toEqual({ company_name: null, has_logo: false });
    expect(config.categories).toBe(CATEGORIES);
  });

  it('keeps the settlements, the flag and the branding of a full answer', () => {
    const config = normalizeFormConfig({
      ...base,
      localities: LOCALITIES,
      settlements_required: true,
      branding: { company_name: 'Acme', has_logo: true },
    });
    expect(config.localities).toEqual(LOCALITIES);
    expect(config.settlements_required).toBe(true);
    expect(config.branding).toEqual({ company_name: 'Acme', has_logo: true });
  });

  it('leaves out settlements that are not {id, name_he, name_en} strings', () => {
    expect(parseLocalities([LOCALITIES[0], null, 'x', { id: 'a' }, { id: 'b', name_he: 1, name_en: 'B' }])).toEqual([LOCALITIES[0]]);
    expect(parseLocalities('nope')).toEqual([]);
    expect(parseLocalities(undefined)).toEqual([]);
  });

  it('reads the branding strictly: a blank name is no name, only true is a logo', () => {
    expect(parseBranding({ company_name: '   ', has_logo: 'yes' })).toEqual({ company_name: null, has_logo: false });
    expect(parseBranding({ company_name: 'Acme' })).toEqual({ company_name: 'Acme', has_logo: false });
    expect(parseBranding(null)).toEqual({ company_name: null, has_logo: false });
    expect(parseBranding([])).toEqual({ company_name: null, has_logo: false });
  });

  it('requires settlements_required to be exactly true', () => {
    expect(normalizeFormConfig({ ...base, settlements_required: 'true' as unknown as boolean }).settlements_required).toBe(false);
  });
});

// --- model: prefill --------------------------------------------------------------------------------

describe('prefill (R30.6)', () => {
  it('preselects the customer settlement when it is in the active list', () => {
    expect(prefillLocalityId({ id: 'l-akko' }, LOCALITIES)).toBe('l-akko');
    const values = initialValues({ ...ME, locality: { id: 'l-akko' } }, CHOICE);
    expect(values.localityId).toBe('l-akko');
    expect(values.city).toBe('');
  });

  it('preselects nothing when the settlement is not in the list, or the customer has none', () => {
    expect(prefillLocalityId({ id: 'l-inactive' }, LOCALITIES)).toBe('');
    expect(prefillLocalityId(null, LOCALITIES)).toBe('');
    expect(prefillLocalityId(undefined, LOCALITIES)).toBe('');
    expect(initialValues({ ...ME, locality: { id: 'l-inactive' } }, CHOICE).localityId).toBe('');
    // A text city is not turned into a settlement.
    expect(initialValues(ME, CHOICE).localityId).toBe('');
  });

  it('keeps the free-text prefill and no settlement while the list is not active', () => {
    for (const settlements of [undefined, { required: false, localities: [] }]) {
      const values = initialValues({ ...ME, locality: { id: 'l-akko' } }, settlements);
      expect(values.city).toBe('Haifa');
      expect(values.localityId).toBeUndefined();
    }
  });

  it('finds a settlement by id and nothing for an empty or unknown id', () => {
    expect(findSettlement(LOCALITIES, 'l-hadera')?.name_en).toBe('Hadera');
    expect(findSettlement(LOCALITIES, '')).toBeNull();
    expect(findSettlement(LOCALITIES, 'nope')).toBeNull();
    expect(findSettlement(LOCALITIES, undefined)).toBeNull();
  });
});

// --- model: the search -----------------------------------------------------------------------------

describe('filterSettlements', () => {
  const ids = (list: readonly FormLocality[]): string[] => list.map((locality) => locality.id);

  it('matches a case-insensitive substring of the English name', () => {
    expect(ids(filterSettlements(LOCALITIES, 'ha'))).toEqual(['l-haifa', 'l-hadera', 'l-nahariya']);
    expect(ids(filterSettlements(LOCALITIES, 'HAI'))).toEqual(['l-haifa']);
    expect(ids(filterSettlements(LOCALITIES, 'ifa'))).toEqual(['l-haifa']);
  });

  it('matches a substring of the Hebrew name', () => {
    expect(ids(filterSettlements(LOCALITIES, 'חי'))).toEqual(['l-haifa']);
    expect(ids(filterSettlements(LOCALITIES, 'הרי'))).toEqual(['l-nahariya']);
  });

  it('matches either language whichever language the page is in', () => {
    // The Hebrew name is found while typing, and the English name too: the filter does not depend on the page language.
    expect(ids(filterSettlements(LOCALITIES, 'acre'))).toEqual(['l-akko']);
    expect(ids(filterSettlements(LOCALITIES, 'עכו'))).toEqual(['l-akko']);
  });

  it('ignores surrounding blanks, keeps the list for a blank search and the server order', () => {
    expect(ids(filterSettlements(LOCALITIES, '  hadera '))).toEqual(['l-hadera']);
    expect(ids(filterSettlements(LOCALITIES, ''))).toEqual(ids(LOCALITIES));
    expect(ids(filterSettlements(LOCALITIES, '   '))).toEqual(ids(LOCALITIES));
    expect(filterSettlements(LOCALITIES, '')).not.toBe(LOCALITIES);
  });

  it('is empty when nothing matches, and for an empty list', () => {
    expect(filterSettlements(LOCALITIES, 'zzz')).toEqual([]);
    expect(filterSettlements([], 'a')).toEqual([]);
  });

  it('treats the search as text, not as a pattern', () => {
    expect(filterSettlements(LOCALITIES, '.*')).toEqual([]);
    expect(filterSettlements(LOCALITIES, '(')).toEqual([]);
  });

  it('compares composed and decomposed forms alike', () => {
    expect(normalizeSearch('  Caf\u0065\u0301 ')).toBe('caf\u00e9');
  });

  it('chooses the live-region text by the number of matches', () => {
    expect(matchesText(0)).toEqual({ key: 'portal.settlementDialog.noMatches' });
    expect(matchesText(1)).toEqual({ key: 'portal.settlementDialog.matches', count: 1 });
    expect(matchesText(12)).toEqual({ key: 'portal.settlementDialog.matches', count: 12 });
  });
});

// --- model: the request body -----------------------------------------------------------------------

describe('buildFormData with settlements', () => {
  const values: FormValues = {
    city: 'typed text',
    localityId: 'l-haifa',
    apartment: '12',
    phone: '0501234567',
    categoryId: 'cat-1',
    description: '',
    urgent: false,
  };

  it('sends locality_id and no city part while the list is active', () => {
    const body = buildFormData(values, null, SETTINGS, true);
    expect(body.get('locality_id')).toBe('l-haifa');
    expect(body.has('city')).toBe(false);
    expect([...body.keys()].sort()).toEqual(['apartment', 'category_id', 'description', 'locality_id', 'phone', 'urgent']);
  });

  it('sends an empty locality_id when none is chosen, so the server answers invalid_settlement', () => {
    expect(buildFormData({ ...values, localityId: '' }, null, SETTINGS, true).get('locality_id')).toBe('');
    const without: FormValues = { city: 'x', apartment: '12', phone: '0501234567', categoryId: 'cat-1', description: '', urgent: false };
    expect(buildFormData(without, null, SETTINGS, true).get('locality_id')).toBe('');
  });

  it('sends the free-text city and no locality_id while the list is not active', () => {
    for (const body of [buildFormData(values, null, SETTINGS), buildFormData(values, null, SETTINGS, false)]) {
      expect(body.get('city')).toBe('typed text');
      expect(body.has('locality_id')).toBe(false);
    }
  });

  it('names the settlement error field city, and clears it when the settlement changes', () => {
    expect(errorFieldOf('localityId')).toBe('city');
    expect(withoutFieldError({ city: 'invalid_settlement', phone: 'invalid_phone' }, errorFieldOf('localityId'))).toEqual({
      phone: 'invalid_phone',
    });
  });
});

// --- model and API: the branding header ------------------------------------------------------------

describe('brandingView', () => {
  it('shows nothing when there is neither a name nor a logo', () => {
    expect(brandingView(undefined, null)).toBeNull();
    expect(brandingView({ company_name: null, has_logo: false }, null)).toBeNull();
    expect(brandingView({ company_name: '  ', has_logo: false }, 'blob:x')).toBeNull();
  });

  it('shows the name alone, with an empty alt for the logo that is not there', () => {
    expect(brandingView({ company_name: 'Acme', has_logo: false }, null)).toEqual({ name: 'Acme', logoUrl: null, logoAlt: 'Acme' });
  });

  it('shows the logo with the name as alt, or an empty alt when there is no name', () => {
    expect(brandingView({ company_name: 'Acme', has_logo: true }, 'blob:x')).toEqual({ name: 'Acme', logoUrl: 'blob:x', logoAlt: 'Acme' });
    expect(brandingView({ company_name: null, has_logo: true }, 'blob:x')).toEqual({ name: null, logoUrl: 'blob:x', logoAlt: '' });
  });

  it('shows the name only when the logo failed to load, and nothing when there was no name either', () => {
    expect(brandingView({ company_name: 'Acme', has_logo: true }, null)).toEqual({ name: 'Acme', logoUrl: null, logoAlt: 'Acme' });
    expect(brandingView({ company_name: null, has_logo: true }, null)).toBeNull();
  });

  it('never shows a logo URL the configuration says does not exist', () => {
    expect(brandingView({ company_name: 'Acme', has_logo: false }, 'blob:stale')?.logoUrl).toBeNull();
  });
});

describe('getBrandingLogo', () => {
  const tokens = { getToken: () => 'customer-token', clearToken: noop, expireSession: noop };

  async function withFetch<T>(answer: () => Response, run: (calls: [string, RequestInit][]) => Promise<T>): Promise<T> {
    const original = globalThis.fetch;
    const calls: [string, RequestInit][] = [];
    globalThis.fetch = ((url: string, init: RequestInit) => {
      calls.push([url, init]);
      return Promise.resolve(answer());
    }) as typeof fetch;
    try {
      return await run(calls);
    } finally {
      globalThis.fetch = original;
    }
  }

  it('fetches the PNG with the bearer header as a Blob', async () => {
    await withFetch(
      () => new Response(new Uint8Array([137, 80, 78, 71]), { status: 200, headers: { 'Content-Type': 'image/png' } }),
      async (calls) => {
        const blob = await getBrandingLogo(undefined, tokens);
        expect(blob.type).toBe('image/png');
        expect(blob.size).toBe(4);
        const [url, init] = calls[0]!;
        expect(url).toBe('/api/service/branding/logo');
        expect((init.headers as Record<string, string>).Authorization).toBe('Bearer customer-token');
      },
    );
  });

  it('rejects a body that is not an image, and a 404, without throwing anything else', async () => {
    await withFetch(
      () => new Response('<html></html>', { status: 200, headers: { 'Content-Type': 'text/html' } }),
      async () => {
        await expect(getBrandingLogo(undefined, tokens)).rejects.toMatchObject({ code: GENERIC_CODE });
      },
    );
    await withFetch(
      () => new Response('', { status: 404 }),
      async () => {
        const error: unknown = await getBrandingLogo(undefined, tokens).catch((caught: unknown) => caught);
        expect(error).toBeInstanceOf(PortalApiError);
        expect((error as PortalApiError).status).toBe(404);
      },
    );
  });
});

// --- locale keys -----------------------------------------------------------------------------------

describe('locale keys of the settlement choice', () => {
  const used = [
    'portal.form.settlementPlaceholder',
    'portal.settlementDialog.title',
    'portal.settlementDialog.searchLabel',
    'portal.settlementDialog.noMatches',
    'portal.settlementDialog.cancel',
    'errors.invalid_settlement',
  ];

  it('has every text in both languages, and the Hebrew term for a settlement', () => {
    for (const language of ['he', 'en'] as const) {
      const instance = i18nFor(language);
      for (const key of used) expect(instance.exists(key), `${language} ${key}`).toBe(true);
    }
    expect(locales.he.portal.form.settlementPlaceholder).toContain('ישוב');
    expect(locales.he.portal.settlementDialog.title).toContain('ישוב');
  });

  it('counts the matches with a plural form per language', () => {
    const en = i18nFor('en');
    expect(en.t('portal.settlementDialog.matches', { count: 1 })).toBe('1 settlement found.');
    expect(en.t('portal.settlementDialog.matches', { count: 4 })).toBe('4 settlements found.');
    const he = i18nFor('he');
    expect(he.t('portal.settlementDialog.matches', { count: 1 })).toBe('נמצא ישוב אחד.');
    expect(he.t('portal.settlementDialog.matches', { count: 2 })).toBe('נמצאו שני ישובים.');
    expect(he.t('portal.settlementDialog.matches', { count: 7 })).toBe('נמצאו 7 ישובים.');
  });
});

// --- markup: the branding header -------------------------------------------------------------------

describe('branding header markup', () => {
  // The branding is no longer part of the form frame: PortalShell renders it above the routed screen.
  // This is that arrangement: the branding, then the screen's own frame with its one h1.
  function frame(language: PortalLanguage, branding: BrandingView | null): string {
    return render(
      language,
      createElement(
        Fragment,
        null,
        createElement(BrandingHeader, { view: branding }),
        createElement(TicketFormFrame, { children: null }),
      ),
    );
  }

  for (const language of ['he', 'en'] as const) {
    const dir = language === 'he' ? 'rtl' : 'ltr';

    describe(language, () => {
      it('shows the name as plain text above the one h1', () => {
        const html = frame(language, brandingView({ company_name: 'Acme Ltd', has_logo: false }, null));
        expect(html).toContain('<p class="portal-branding-name" dir="auto">Acme Ltd</p>');
        expect(html).not.toContain('<img');
        expect(countOf(html, /<h1[ >]/g)).toBe(1);
        expect(html.indexOf('portal-branding')).toBeLessThan(html.indexOf('<h1'));
        expect(html).toMatch(/^<div class="portal-branding">/);
        expect(html).toContain(`</div><section class="portal-ticket-form" lang="${language}" dir="${dir}"><h1 `);
      });

      it('shows the logo with the name as its alt text', () => {
        const html = frame(language, brandingView({ company_name: 'Acme Ltd', has_logo: true }, 'blob:http://x/1'));
        expect(html).toContain('<img class="portal-branding-logo" src="blob:http://x/1" alt="Acme Ltd"/>');
        expect(html).toContain('Acme Ltd</p>');
        expect(html.indexOf('<img')).toBeLessThan(html.indexOf('<h1'));
        expect(countOf(html, /<h1[ >]/g)).toBe(1);
      });

      it('shows a logo with no name with an empty alt text and no name element', () => {
        const html = frame(language, brandingView({ company_name: null, has_logo: true }, 'blob:http://x/1'));
        expect(html).toContain('src="blob:http://x/1" alt=""');
        expect(html).not.toContain('portal-branding-name');
      });

      it('renders nothing at all when there is neither a name nor a logo', () => {
        for (const branding of [null, brandingView({ company_name: null, has_logo: false }, null), brandingView(undefined, null)]) {
          const html = frame(language, branding);
          expect(html).not.toContain('portal-branding');
          expect(html).not.toContain('<img');
          expect(html).toContain(`dir="${dir}"><h1 `);
        }
        expect(render(language, createElement(BrandingHeader, { view: null }))).toBe('');
      });

      it('shows markup typed into the name literally', () => {
        const html = frame(language, brandingView({ company_name: '<b>Acme</b> & Co', has_logo: false }, null));
        expect(html).not.toContain('<b>');
        expect(html).toContain('&lt;b&gt;Acme&lt;/b&gt; &amp; Co');
      });
    });
  }
});

// --- markup: the control ---------------------------------------------------------------------------

describe('settlement control markup', () => {
  for (const language of ['he', 'en'] as const) {
    const placeholder = locales[language].portal.form.settlementPlaceholder;

    describe(language, () => {
      it('is a button with the placeholder while nothing is chosen, named by the label and the value', () => {
        const html = renderControl(language);
        const tag = tagWithId(html, 'ticket-form-city');
        expect(tag).toMatch(/^<button /);
        expect(tag).toContain('type="button"');
        expect(tag).toContain('aria-haspopup="dialog"');
        expect(tag).toContain(`aria-labelledby="ticket-form-city-label ${SETTLEMENT_VALUE_ID}"`);
        expect(tag).not.toContain('aria-invalid');
        expect(tag).not.toContain('aria-describedby');
        expect(html).toContain(`>${placeholder}</span>`);
        expect(html).toContain('settlement-control-placeholder');
        expect(html).not.toContain('role="dialog"');
      });

      it('shows the chosen settlement as text in the current language', () => {
        const html = renderControl(language, { value: 'l-akko' });
        const name = language === 'he' ? 'עכו' : 'Acre';
        expect(html).toContain(`>${name}</span>`);
        expect(html).not.toContain(placeholder);
        expect(html).toContain('settlement-control-value');
      });

      it('re-labels the chosen settlement by id when the language changes', () => {
        expect(renderControl('en', { value: 'l-haifa' })).toContain('>Haifa</span>');
        expect(renderControl('he', { value: 'l-haifa' })).toContain('>חיפה</span>');
      });

      it('shows the placeholder for an id that is not in the list', () => {
        expect(renderControl(language, { value: 'l-gone' })).toContain(`>${placeholder}</span>`);
      });

      it('carries aria-invalid and aria-describedby when the field has an error', () => {
        const tag = tagWithId(renderControl(language, { invalid: true, describedBy: 'ticket-form-city-error' }), 'ticket-form-city');
        expect(tag).toContain('aria-invalid="true"');
        expect(tag).toContain('aria-describedby="ticket-form-city-error"');
      });

    });
  }
});

// --- markup: the dialog ----------------------------------------------------------------------------

describe('settlement dialog markup', () => {
  function renderDialog(language: PortalLanguage, props: Partial<Parameters<typeof SettlementDialog>[0]> = {}): string {
    return render(
      language,
      createElement(SettlementDialog, {
        localities: LOCALITIES,
        language,
        selectedId: '',
        onChoose: noop,
        onCancel: noop,
        ...props,
      }),
    );
  }

  for (const language of ['he', 'en'] as const) {
    const keys = locales[language].portal.settlementDialog;
    const names = LOCALITIES.map((locality) => settlementName(locality, language));

    describe(language, () => {
      it('is a labelled modal dialog with a titled heading', () => {
        const html = renderDialog(language);
        const tag = /<div [^>]*role="dialog"[^>]*>/.exec(html)?.[0] ?? '';
        expect(tag).toContain('aria-modal="true"');
        expect(tag).toContain(`aria-labelledby="${SETTLEMENT_DIALOG_IDS.title}"`);
        expect(tag).toContain('tabindex="-1"');
        expect(html).toContain(`<h2 id="${SETTLEMENT_DIALOG_IDS.title}">${keys.title}</h2>`);
        // The page heading stays the only h1: the dialog title is an h2.
        expect(html).not.toContain('<h1');
      });

      it('has a labelled search box', () => {
        const html = renderDialog(language);
        expect(html).toContain(`<label for="${SETTLEMENT_DIALOG_IDS.search}">${keys.searchLabel}</label>`);
        const tag = tagWithId(html, SETTLEMENT_DIALOG_IDS.search);
        expect(tag).toContain('type="search"');
        expect(tag).toContain('value=""');
        expect(tag).toContain(`aria-describedby="${SETTLEMENT_DIALOG_IDS.status}"`);
        expect(tag).toContain('autoComplete="off"');
      });

      it('lists every settlement as an option button in the current language, in order', () => {
        const html = renderDialog(language);
        expect(html).toContain(`<ul id="${SETTLEMENT_DIALOG_IDS.list}" class="settlement-options" role="listbox" aria-labelledby="${SETTLEMENT_DIALOG_IDS.title}">`);
        expect(countOf(html, /<button type="button" role="option"/g)).toBe(LOCALITIES.length);
        const positions = names.map((name) => html.indexOf(`>${name}</button>`));
        expect(positions.every((position) => position > 0)).toBe(true);
        expect([...positions].sort((a, b) => a - b)).toEqual(positions);
        expect(countOf(html, /aria-selected="false"/g)).toBe(LOCALITIES.length);
        expect(html).not.toContain('aria-selected="true"');
      });

      it('marks the current choice with aria-selected', () => {
        const html = renderDialog(language, { selectedId: 'l-akko' });
        expect(countOf(html, /aria-selected="true"/g)).toBe(1);
        const selected = /<button [^>]*aria-selected="true"[^>]*>([^<]*)<\/button>/.exec(html);
        expect(selected?.[1]).toBe(settlementName(LOCALITIES[2]!, language));
      });

      it('announces the number of matches in a polite live region', () => {
        const html = renderDialog(language);
        const tag = tagWithId(html, SETTLEMENT_DIALOG_IDS.status);
        expect(tag).toContain('role="status"');
        expect(tag).toContain('aria-live="polite"');
        expect(html).toContain(String(i18nFor(language).t('portal.settlementDialog.matches', { count: 4 })));
        const filtered = renderDialog(language, { initialQuery: 'hai' });
        expect(filtered).toContain(String(i18nFor(language).t('portal.settlementDialog.matches', { count: 1 })));
      });

      it('filters the list by the search text, in either language, case-insensitively', () => {
        const byEnglish = renderDialog(language, { initialQuery: 'HA' });
        expect(countOf(byEnglish, /role="option"/g)).toBe(3);
        expect(byEnglish).toContain('value="HA"');
        expect(byEnglish).not.toContain(`>${settlementName(LOCALITIES[2]!, language)}</button>`);
        const byHebrew = renderDialog(language, { initialQuery: 'עכ' });
        expect(countOf(byHebrew, /role="option"/g)).toBe(1);
        expect(byHebrew).toContain(`>${settlementName(LOCALITIES[2]!, language)}</button>`);
      });

      it('says that no settlements match and renders no list', () => {
        const html = renderDialog(language, { initialQuery: 'zzz' });
        expect(html).toContain(`>${keys.noMatches}</p>`);
        expect(html).not.toContain('role="listbox"');
        expect(html).not.toContain('role="option"');
        expect(tagWithId(html, SETTLEMENT_DIALOG_IDS.status)).toContain('role="status"');
      });

      it('has a Cancel button and every control is a button of type button', () => {
        const html = renderDialog(language);
        expect(html).toContain(`<button type="button" id="${SETTLEMENT_DIALOG_IDS.cancel}" class="portal-button portal-button--quiet">${keys.cancel}</button>`);
        expect(countOf(html, /<button /g)).toBe(LOCALITIES.length + 1);
        expect(countOf(html, /<button type="button"/g)).toBe(LOCALITIES.length + 1);
        expect(html).not.toContain('type="submit"');
      });

      it('shows the options as 44 px targets through the portal button class', () => {
        expect(renderDialog(language)).toContain('class="portal-button portal-button--quiet settlement-option"');
      });
    });
  }

  it('renders when the list is empty: no list, the no-match text', () => {
    const html = renderDialog('en', { localities: [] });
    expect(html).toContain('No settlements match.');
    expect(html).not.toContain('role="listbox"');
  });

  it('escapes markup in a settlement name', () => {
    const html = renderDialog('en', { localities: [{ id: 'x', name_he: '<i>x</i>', name_en: '<b>y</b>' }] });
    expect(html).not.toContain('<b>y</b>');
    expect(html).toContain('&lt;b&gt;y&lt;/b&gt;');
  });
});

describe('settlement control with the dialog open', () => {
  for (const language of ['he', 'en'] as const) {
    it(`${language}: shows the control and the dialog together, with the current choice selected`, () => {
      const html = renderControl(language, { initialOpen: true, value: 'l-hadera' });
      expect(html).toContain('role="dialog"');
      expect(html).toContain('aria-selected="true"');
      expect(tagWithId(html, 'ticket-form-city')).toContain('aria-haspopup="dialog"');
      expect(html).toContain(`>${settlementName(LOCALITIES[1]!, language)}</span>`);
    });
  }
});

// --- markup: the form ------------------------------------------------------------------------------

describe('TicketForm with the settlement list', () => {
  for (const language of ['he', 'en'] as const) {
    const t = texts[language];

    describe(language, () => {
      it('replaces the City text input by the control, labelled and marked required', () => {
        const html = renderForm(language);
        expect(html).not.toMatch(/<input [^>]*id="ticket-form-city"/);
        expect(html).not.toContain('name="city"');
        expect(tagWithId(html, 'ticket-form-city')).toMatch(/^<button /);
        expect(html).toContain(
          `<label for="ticket-form-city" id="ticket-form-city-label">${t.fields.city}<span class="portal-field-mark"> ${t.a11y.required}</span></label>`,
        );
        expect(html).toContain(`>${locales[language].portal.form.settlementPlaceholder}</span>`);
      });

      it('shows the chosen settlement and keeps the other values', () => {
        const values: FormValues = { ...EMPTY, localityId: 'l-nahariya', apartment: '4', description: 'drip' };
        const html = renderForm(language, { values });
        expect(html).toContain(`>${settlementName(LOCALITIES[3]!, language)}</span>`);
        expect(tagWithId(html, 'ticket-form-apartment')).toContain('value="4"');
        expect(html).toContain('>drip</textarea>');
      });

      it('shows the invalid_settlement error beside the control and in the summary', () => {
        const html = renderForm(language, { fieldErrors: { city: 'invalid_settlement' } });
        const tag = tagWithId(html, 'ticket-form-city');
        expect(tag).toContain('aria-invalid="true"');
        expect(tag).toContain('aria-describedby="ticket-form-city-error"');
        expect(html).toContain(
          `<p id="ticket-form-city-error" class="portal-error">${t.a11y.errorPrefix} ${t.errors.invalid_settlement}</p>`,
        );
        expect(html).toContain('href="#ticket-form-city"');
        expect(html).toContain(t.portal.form.errorSummary);
      });

      it('marks no error when there is none', () => {
        const tag = tagWithId(renderForm(language), 'ticket-form-city');
        expect(tag).not.toContain('aria-invalid');
        expect(tag).not.toContain('aria-describedby');
      });

      it('keeps the one h1 and renders the control before the apartment field', () => {
        const html = renderForm(language);
        expect(countOf(html, /<h1[ >]/g)).toBe(1);
        expect(html.indexOf('id="ticket-form-city"')).toBeLessThan(html.indexOf('id="ticket-form-apartment"'));
      });
    });
  }

  it('keeps the free-text input exactly as before while the list is not active', () => {
    // (A form with no `settlements` prop at all is covered by ticketForm.test.ts.)
    const cases: SettlementChoice[] = [
      { required: false, localities: [] },
      // A list that is present but not required (the server never sends it, but the form must not act on it).
      { required: false, localities: LOCALITIES },
    ];
    for (const settlements of cases) {
      const html = renderForm('en', { values: initialValues(ME, settlements), settlements });
      const tag = tagWithId(html, 'ticket-form-city');
      expect(tag).toMatch(/^<input /);
      expect(tag).toContain('name="city"');
      expect(tag).toContain('aria-required="true"');
      expect(tag).toContain('value="Haifa"');
      expect(html).toContain('<label for="ticket-form-city">');
      expect(html).not.toContain('settlement-control');
      expect(html).not.toContain('ticket-form-city-label');
    }
  });
});

// --- markup: the screen with a filled cache --------------------------------------------------------

describe('TicketFormScreen with branding and settlements', () => {
  const config: FormConfig = {
    categories: CATEGORIES,
    labels: {},
    settings: SETTINGS,
    localities: LOCALITIES,
    settlements_required: true,
    branding: { company_name: 'Acme Ltd', has_logo: false },
  };

  /** An answer from before settlements and branding existed. */
  const bare: FormConfig = { categories: CATEGORIES, labels: {}, settings: SETTINGS };

  function renderScreen(language: PortalLanguage, data: { config?: FormConfig; me?: CustomerProfile }): string {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    if (data.config) client.setQueryData(formConfigKey, data.config);
    if (data.me) client.setQueryData(portalKeys.me, data.me);
    return renderToStaticMarkup(
      createElement(
        I18nextProvider,
        { i18n: i18nFor(language) },
        createElement(
          QueryClientProvider,
          { client },
          createElement(StaticRouter, { location: '/new' }, createElement(TicketFormScreen)),
        ),
      ),
    );
  }

  it('shows the settlement control, with nothing preselected for a text city, and no branding of its own', () => {
    const html = renderScreen('en', { config, me: ME });
    // The branding is the layout's (task 30.15): the screen alone must not show it, or /new would show it twice.
    expect(html).not.toContain('portal-branding');
    expect(html).not.toContain('Acme Ltd');
    expect(countOf(html, /<h1[ >]/g)).toBe(1);
    expect(tagWithId(html, 'ticket-form-city')).toMatch(/^<button /);
    expect(html).toContain('>Choose a settlement</span>');
  });

  it('preselects the settlement of the customer when it is in the list, in the current language', () => {
    const me: CustomerProfile = { ...ME, locality: { id: 'l-akko', name_he: 'עכו', name_en: 'Acre' } };
    expect(renderScreen('en', { config, me })).toContain('>Acre</span>');
    expect(renderScreen('he', { config, me })).toContain('>עכו</span>');
  });

  it('preselects nothing when the settlement of the customer is not in the active list', () => {
    const me: CustomerProfile = { ...ME, locality: { id: 'l-inactive', name_he: 'לוד', name_en: 'Lod' } };
    const html = renderScreen('en', { config, me });
    expect(html).toContain('>Choose a settlement</span>');
    expect(html).not.toContain('>Lod</span>');
  });

  it('renders no branding element for an answer without branding or with neither member', () => {
    const withoutBranding: FormConfig = { ...bare, localities: LOCALITIES, settlements_required: true };
    for (const cached of [normalizeFormConfig(withoutBranding), { ...config, branding: { company_name: null, has_logo: false } }]) {
      const html = renderScreen('he', { config: cached, me: ME });
      expect(html).not.toContain('portal-branding');
      expect(countOf(html, /<h1[ >]/g)).toBe(1);
    }
  });

  it('keeps the free-text field when the answer has no settlement members', () => {
    const html = renderScreen('en', { config: normalizeFormConfig(bare), me: ME });
    expect(tagWithId(html, 'ticket-form-city')).toMatch(/^<input /);
    expect(tagWithId(html, 'ticket-form-city')).toContain('value="Haifa"');
    expect(html).not.toContain('portal-branding');
  });
});
