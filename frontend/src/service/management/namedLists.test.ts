// Tests of the settlement and replaceable-parts screens (task 30.7): the pure view model, and server-rendered
// markup of the table, the form, the delete confirmation and the two pages with a filled cache, in both languages.
// No DOM, no testing library: the containers that hold queries and mutations are rendered only up to their first
// paint; everything they decide is a pure function of `namedListModel`.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { describe, expect, it } from 'vitest';
import { ERROR_CODES } from '../api/errorCodes';
import { staffListKeys } from '../api/staffLists';
import type { NamedListKind, StaffNamedItem } from '../api/staffListsTypes';
import { createPortalI18n, type PortalLanguage } from '../portal/portalI18n';
import {
  EMPTY_NAMED_VALUES,
  NAMED_LISTS,
  NAME_FIELD_ORDER,
  NO_NAMED_ERRORS,
  availableNamedActions,
  configLanguage,
  errorTextKey,
  firstFailingNameField,
  isNamedItemGone,
  isolateName,
  listText,
  namedActionTexts,
  namedDeleteResultKey,
  namedFormErrors,
  namedFormValues,
  namedItemDisplayName,
  namedStateKey,
  toNamedBody,
  withoutNamedError,
  type NamedFormErrors,
} from './namedListModel';
import { NamedListDeleteView, namedDeleteIds } from './namedListDelete';
import { NAMED_FORM_FIELD_ORDER, NamedListFormView, namedFormIds, namedInputId } from './namedListForm';
import { NamedListTable } from './namedListTable';
import { LocalitiesPage } from './pages/LocalitiesPage';
import { PartsPage } from './pages/PartsPage';
import en from '@/i18n/locales/en/service.json';
import he from '@/i18n/locales/he/service.json';

const locales = { en, he };

const noop = (): void => undefined;

const KINDS: readonly NamedListKind[] = ['localities', 'parts'];
const LANGUAGES: readonly PortalLanguage[] = ['en', 'he'];

const haifa: StaffNamedItem = { id: 's-1', name_he: 'חיפה', name_en: 'Haifa', is_active: true };
const eilat: StaffNamedItem = { id: 's-2', name_he: 'אילת', name_en: 'Eilat', is_active: false };

// --- render helpers ----------------------------------------------------------------------------------

function render(language: PortalLanguage, element: ReactElement, prime?: (client: QueryClient) => void): string {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  prime?.(queryClient);
  const i18n = createPortalI18n(language);
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(
        I18nextProvider,
        { i18n },
        createElement(StaticRouter, { location: '/tickets/settings' }, element),
      ),
    ),
  );
}

/** No text key may reach the screen unresolved. */
function expectNoRawKeys(html: string): void {
  expect(html).not.toMatch(/\bmanagement\.[a-zA-Z]/);
  expect(html).not.toMatch(/\berrors\.[a-z_]+/);
  expect(html).not.toMatch(/\ba11y\.[a-zA-Z]/);
}

function count(html: string, pattern: RegExp): number {
  return html.match(pattern)?.length ?? 0;
}

// --- the model -----------------------------------------------------------------------------------------

describe('the two lists', () => {
  it('have their own text prefix, element ids, not-found code and route', () => {
    expect(NAMED_LISTS.localities).toMatchObject({
      textPrefix: 'management.settings.localities',
      idPrefix: 'config-localities',
      notFoundCode: 'settlement_not_found',
      path: '/tickets/settings/localities',
    });
    expect(NAMED_LISTS.parts).toMatchObject({
      textPrefix: 'management.settings.parts',
      idPrefix: 'config-parts',
      notFoundCode: 'part_not_found',
      path: '/tickets/settings/parts',
    });
    expect(listText('parts', 'title')).toBe('management.settings.parts.title');
  });

  it('recognise only their own not-found code as "the item is gone"', () => {
    expect(isNamedItemGone('localities', 'settlement_not_found')).toBe(true);
    expect(isNamedItemGone('localities', 'part_not_found')).toBe(false);
    expect(isNamedItemGone('parts', 'part_not_found')).toBe(true);
    expect(isNamedItemGone('parts', 'duplicate_part')).toBe(false);
  });

  it('name an item in the language of the screen and reuse the shared language helpers', () => {
    expect(namedItemDisplayName(haifa, 'he')).toBe('חיפה');
    expect(namedItemDisplayName(haifa, 'en')).toBe('Haifa');
    expect(configLanguage('he-IL')).toBe('he');
    expect(configLanguage('fr')).toBe('en');
    expect(isolateName('Haifa')).toBe('\u2068Haifa\u2069');
    expect(errorTextKey('duplicate_settlement')).toBe('errors.duplicate_settlement');
    expect(errorTextKey('network')).toBe('errors.generic');
  });
});

