// Tests of the configuration screens (task 24.7): the pure view model, and server-rendered markup of the category
// table and form, the delete confirmation, the label editor, the option form and the three pages with a filled
// cache. No DOM, no testing library: the containers that hold queries and mutations are rendered only up to
// their first paint; everything they decide is a pure function of `configModel`.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { describe, expect, it } from 'vitest';
import { staffKeys } from '../api/staff';
import { ERROR_CODES } from '../api/errorCodes';
import type { FormSettings, StaffCategory, StaffLabels } from '../api/staffTypes';
import { createPortalI18n, type PortalLanguage } from '../portal/portalI18n';
import { CategoriesTable } from './configCategoriesTable';
import { CategoryDeleteView } from './configCategoryDelete';
import { CategoryFormView, NAME_FIELD_ORDER, categoryInputId } from './configCategoryForm';
import { LabelsFormView, labelInputId } from './configLabelsView';
import {
  CATEGORY_FIELD_ORDER,
  CONFIG_LANGUAGES,
  EMPTY_CATEGORY_VALUES,
  LABEL_FIELDS,
  NO_CATEGORY_ERRORS,
  NO_LABEL_ERRORS,
  OPTION_NAMES,
  availableCategoryActions,
  categoryActionTexts,
  categoryDisplayName,
  categoryFormErrors,
  categoryFormValues,
  categoryStateKey,
  changedLabels,
  configLanguage,
  deleteResultKey,
  errorTextKey,
  firstFailingCategoryField,
  firstFailingLabel,
  isCategoryGone,
  isolateName,
  labelDrafts,
  labelErrorKey,
  labelFieldNameKey,
  labelFormErrors,
  parseLabelErrorKey,
  toCategoryBody,
  withLabelText,
  withOption,
  withoutLabelError,
  type CategoryFormErrors,
  type LabelFormErrors,
} from './configModel';
import { OPTION_ORDER, OptionsFormView, optionInputId } from './configOptionsView';
import { CategoriesPage } from './pages/CategoriesPage';
import { LabelsPage } from './pages/LabelsPage';
import { OptionsPage } from './pages/OptionsPage';

const noop = (): void => undefined;

const leak: StaffCategory = { id: 'k-1', name_he: 'דליפה / סתימה', name_en: 'Leak or Blockage', is_active: true };
const old: StaffCategory = { id: 'k-2', name_he: 'ישן', name_en: 'Old', is_active: false };

const LABELS: StaffLabels = {
  city: { he: 'עיר', en: 'City' },
  apartment: { he: 'מספר דירה', en: 'Apartment number' },
  phone: { he: 'מספר טלפון', en: 'Phone number' },
  category: { he: 'קטגוריית תקלה', en: 'Issue category' },
  description: { he: 'תיאור התקלה', en: 'Issue description' },
  photo: { he: 'תמונה', en: 'Photo' },
  urgent: { he: 'דחוף', en: 'Urgent' },
};

const SETTINGS: FormSettings = { description_required: true, photo_upload_enabled: false };

// --- render helpers ----------------------------------------------------------------------------------

function i18nFor(language: PortalLanguage) {
  const instance = createPortalI18n(language);
  return instance;
}