describe('the form model (R30.1, R29.6)', () => {
  it('sends the names as typed, so the server does the trimming and the length check', () => {
    expect(toNamedBody({ nameHe: '  חיפה ', nameEn: ' Haifa ' })).toEqual({ name_he: '  חיפה ', name_en: ' Haifa ' });
    expect(namedFormValues(haifa)).toEqual({ nameHe: 'חיפה', nameEn: 'Haifa' });
    expect(EMPTY_NAMED_VALUES).toEqual({ nameHe: '', nameEn: '' });
  });

  it.each([
    ['invalid_settlement_name_he', 'invalid_settlement_name_en'],
    ['invalid_part_name_he', 'invalid_part_name_en'],
  ])('shows each failing name of a 422 (%s, %s) on its own field and nothing at form level', (he, en) => {
    const errors = namedFormErrors(he, { name_he: he, name_en: en });
    expect(errors).toEqual({ fields: { name_he: `errors.${he}`, name_en: `errors.${en}` }, form: null });
    expect(firstFailingNameField(errors)).toBe('name_he');
  });

  it.each(['duplicate_settlement', 'duplicate_part'])('puts %s on the clashing language only', (code) => {
    const errors = namedFormErrors(code, { name_en: code });
    expect(errors).toEqual({ fields: { name_en: `errors.${code}` }, form: null });
    expect(firstFailingNameField(errors)).toBe('name_en');
  });

  it('gives a refusal that names no known field to the whole form', () => {
    expect(namedFormErrors('duplicate_settlement')).toEqual({ fields: {}, form: 'errors.duplicate_settlement' });
    expect(namedFormErrors('part_not_found')).toEqual({ fields: {}, form: 'errors.part_not_found' });
    expect(namedFormErrors('generic', { other: 'invalid_part_name_he' })).toEqual({ fields: {}, form: 'errors.generic' });
    expect(firstFailingNameField(NO_NAMED_ERRORS)).toBeNull();
  });

  it('clears the message of the name being edited and keeps the other', () => {
    const errors = namedFormErrors('x', { name_he: 'invalid_part_name_he', name_en: 'invalid_part_name_en' });
    const after = withoutNamedError(errors, 'name_he');
    expect(after.fields).toEqual({ name_en: 'errors.invalid_part_name_en' });
    expect(withoutNamedError(after, 'name_he')).toBe(after);
  });

  it('keeps the form order of the model and of the markup together', () => {
    expect(NAMED_FORM_FIELD_ORDER).toEqual(NAME_FIELD_ORDER);
  });
});

describe('the row actions (R30.1, R29.6)', () => {
  it('offer deactivate and delete for an active item, reactivate and delete for an inactive one', () => {
    expect(availableNamedActions(haifa)).toEqual(['deactivate', 'delete']);
    expect(availableNamedActions(eilat)).toEqual(['reactivate', 'delete']);
  });

  it('have no "last active" rule: the actions depend on the item alone, never on how many are active', () => {
    // The only active item of a list still offers both actions: the function takes the item and nothing else.
    expect(availableNamedActions({ is_active: true })).toEqual(['deactivate', 'delete']);
  });

  it.each(KINDS)('name the label and the success message of each action of %s; delete has its two results', (kind) => {
    expect(namedActionTexts(kind, 'deactivate')).toEqual({
      labelKey: `management.settings.${kind}.deactivate`,
      doneKey: `management.settings.${kind}.deactivateDone`,
    });
    expect(namedActionTexts(kind, 'reactivate').doneKey).toBe(`management.settings.${kind}.reactivateDone`);
    expect(namedActionTexts(kind, 'delete').doneKey).toBeNull();
    expect(namedDeleteResultKey(kind, 'removed')).toBe(`management.settings.${kind}.removed`);
    expect(namedDeleteResultKey(kind, 'deactivated')).toBe(`management.settings.${kind}.deactivated`);
  });

  it('show the state as text', () => {
    expect(namedStateKey('localities', haifa)).toBe('management.settings.localities.active');
    expect(namedStateKey('parts', eilat)).toBe('management.settings.parts.inactive');
  });
});

// --- markup: the form ----------------------------------------------------------------------------------

describe('NamedListFormView', () => {
  const view = (kind: NamedListKind, errors: NamedFormErrors, pending = false): ReactElement =>
    createElement(NamedListFormView, {
      kind,
      values: { nameHe: 'חיפה', nameEn: 'Haifa' },
      errors,
      pending,
      onChange: noop,
      onSubmit: noop,
      onCancel: noop,
    });

  it.each(KINDS.flatMap((kind) => LANGUAGES.map((language) => [kind, language] as const)))(
    'renders two labelled required names with the right direction (%s, %s)',
    (kind, language) => {
      const html = render(language, view(kind, NO_NAMED_ERRORS));
      expectNoRawKeys(html);
      for (const field of NAME_FIELD_ORDER) {
        expect(html).toContain(`id="${namedInputId(kind, field)}"`);
        expect(html).toContain(`for="${namedInputId(kind, field)}"`);
      }
      expect(count(html, /aria-required="true"/g)).toBe(2);
      expect(html).toContain('lang="he"');
      expect(html).toContain('dir="rtl"');
      expect(html).toContain('lang="en"');
      expect(html).toContain('dir="ltr"');
      expect(html).not.toContain('aria-invalid="true"');
      expect(html).toContain(`id="${namedFormIds(kind).form}"`);
    },
  );

  it('uses ids of its own list, so the two screens never share an id', () => {
    expect(namedInputId('localities', 'name_he')).toBe('config-localities-name-he');
    expect(namedInputId('parts', 'name_en')).toBe('config-parts-name-en');
    expect(namedFormIds('localities').title).not.toBe(namedFormIds('parts').title);
  });

  it('states the length rule of each list in the hint', () => {
    expect(render('en', view('localities', NO_NAMED_ERRORS))).toContain('1 to 100 characters');
    expect(render('en', view('parts', NO_NAMED_ERRORS))).toContain('1 to 60 characters');
    expect(render('he', view('parts', NO_NAMED_ERRORS))).toContain('1 עד 60 תווים');
  });

  it('marks the clashing name invalid and ties the message to it (duplicate_settlement)', () => {
    const html = render('en', view('localities', namedFormErrors('duplicate_settlement', { name_en: 'duplicate_settlement' })));
    expect(count(html, /aria-invalid="true"/g)).toBe(1);
    expect(html).toContain('A settlement with this name already exists.');
    expect(html).toContain(`id="${namedInputId('localities', 'name_en')}-error"`);
    expect(html).toMatch(
      new RegExp(`aria-describedby="${namedInputId('localities', 'name_en')}-hint ${namedInputId('localities', 'name_en')}-error"`),
    );
  });

  it('marks every failing name of a 422 for a part, with the text of its own code', () => {
    const html = render(
      'he',
      view('parts', namedFormErrors('invalid_part_name_he', { name_he: 'invalid_part_name_he', name_en: 'invalid_part_name_en' })),
    );
    expect(count(html, /aria-invalid="true"/g)).toBe(2);
    expect(html).toContain(`id="${namedInputId('parts', 'name_he')}-error"`);
    expect(html).toContain(`id="${namedInputId('parts', 'name_en')}-error"`);
    expectNoRawKeys(html);
  });

  it('shows a refusal of the whole form as an alert, and disables the controls while pending', () => {
    const refused = render('en', view('parts', namedFormErrors('part_not_found')));
    expect(refused).toContain('role="alert"');
    expect(refused).toContain('The part was not found.');
    const pending = render('he', view('localities', NO_NAMED_ERRORS, true));
    expect(count(pending, /disabled=""/g)).toBe(4);
  });
});

// --- markup: the delete confirmation -------------------------------------------------------------------