function render(language: PortalLanguage, element: ReactElement, prime?: (client: QueryClient) => void): string {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  prime?.(queryClient);
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(
        I18nextProvider,
        { i18n: i18nFor(language) },
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
  expect(html).not.toMatch(/\bfields\.[a-z]+\b/);
}

const LANGUAGES: readonly PortalLanguage[] = ['en', 'he'];

function count(html: string, pattern: RegExp): number {
  return html.match(pattern)?.length ?? 0;
}

// --- shared helpers ------------------------------------------------------------------------------------

describe('shared helpers', () => {
  it('reads the screen language from an i18next tag, English for anything else', () => {
    expect(configLanguage('he')).toBe('he');
    expect(configLanguage('he-IL')).toBe('he');
    expect(configLanguage('en')).toBe('en');
    expect(configLanguage('fr')).toBe('en');
    expect(configLanguage(undefined)).toBe('en');
  });

  it('names a category in the language of the screen', () => {
    expect(categoryDisplayName(leak, 'he')).toBe('דליפה / סתימה');
    expect(categoryDisplayName(leak, 'en')).toBe('Leak or Blockage');
  });

  it('maps a code to its text key, and an unknown code to the generic one', () => {
    expect(errorTextKey('duplicate_category')).toBe('errors.duplicate_category');
    expect(errorTextKey('last_active_category')).toBe('errors.last_active_category');
    expect(errorTextKey('network')).toBe('errors.generic');
    expect(errorTextKey(null)).toBe('errors.generic');
  });

  it('wraps a name in first-strong isolates and recognises a missing category', () => {
    expect(isolateName('Old')).toBe('\u2068Old\u2069');
    expect(isCategoryGone('category_not_found')).toBe(true);
    expect(isCategoryGone('duplicate_category')).toBe(false);
  });
});

// --- categories ----------------------------------------------------------------------------------------

describe('category form model (R19.5, R19.6, R19.7)', () => {
  it('sends the names as typed, so the server does the trimming and the length check', () => {
    expect(toCategoryBody({ nameHe: '  רטיבות ', nameEn: ' Dampness ' })).toEqual({
      name_he: '  רטיבות ',
      name_en: ' Dampness ',
    });
    expect(categoryFormValues(leak)).toEqual({ nameHe: 'דליפה / סתימה', nameEn: 'Leak or Blockage' });
  });

  it('shows each failing name of a 422 on its own field, and nothing at form level', () => {
    const errors = categoryFormErrors('invalid_name_he', { name_he: 'invalid_name_he', name_en: 'invalid_name_en' });
    expect(errors).toEqual({
      fields: { name_he: 'errors.invalid_name_he', name_en: 'errors.invalid_name_en' },
      form: null,
    });
    expect(firstFailingCategoryField(errors)).toBe('name_he');
  });

  it('puts duplicate_category on the clashing language only (R19.7)', () => {
    const errors = categoryFormErrors('duplicate_category', { name_en: 'duplicate_category' });
    expect(errors).toEqual({ fields: { name_en: 'errors.duplicate_category' }, form: null });
    expect(firstFailingCategoryField(errors)).toBe('name_en');
  });

  it('gives a refusal that names no known field to the whole form', () => {
    expect(categoryFormErrors('duplicate_category')).toEqual({ fields: {}, form: 'errors.duplicate_category' });
    expect(categoryFormErrors('category_not_found')).toEqual({ fields: {}, form: 'errors.category_not_found' });
    expect(categoryFormErrors('generic', { other: 'invalid_name_he' })).toEqual({
      fields: {},
      form: 'errors.generic',
    });
    expect(firstFailingCategoryField(NO_CATEGORY_ERRORS)).toBeNull();
  });

  it('keeps the form order of the model and of the markup together', () => {
    expect(NAME_FIELD_ORDER).toEqual(CATEGORY_FIELD_ORDER);
  });
});

describe('category row actions (R19.9 to R19.12)', () => {
  it('offers deactivate and delete for an active category, reactivate and delete for an inactive one', () => {
    expect(availableCategoryActions(leak)).toEqual(['deactivate', 'delete']);
    expect(availableCategoryActions(old)).toEqual(['reactivate', 'delete']);
  });

  it('names the label and the success message of each action; delete has its two results instead', () => {
    expect(categoryActionTexts('deactivate').doneKey).toBe('management.settings.categories.deactivateDone');
    expect(categoryActionTexts('reactivate').doneKey).toBe('management.settings.categories.reactivateDone');
    expect(categoryActionTexts('delete').doneKey).toBeNull();
    expect(deleteResultKey('removed')).toBe('management.settings.categories.removed');
    expect(deleteResultKey('deactivated')).toBe('management.settings.categories.deactivated');
  });

  it('shows the state as text', () => {
    expect(categoryStateKey(leak)).toBe('management.settings.categories.active');
    expect(categoryStateKey(old)).toBe('management.settings.categories.inactive');
  });
});

// --- labels --------------------------------------------------------------------------------------------

describe('label model (R19.4, R19.5)', () => {
  it('covers the seven fields of the form in order', () => {
    expect(LABEL_FIELDS).toEqual(['city', 'apartment', 'phone', 'category', 'description', 'photo', 'urgent']);
    expect(labelFieldNameKey('apartment')).toBe('fields.apartment');
  });

  it('builds drafts for every field, empty where the service holds no label', () => {
    const drafts = labelDrafts({ city: { he: 'עיר', en: 'City' } });
    expect(Object.keys(drafts)).toEqual([...LABEL_FIELDS]);
    expect(drafts.city).toEqual({ he: 'עיר', en: 'City' });
    expect(drafts.urgent).toEqual({ he: '', en: '' });
  });

  it('sends only the fields whose text changed, as typed', () => {
    const stored = labelDrafts(LABELS);
    expect(changedLabels(stored, stored)).toEqual({});
    const edited = withLabelText(withLabelText(stored, 'photo', 'en', ' Picture '), 'city', 'he', 'יישוב');
    expect(changedLabels(edited, stored)).toEqual({
      city: { he: 'יישוב', en: 'City' },
      photo: { he: 'תמונה', en: ' Picture ' },
    });
    expect(stored.photo.en).toBe('Photo');
  });

  it.each([
    ['city.label_he', 'city.he'],
    ['urgent.label_en', 'urgent.en'],
    ['phone.he', 'phone.he'],
    ['description.en', 'description.en'],
  ])('reads the failing text %s as %s', (name, expected) => {
    expect(parseLabelErrorKey(name)).toBe(expected);
  });

  it.each(['city', 'nonsense.label_he', 'city.label_fr', '.label_he', 'city.label_', ''])(
    'does not read %j as a label text',
    (name) => {
      expect(parseLabelErrorKey(name)).toBeNull();
    },
  );

  it('shows every failing text of a 422 on its own input and puts the first in form order first', () => {
    const errors = labelFormErrors('invalid_label_he', {
      'urgent.label_en': 'invalid_label_en',
      'city.label_en': 'invalid_label_en',
      'photo.label_he': 'invalid_label_he',
    });
    expect(errors.form).toBeNull();
    expect(errors.fields).toEqual({
      'urgent.en': 'errors.invalid_label_en',
      'city.en': 'errors.invalid_label_en',
      'photo.he': 'errors.invalid_label_he',
    });
    expect(firstFailingLabel(errors)).toEqual({ key: 'city', language: 'en' });
  });

  it('gives unknown_field, which names a bare field, to the whole form', () => {
    expect(labelFormErrors('unknown_field', { bogus: 'unknown_field' })).toEqual({
      fields: {},
      form: 'errors.unknown_field',
    });
    expect(firstFailingLabel(NO_LABEL_ERRORS)).toBeNull();
  });

  it('clears the message of the text being edited and keeps the others', () => {
    const errors = labelFormErrors('invalid_label_he', {
      'city.label_he': 'invalid_label_he',
      'city.label_en': 'invalid_label_en',
    });
    const after = withoutLabelError(errors, 'city', 'he');
    expect(after.fields).toEqual({ 'city.en': 'errors.invalid_label_en' });
    expect(withoutLabelError(after, 'urgent', 'he')).toBe(after);
    expect(labelErrorKey('urgent', 'he')).toBe('urgent.he');
  });
});

// --- options -------------------------------------------------------------------------------------------

describe('option model (R19.2, R19.3)', () => {
  it('has the two options in the order the markup shows them', () => {
    expect(OPTION_NAMES).toEqual(['description_required', 'photo_upload_enabled']);
    expect(OPTION_ORDER).toEqual(OPTION_NAMES);
  });

  it('changes one option and leaves the other', () => {
    expect(withOption(SETTINGS, 'photo_upload_enabled', true)).toEqual({
      description_required: true,
      photo_upload_enabled: true,
    });
    expect(withOption(SETTINGS, 'description_required', false)).toEqual({
      description_required: false,
      photo_upload_enabled: false,
    });
    expect(SETTINGS.description_required).toBe(true);
  });
});

// --- markup: categories ----------------------------------------------------------------------------------

describe('CategoryFormView', () => {
  const view = (errors: CategoryFormErrors, pending = false): ReactElement =>
    createElement(CategoryFormView, {
      values: { nameHe: 'דליפה', nameEn: 'Leak' },
      errors,
      pending,
      onChange: noop,
      onSubmit: noop,
      onCancel: noop,
    });

  it.each(LANGUAGES)('renders two labelled required names with the right direction (%s)', (language) => {
    const html = render(language, view(NO_CATEGORY_ERRORS));
    expectNoRawKeys(html);
    for (const field of CATEGORY_FIELD_ORDER) {
      expect(html).toContain(`id="${categoryInputId(field)}"`);
      expect(html).toContain(`for="${categoryInputId(field)}"`);
    }
    expect(count(html, /aria-required="true"/g)).toBe(2);
    expect(html).toContain('lang="he"');
    expect(html).toContain('dir="rtl"');
    expect(html).toContain('lang="en"');
    expect(html).toContain('dir="ltr"');
    expect(html).not.toContain('aria-invalid="true"');
  });

  it('marks the clashing name invalid and ties the message to it (R19.7)', () => {
    const html = render('en', view(categoryFormErrors('duplicate_category', { name_en: 'duplicate_category' })));
    expect(html).toContain(`aria-invalid="true"`);
    expect(count(html, /aria-invalid="true"/g)).toBe(1);
    expect(html).toContain('A category with this name already exists.');
    expect(html).toContain(`id="${categoryInputId('name_en')}-error"`);
    expect(html).toMatch(new RegExp(`aria-describedby="${categoryInputId('name_en')}-hint ${categoryInputId('name_en')}-error"`));
  });

  it('shows a refusal of the whole form as an alert, and disables the controls while pending', () => {
    const refused = render('en', view(categoryFormErrors('category_not_found')));
    expect(refused).toContain('role="alert"');
    expect(refused).toContain('The category was not found.');
    const pending = render('he', view(NO_CATEGORY_ERRORS, true));
    expect(count(pending, /disabled=""/g)).toBe(4);
  });

  it('starts empty for a new category', () => {
    expect(EMPTY_CATEGORY_VALUES).toEqual({ nameHe: '', nameEn: '' });
  });
});

describe('CategoryDeleteView', () => {
  it.each(LANGUAGES)('names the category and the deactivate-instead rule (%s)', (language) => {
    const html = render(
      language,
      createElement(CategoryDeleteView, { name: 'Old', pending: false, error: null, gone: false, onConfirm: noop, onDismiss: noop }),
    );
    expectNoRawKeys(html);
    expect(html).toContain('\u2068Old\u2069');
    expect(html).toContain('id="config-delete-yes"');
    expect(html).toContain('id="config-delete-no"');
    expect(html).not.toContain('role="alert"');
  });

  it('shows last_active_category as an alert and keeps the dialog open (R19.13)', () => {
    const html = render(
      'en',
      createElement(CategoryDeleteView, {
        name: 'Old',
        pending: false,
        error: 'errors.last_active_category',
        gone: false,
        onConfirm: noop,
        onDismiss: noop,
      }),
    );
    expect(html).toContain('role="alert"');
    expect(html).toContain('The last active category cannot be deactivated or deleted.');
    expect(html).toContain('id="config-delete-yes"');
  });

  it('leaves only Close once the category is gone', () => {
    const html = render(
      'en',
      createElement(CategoryDeleteView, {
        name: 'Old',
        pending: false,
        error: 'errors.category_not_found',
        gone: true,
        onConfirm: noop,
        onDismiss: noop,
      }),
    );
    expect(html).not.toContain('config-delete-yes');
    expect(html).toContain('>Close<');
  });
});

describe('CategoriesTable', () => {
  const table = (busy = false): ReactElement =>
    createElement(CategoriesTable, { rows: [leak, old], language: 'en', busy, onEdit: noop, onAction: noop });

  it.each(LANGUAGES)('lists both names, the state as text and the actions of each state (%s)', (language) => {
    const html = render(language, table());
    expectNoRawKeys(html);
    expect(html).toContain('דליפה / סתימה');
    expect(html).toContain('Leak or Blockage');
    expect(html).toContain('ישן');
    expect(count(html, /<tr/g)).toBe(3);
    // Edit and delete on both rows, deactivate on the active row, reactivate on the inactive one.
    expect(count(html, /<button/g)).toBe(6);
  });

  it('names the category in every row button and offers the right toggle (English)', () => {
    const html = render('en', table());
    expect(html).toContain('aria-label="Edit: Leak or Blockage"');
    expect(html).toContain('aria-label="Deactivate: Leak or Blockage"');
    expect(html).toContain('aria-label="Delete: Leak or Blockage"');
    expect(html).toContain('aria-label="Reactivate: Old"');
    expect(html).not.toContain('aria-label="Reactivate: Leak or Blockage"');
    expect(html).not.toContain('aria-label="Deactivate: Old"');
    expect(html).toContain('>Active<');
    expect(html).toContain('>Inactive<');
  });

  it('marks busy buttons with aria-disabled instead of disabling them, so focus stays', () => {
    const html = render('en', table(true));
    expect(count(html, /aria-disabled="true"/g)).toBe(6);
    expect(html).not.toContain('disabled=""');
  });
});

// --- markup: labels -------------------------------------------------------------------------------------

describe('LabelsFormView', () => {
  const view = (errors: LabelFormErrors, pending = false): ReactElement =>
    createElement(LabelsFormView, { drafts: labelDrafts(LABELS), errors, pending, onChange: noop, onSubmit: noop });

  it.each(LANGUAGES)('renders seven groups of a Hebrew and an English label (%s)', (language) => {
    const html = render(language, view(NO_LABEL_ERRORS));
    expectNoRawKeys(html);
    expect(count(html, /<fieldset/g)).toBe(7);
    expect(count(html, /<legend/g)).toBe(7);
    expect(count(html, /<input/g)).toBe(14);
    for (const key of LABEL_FIELDS) {
      for (const lang of CONFIG_LANGUAGES) {
        expect(html).toContain(`id="${labelInputId(key, lang)}"`);
        expect(html).toContain(`for="${labelInputId(key, lang)}"`);
      }
    }
    expect(count(html, /aria-required="true"/g)).toBe(14);
    expect(html).not.toContain('aria-invalid="true"');
    expect(html).toContain('value="Apartment number"');
    expect(html).toContain('value="מספר דירה"');
  });

  it('names the fields with the existing field texts', () => {
    const en = render('en', view(NO_LABEL_ERRORS));
    expect(en).toContain('<legend>Issue description</legend>');
    expect(en).toContain('1 to 60 characters');
    const he = render('he', view(NO_LABEL_ERRORS));
    expect(he).toContain('<legend>תיאור התקלה</legend>');
  });

  it('marks every failing text and ties its message to it (R19.5)', () => {
    const errors = labelFormErrors('invalid_label_he', {
      'city.label_he': 'invalid_label_he',
      'urgent.label_en': 'invalid_label_en',
    });
    const html = render('en', view(errors));
    expect(count(html, /aria-invalid="true"/g)).toBe(2);
    expect(html).toContain('The Hebrew label is not valid. Enter 1 to 60 characters.');
    expect(html).toContain('The English label is not valid. Enter 1 to 60 characters.');
    expect(html).toContain(`id="${labelInputId('city', 'he')}-error"`);
    expect(html).toContain(`id="${labelInputId('urgent', 'en')}-error"`);
  });

  it('shows a refusal of the whole form as an alert and disables the inputs while saving', () => {
    const html = render('en', view(labelFormErrors('generic'), true));
    expect(html).toContain('role="alert"');
    expect(html).toContain('Something went wrong. Try again.');
    expect(count(html, /disabled=""/g)).toBe(15);
  });
});

// --- markup: options ------------------------------------------------------------------------------------

describe('OptionsFormView', () => {
  const view = (settings: FormSettings, error: null | 'errors.generic' = null, pending = false): ReactElement =>
    createElement(OptionsFormView, { settings, pending, error, onChange: noop, onSubmit: noop });

  it.each(LANGUAGES)('renders the two options as labelled checkboxes (%s)', (language) => {
    const html = render(language, view(SETTINGS));
    expectNoRawKeys(html);
    expect(count(html, /type="checkbox"/g)).toBe(2);
    for (const name of OPTION_NAMES) {
      expect(html).toContain(`id="${optionInputId(name)}"`);
      expect(html).toContain(`for="${optionInputId(name)}"`);
    }
  });

  it('checks the box of each option that is on (R19.2, R19.3)', () => {
    const html = render('en', view(SETTINGS));
    const description = html.match(/<input[^>]*id="config-option-description-required"[^>]*>/)?.[0] ?? '';
    const photo = html.match(/<input[^>]*id="config-option-photo-upload-enabled"[^>]*>/)?.[0] ?? '';
    expect(description).toContain('checked=""');
    expect(photo).not.toContain('checked=""');
    expect(html).toContain('The issue description is required');
    expect(html).toContain('Allow a photo on a ticket');
    expect(html).toContain('When this is off, photos already attached stay visible.');
  });

  it('shows a refused save as an alert', () => {
    const html = render('en', view(SETTINGS, 'errors.generic'));
    expect(html).toContain('role="alert"');
    expect(html).toContain('Something went wrong. Try again.');
  });
});

// --- the pages --------------------------------------------------------------------------------------------

describe('CategoriesPage', () => {
  const page = (): ReactElement => createElement(CategoriesPage);
  const prime = (categories: readonly StaffCategory[] | null) => (client: QueryClient) => {
    if (categories !== null) client.setQueryData<readonly StaffCategory[]>(staffKeys.categories, categories);
  };

  it.each(LANGUAGES)('shows the heading, the add button and the rows from the cache (%s)', (language) => {
    const html = render(language, page(), prime([leak, old]));
    expectNoRawKeys(html);
    expect(html).toContain('<h1');
    expect(html).toContain('Leak or Blockage');
    expect(html).toContain('ישן');
    expect(html).toContain('role="status"');
    expect(html).not.toContain('role="dialog"');
  });

  it('says it is loading before the list arrives, with no table', () => {
    const html = render('en', page(), prime(null));
    expect(html).toContain('Loading…');
    expect(html).not.toContain('<table');
  });

  it('shows the empty message for an empty list', () => {
    const html = render('en', page(), prime([]));
    expect(html).toContain('There are no categories.');
    expect(html).not.toContain('<table');
  });
});

describe('LabelsPage', () => {
  it.each(LANGUAGES)('shows the editor with the labels the service returned (%s)', (language) => {
    const html = render(language, createElement(LabelsPage), (client) => {
      client.setQueryData<StaffLabels>(staffKeys.labels, LABELS);
    });
    expectNoRawKeys(html);
    expect(count(html, /<fieldset/g)).toBe(7);
    expect(html).toContain('value="Photo"');
    expect(html).toContain('role="status"');
  });

  it('says it is loading before the labels arrive, with no controls', () => {
    const html = render('en', createElement(LabelsPage));
    expect(html).toContain('Loading…');
    expect(html).not.toContain('<form');
  });
});

describe('OptionsPage', () => {
  it.each(LANGUAGES)('shows the two options with the values the service returned (%s)', (language) => {
    const html = render(language, createElement(OptionsPage), (client) => {
      client.setQueryData<FormSettings>(staffKeys.settings, SETTINGS);
    });
    expectNoRawKeys(html);
    expect(count(html, /type="checkbox"/g)).toBe(2);
    expect(html).toContain('role="status"');
  });

  it('says it is loading before the settings arrive, with no controls', () => {
    const html = render('en', createElement(OptionsPage));
    expect(html).toContain('Loading…');
    expect(html).not.toContain('<form');
  });
});

describe('texts', () => {
  it('has a text for every error code a configuration refusal can carry, in both languages', () => {
    const codes = ['invalid_name_he', 'invalid_name_en', 'invalid_label_he', 'invalid_label_en', 'duplicate_category', 'last_active_category', 'category_not_found', 'unknown_field', 'insufficient_role'];
    for (const code of codes) expect(ERROR_CODES).toContain(code);
    for (const language of LANGUAGES) {
      const i18n = i18nFor(language);
      for (const code of codes) expect(i18n.t(`errors.${code}`, { ns: 'service' })).not.toBe(`errors.${code}`);
    }
  });

  it('has every key the screens use for the two result messages and the new notices', () => {
    for (const language of LANGUAGES) {
      const i18n = i18nFor(language);
      for (const key of [
        'management.settings.categories.removed',
        'management.settings.categories.deactivated',
        'management.settings.categories.deactivateDone',
        'management.settings.categories.reactivateDone',
        'management.settings.categories.createTitle',
        'management.settings.categories.editTitle',
        'management.settings.categories.created',
        'management.settings.categories.updated',
        'management.settings.labels.saved',
        'management.settings.labels.noChanges',
        'management.settings.options.saved',
      ]) {
        expect(i18n.t(key, { ns: 'service' }), `${language} ${key}`).not.toBe(key);
      }
    }
  });
});