describe('NamedListDeleteView', () => {
  const view = (kind: NamedListKind, extra: Partial<Parameters<typeof NamedListDeleteView>[0]> = {}): ReactElement =>
    createElement(NamedListDeleteView, {
      kind,
      name: 'Old',
      pending: false,
      error: null,
      gone: false,
      onConfirm: noop,
      onDismiss: noop,
      ...extra,
    });

  it.each(KINDS.flatMap((kind) => LANGUAGES.map((language) => [kind, language] as const)))(
    'names the item and the deactivate-instead rule (%s, %s)',
    (kind, language) => {
      const html = render(language, view(kind));
      expectNoRawKeys(html);
      expect(html).toContain('\u2068Old\u2069');
      expect(html).toContain(`id="${namedDeleteIds(kind).confirm}"`);
      expect(html).toContain(`id="${namedDeleteIds(kind).dismiss}"`);
      expect(html).not.toContain('role="alert"');
    },
  );

  it('says what delete does for each list (English)', () => {
    expect(render('en', view('localities'))).toContain('If tickets or residents use it, it is deactivated instead of deleted.');
    expect(render('en', view('parts'))).toContain('If tickets use it, it is deactivated instead of deleted.');
  });

  it('shows a refusal as an alert and keeps Delete; leaves only Close once the item is gone', () => {
    const refused = render('en', view('parts', { error: 'errors.generic' }));
    expect(refused).toContain('role="alert"');
    expect(refused).toContain(`id="${namedDeleteIds('parts').confirm}"`);
    const gone = render('en', view('localities', { error: 'errors.settlement_not_found', gone: true }));
    expect(gone).toContain('The settlement was not found.');
    expect(gone).not.toContain(namedDeleteIds('localities').confirm);
    expect(gone).toContain('>Close<');
  });
});

// --- markup: the table ---------------------------------------------------------------------------------

describe('NamedListTable', () => {
  const table = (kind: NamedListKind, busy = false): ReactElement =>
    createElement(NamedListTable, { kind, rows: [haifa, eilat], language: 'en', busy, onEdit: noop, onAction: noop });

  it.each(KINDS.flatMap((kind) => LANGUAGES.map((language) => [kind, language] as const)))(
    'lists both names, the state as text and the actions of each state (%s, %s)',
    (kind, language) => {
      const html = render(language, table(kind));
      expectNoRawKeys(html);
      expect(html).toContain('חיפה');
      expect(html).toContain('Haifa');
      expect(html).toContain('אילת');
      expect(count(html, /<tr/g)).toBe(3);
      // Edit and delete on both rows, deactivate on the active row, reactivate on the inactive one.
      expect(count(html, /<button/g)).toBe(6);
    },
  );

  it('names the item in every row button and offers the right toggle (English)', () => {
    const html = render('en', table('localities'));
    expect(html).toContain('aria-label="Edit: Haifa"');
    expect(html).toContain('aria-label="Deactivate: Haifa"');
    expect(html).toContain('aria-label="Delete: Haifa"');
    expect(html).toContain('aria-label="Reactivate: Eilat"');
    expect(html).not.toContain('aria-label="Reactivate: Haifa"');
    expect(html).not.toContain('aria-label="Deactivate: Eilat"');
    expect(html).toContain('>Active<');
    expect(html).toContain('>Inactive<');
  });

  it('shows the Hebrew state words in Hebrew and the caption of the list', () => {
    const html = render('he', table('parts'));
    expect(html).toContain('>פעיל<');
    expect(html).toContain('>לא פעיל<');
    expect(html).toContain('<caption class="config-sr-only">חלקים להחלפה</caption>');
  });

  it('marks busy buttons with aria-disabled instead of disabling them, so focus stays', () => {
    const html = render('en', table('parts', true));
    expect(count(html, /aria-disabled="true"/g)).toBe(6);
    expect(html).not.toContain('disabled=""');
  });
});

// --- the pages -----------------------------------------------------------------------------------------

describe.each([
  ['localities', LocalitiesPage, { en: 'Settlements', he: 'ישובים' }, { en: 'There are no settlements.', he: 'אין ישובים.' }],
  ['parts', PartsPage, { en: 'Replaceable parts', he: 'חלקים להחלפה' }, { en: 'There are no parts.', he: 'אין חלקים.' }],
] as const)('the %s page', (kind, Page, titles, empty) => {
  const prime = (items: readonly StaffNamedItem[] | null) => (client: QueryClient) => {
    if (items !== null) client.setQueryData<readonly StaffNamedItem[]>(staffListKeys[kind], items);
  };

  it.each(LANGUAGES)('shows the heading, the add button and the rows from the cache (%s)', (language) => {
    const html = render(language, createElement(Page), prime([haifa, eilat]));
    expectNoRawKeys(html);
    expect(html).toContain('<h1');
    expect(html).toContain(titles[language]);
    expect(html).toContain('Haifa');
    expect(html).toContain('אילת');
    expect(html).toContain('role="status"');
    expect(html).not.toContain('role="dialog"');
    expect(html).toContain('<table');
  });

  it('says it is loading before the list arrives, with no table', () => {
    const html = render('en', createElement(Page), prime(null));
    expect(html).toContain('Loading…');
    expect(html).not.toContain('<table');
  });

  it.each(LANGUAGES)('shows the empty text for an empty list (%s)', (language) => {
    const html = render(language, createElement(Page), prime([]));
    expect(html).toContain(empty[language]);
    expect(html).not.toContain('<table');
  });
});

// --- texts ---------------------------------------------------------------------------------------------

describe('texts', () => {
  it('has a text for every error code the two lists can refuse with, in both languages', () => {
    const codes = [
      'invalid_settlement_name_he',
      'invalid_settlement_name_en',
      'duplicate_settlement',
      'settlement_not_found',
      'invalid_part_name_he',
      'invalid_part_name_en',
      'duplicate_part',
      'part_not_found',
      'insufficient_role',
    ];
    for (const code of codes) expect(ERROR_CODES).toContain(code);
    for (const language of LANGUAGES) {
      const i18n = createPortalI18n(language);
      for (const code of codes) expect(i18n.t(`errors.${code}`, { ns: 'service' })).not.toBe(`errors.${code}`);
    }
  });

  it('has every key the screens of both lists use, in both languages, with the same key paths', () => {
    const paths = (value: unknown, prefix = ''): string[] =>
      typeof value === 'object' && value !== null
        ? Object.entries(value).flatMap(([name, child]) => paths(child, prefix === '' ? name : `${prefix}.${name}`))
        : [prefix];
    expect(paths(locales.he.management.settings).sort()).toEqual(paths(locales.en.management.settings).sort());
    for (const language of LANGUAGES) {
      const i18n = createPortalI18n(language);
      for (const kind of KINDS) {
        for (const name of [
          'title', 'subtitle', 'add', 'nameHe', 'nameEn', 'nameHint', 'state', 'active', 'inactive', 'created', 'updated',
          'deactivate', 'reactivate', 'delete', 'deleteTitle', 'deletePrompt', 'removed', 'deactivated', 'empty',
          'loadError', 'createTitle', 'editTitle', 'rowAction', 'rowError', 'working', 'deactivateDone', 'reactivateDone',
        ]) {
          const key = listText(kind, name);
          expect(i18n.t(key, { ns: 'service' }), `${language} ${key}`).not.toBe(key);
        }
      }
    }
  });

  it('names the three pages and their nav entries as the task says', () => {
    expect(locales.en.management.nav).toMatchObject({ localities: 'Settlements', parts: 'Replaceable parts', branding: 'Branding' });
    expect(locales.he.management.nav).toMatchObject({
      localities: 'ישובים',
      parts: 'חלקים להחלפה',
      branding: 'לוגו ושם החברה',
    });
  });
});

// --- the routes ----------------------------------------------------------------------------------------

describe('the routes', () => {
  const app = readFileSync(fileURLToPath(new URL('../../App.tsx', import.meta.url)), 'utf8');

  it.each([
    ['settings/localities', 'LocalitiesPage'],
    ['settings/parts', 'PartsPage'],
    ['settings/branding', 'BrandingPage'],
  ])('declares %s behind RequireConfigStaff', (path, page) => {
    const route = new RegExp(
      `path="${path}"\\s+element=\\{\\s*<RequireConfigStaff>\\s*<${page} />\\s*</RequireConfigStaff>`,
    );
    expect(app).toMatch(route);
  });
});
