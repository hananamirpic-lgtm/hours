// Tests of the management New ticket screen (task 29.5; R26.1, R26.2, R26.7, R23.12): the pure model and the
// markup of the picker and the form in both languages. No DOM and no testing library: `renderToStaticMarkup`.
// The container (`NewTicketPage`) holds queries and timers and is not rendered here; everything it decides is
// a pure function of `newTicketModel` or of the portal's pure form model.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { describe, expect, it } from 'vitest';
import en from '@/i18n/locales/en/service.json';
import he from '@/i18n/locales/he/service.json';
import { FORM_LABEL_KEYS, type FormConfig, type FormSettings } from '../api/formTypes';
import { StaffApiError } from '../api/staff';
import type { StaffCustomer } from '../api/staffTypes';
import { createPortalI18n, type PortalLanguage } from '../portal/portalI18n';
import {
  MAX_PHOTO_BYTES,
  categoryOptions,
  initialValues,
  resolveLabels,
  shownErrors,
  type FormValues,
} from '../portal/screens/ticketFormModel';
import {
  NEW_TICKET_PATH,
  buildStaffFormData,
  createdTicketPath,
  customerPlace,
  failureFromError,
  hasMoreMatches,
  noticeKey,
  pickerQuery,
  pickerState,
  settlementSelectOptions,
  staffSettlementChoice,
} from './newTicketModel';
import {
  ChosenCustomerView,
  CustomerPickerView,
  NewTicketFormView,
  type CustomerPickerViewProps,
  type NewTicketFormViewProps,
} from './newTicketView';
import { openedByStaff } from './ticketDetailModel';

// --- fixtures --------------------------------------------------------------------------------------

function customer(overrides: Partial<StaffCustomer> = {}): StaffCustomer {
  return {
    id: 'cust-1',
    full_name: 'Ruth Levi',
    phone: '0501234567',
    city: 'Haifa',
    apartment: '12',
    is_active: true,
    ...overrides,
  };
}

const SETTINGS: FormSettings = { description_required: true, photo_upload_enabled: true };

const CONFIG: FormConfig = {
  categories: [
    { id: 'c1', name_he: 'אינסטלציה', name_en: 'Plumbing' },
    { id: 'c2', name_he: 'חשמל', name_en: 'Electricity' },
  ],
  labels: {
    city: { he: 'ישוב מותאם', en: 'Custom settlement' },
    urgent: { he: 'דחוף מאוד', en: 'Very urgent' },
  },
  settings: SETTINGS,
};

function labelsFor(language: 'he' | 'en') {
  const texts = language === 'en' ? en : he;
  return resolveLabels(CONFIG.labels, language, texts.fields);
}

function formProps(language: 'he' | 'en', overrides: Partial<NewTicketFormViewProps> = {}): NewTicketFormViewProps {
  return {
    values: initialValues(customer()),
    fieldErrors: {},
    notice: null,
    pending: false,
    categories: categoryOptions(CONFIG.categories, language),
    labels: labelsFor(language),
    settings: SETTINGS,
    photo: null,
    photoInputKey: 0,
    onChange: () => undefined,
    onPhotoChange: () => undefined,
    onSubmit: () => undefined,
    onFocusField: () => undefined,
    ...overrides,
  };
}

function pickerProps(overrides: Partial<CustomerPickerViewProps> = {}): CustomerPickerViewProps {
  return {
    searchDraft: '',
    state: { kind: 'loading' },
    busy: false,
    onSearchDraftChange: () => undefined,
    onSearchSubmit: () => undefined,
    onPick: () => undefined,
    onRetry: () => undefined,
    ...overrides,
  };
}

function render(language: PortalLanguage, element: ReactElement): string {
  return renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n: createPortalI18n(language) },
      createElement(StaticRouter, { location: '/tickets/list/new' }, element),
    ),
  );
}

const LANGUAGES = ['he', 'en'] as const;
const texts = { en, he };

/** A raw translation key in the markup means a key was not found. */
function expectNoMissingKeys(markup: string): void {
  expect(markup).not.toMatch(/\b(management|errors|a11y|status|fields)\.[a-zA-Z_.]+/);
}

/** The ids of every input, select and textarea of the markup. */
function controlIds(markup: string): string[] {
  return Array.from(markup.matchAll(/<(?:input|select|textarea)\b[^>]*?\bid="([^"]+)"/g), (match) => match[1] as string);
}

function tag(markup: string, id: string): string {
  const match = new RegExp(`<(?:input|select|textarea)\\b[^>]*?\\bid="${id}"[^>]*>`).exec(markup);
  if (match === null) throw new Error(`no control ${id}`);
  return match[0];
}

// --- the picker query and state (R26.1) ------------------------------------------------------------

describe('picker query and state', () => {
  it('asks for active customers only, page 1, with the trimmed search when it has text', () => {
    expect(pickerQuery('')).toEqual({ page: 1, active: true });
    expect(pickerQuery('   ')).toEqual({ page: 1, active: true });
    expect(pickerQuery('  dana ')).toEqual({ page: 1, active: true, search: 'dana' });
    expect(pickerQuery('050')).toEqual({ page: 1, active: true, search: '050' });
  });

  it('is loading, error, empty or results', () => {
    const page = { items: [customer()], total: 1 };
    expect(pickerState({ data: undefined, errorCode: null })).toEqual({ kind: 'loading' });
    expect(pickerState({ data: undefined, errorCode: 'network' })).toEqual({ kind: 'error', code: 'network' });
    expect(pickerState({ data: { items: [], total: 0 }, errorCode: null })).toEqual({ kind: 'empty' });
    expect(pickerState({ data: page, errorCode: null })).toEqual({ kind: 'results', items: page.items, total: 1 });
    // A failed reload keeps the results that are already shown.
    expect(pickerState({ data: page, errorCode: 'network' }).kind).toBe('results');
  });

  it('tells when more customers match than the page shows', () => {
    const items = [customer()];
    expect(hasMoreMatches({ kind: 'results', items, total: 1 })).toBe(false);
    expect(hasMoreMatches({ kind: 'results', items, total: 51 })).toBe(true);
    expect(hasMoreMatches({ kind: 'empty' })).toBe(false);
  });

  it('reads the settlement and apartment, with blank for an absent value', () => {
    expect(customerPlace(customer())).toEqual({ city: 'Haifa', apartment: '12' });
    expect(customerPlace(customer({ city: null, apartment: '  ' }))).toEqual({ city: '', apartment: '' });
  });
});

// --- the prefill and the request body (R26.2) ------------------------------------------------------

describe('prefill and multipart body', () => {
  it('prefills city, apartment and phone from the customer, and leaves the rest empty', () => {
    expect(initialValues(customer())).toEqual({
      city: 'Haifa',
      apartment: '12',
      phone: '0501234567',
      categoryId: '',
      description: '',
      urgent: false,
    });
    expect(initialValues(customer({ city: null, apartment: null }))).toMatchObject({ city: '', apartment: '' });
  });

  const values: FormValues = {
    city: 'Haifa',
    apartment: '12',
    phone: '0509999999',
    categoryId: 'c1',
    description: 'The tap leaks',
    urgent: true,
  };

  it('sends the customer form parts and customer_id, with values as typed', () => {
    const body = buildStaffFormData('cust-1', values, null, SETTINGS);
    expect(body).toBeInstanceOf(FormData);
    expect(Object.fromEntries(body.entries())).toEqual({
      city: 'Haifa',
      apartment: '12',
      phone: '0509999999',
      category_id: 'c1',
      description: 'The tap leaks',
      urgent: 'true',
      customer_id: 'cust-1',
    });
  });

  it('adds the photo part only when a photo is chosen and uploads are on', () => {
    const file = new File(['x'], 'leak.jpg', { type: 'image/jpeg' });
    expect(buildStaffFormData('cust-1', values, file, SETTINGS).get('photo')).toBeInstanceOf(File);
    expect(buildStaffFormData('cust-1', values, null, SETTINGS).has('photo')).toBe(false);
    expect(buildStaffFormData('cust-1', values, file, { ...SETTINGS, photo_upload_enabled: false }).has('photo')).toBe(false);
  });

  it('shows the size hint for a photo over 10 MB and none for exactly 10 MB', () => {
    expect(shownErrors({}, { size: MAX_PHOTO_BYTES }, true)).toEqual({});
    expect(shownErrors({}, { size: MAX_PHOTO_BYTES + 1 }, true)).toEqual({ photo: 'photo_too_large' });
    expect(shownErrors({}, { size: MAX_PHOTO_BYTES + 1 }, false)).toEqual({});
  });

  it('opens the new ticket detail after the 201', () => {
    expect(createdTicketPath({ ticket_number: 12345 })).toBe('/tickets/list/12345');
    expect(NEW_TICKET_PATH).toBe('/tickets/list/new');
  });
});

// --- the settlement choice (R26.1, R30.2, R30.3, R30.6) --------------------------------------------

const HAIFA = { id: 'l-1', name_he: 'חיפה', name_en: 'Haifa' };
const ACRE = { id: 'l-2', name_he: 'עכו', name_en: 'Acre' };
const LOCALITIES = [HAIFA, ACRE];
const ACTIVE = { required: true, localities: LOCALITIES };

describe('settlement choice of the staff form', () => {
  it('is active only with a non-empty list and settlements_required', () => {
    expect(staffSettlementChoice({ localities: LOCALITIES, settlements_required: true })).toEqual(ACTIVE);
    expect(staffSettlementChoice({ localities: [], settlements_required: true })).toEqual({ required: false, localities: [] });
    expect(staffSettlementChoice({ localities: LOCALITIES, settlements_required: false })).toEqual({ required: false, localities: [] });
    expect(staffSettlementChoice({})).toEqual({ required: false, localities: [] });
  });

  it('names the options in the current language, with the other language for a blank name', () => {
    expect(settlementSelectOptions(LOCALITIES, 'en')).toEqual([
      { id: 'l-1', name: 'Haifa' },
      { id: 'l-2', name: 'Acre' },
    ]);
    expect(settlementSelectOptions(LOCALITIES, 'he').map((option) => option.name)).toEqual(['חיפה', 'עכו']);
    expect(settlementSelectOptions([{ id: 'l-3', name_he: ' ', name_en: 'Safed' }], 'he')).toEqual([{ id: 'l-3', name: 'Safed' }]);
  });

  it("preselects the customer's settlement when it is in the list, else nothing, and ignores the text city", () => {
    expect(initialValues(customer({ locality: ACRE }), ACTIVE)).toMatchObject({ city: '', localityId: 'l-2' });
    expect(initialValues(customer({ locality: { id: 'l-9', name_he: 'ישן', name_en: 'Old' } }), ACTIVE)).toMatchObject({ localityId: '' });
    expect(initialValues(customer({ locality: null }), ACTIVE)).toMatchObject({ city: '', localityId: '' });
    // With no active list the free-text city is prefilled as before.
    const free = initialValues(customer(), { required: false, localities: [] });
    expect(free.city).toBe('Haifa');
    expect(free.localityId).toBeUndefined();
  });

  const values: FormValues = {
    city: '',
    localityId: 'l-2',
    apartment: '12',
    phone: '0509999999',
    categoryId: 'c1',
    description: 'The tap leaks',
    urgent: false,
  };

  it('sends locality_id and no city part while the list is active', () => {
    const body = buildStaffFormData('cust-1', values, null, SETTINGS, true);
    expect(Object.fromEntries(body.entries())).toEqual({
      locality_id: 'l-2',
      apartment: '12',
      phone: '0509999999',
      category_id: 'c1',
      description: 'The tap leaks',
      urgent: 'false',
      customer_id: 'cust-1',
    });
    expect(body.has('city')).toBe(false);
  });

  it('sends the city text and no locality_id with an empty list', () => {
    const body = buildStaffFormData('cust-1', { ...values, city: 'Haifa' }, null, SETTINGS);
    expect(body.get('city')).toBe('Haifa');
    expect(body.has('locality_id')).toBe(false);
  });

  it('shows invalid_settlement beside the city field and keeps the other fields clean', () => {
    const failure = failureFromError(new StaffApiError('invalid_settlement', { city: 'invalid_settlement' }));
    expect(failure).toEqual({ fieldErrors: { city: 'invalid_settlement' }, notice: null });
  });
});

// --- failures --------------------------------------------------------------------------------------

describe('failureFromError', () => {
  it('shows each failing form field of a 422 beside the field', () => {
    const failure = failureFromError(
      new StaffApiError('invalid_phone', { phone: 'invalid_phone', category_id: 'invalid_category' }),
    );
    expect(failure).toEqual({ fieldErrors: { phone: 'invalid_phone', category_id: 'invalid_category' }, notice: null });
  });

  it('turns an error on customer_id or another part into a notice, with the form fields kept', () => {
    expect(failureFromError(new StaffApiError('customer_not_found', { customer_id: 'customer_not_found' }))).toEqual({
      fieldErrors: {},
      notice: { kind: 'error', code: 'customer_not_found' },
    });
    expect(failureFromError(new StaffApiError('invalid_city', { city: 'invalid_city', customer_id: 'customer_not_found' }))).toEqual({
      fieldErrors: { city: 'invalid_city' },
      notice: { kind: 'error', code: 'customer_not_found' },
    });
  });

  it('keeps a customer that does not exist or is inactive as a notice', () => {
    expect(failureFromError(new StaffApiError('customer_not_found')).notice).toEqual({ kind: 'error', code: 'customer_not_found' });
    expect(failureFromError(new StaffApiError('customer_inactive')).notice).toEqual({ kind: 'error', code: 'customer_inactive' });
  });

  it('puts a body that is too large on the photo field', () => {
    for (const code of ['request_too_large', 'photo_too_large']) {
      expect(failureFromError(new StaffApiError(code))).toEqual({ fieldErrors: { photo: 'photo_too_large' }, notice: null });
    }
  });

  it('tells the staff member to check the list when the answer was lost', () => {
    for (const error of [new StaffApiError('network'), new StaffApiError('generic'), new TypeError('x')]) {
      expect(failureFromError(error)).toEqual({ fieldErrors: {}, notice: { kind: 'submit_failed' } });
    }
  });

  it('names the text of a notice', () => {
    expect(noticeKey({ kind: 'submit_failed' })).toBe('management.newTicket.submitFailed');
    expect(noticeKey({ kind: 'error', code: 'customer_inactive' })).toBe('errors.customer_inactive');
    expect(noticeKey({ kind: 'error', code: 'something_new' })).toBe('errors.generic');
  });
});

// --- the ticket detail line (R26.7) ----------------------------------------------------------------

describe('openedByStaff', () => {
  it('returns the username only when it is a non-empty string', () => {
    expect(openedByStaff({ opened_by_username: 'avi' })).toBe('avi');
    expect(openedByStaff({ opened_by_username: null })).toBeNull();
    expect(openedByStaff({})).toBeNull();
    expect(openedByStaff({ opened_by_username: '' })).toBeNull();
    expect(openedByStaff({ opened_by_username: '   ' })).toBeNull();
    expect(openedByStaff({ opened_by_username: 7 as unknown as string })).toBeNull();
  });
});

// --- the picker markup (R26.1) ---------------------------------------------------------------------

describe('CustomerPickerView markup', () => {
  const results = {
    kind: 'results',
    items: [customer(), customer({ id: 'cust-2', full_name: 'Dana Cohen', phone: '0529876543', city: null, apartment: null })],
    total: 2,
  } as const;

  it.each(LANGUAGES)('has a labelled search box tied to its hint, in %s', (language) => {
    const markup = render(language, createElement(CustomerPickerView, pickerProps({ state: results })));
    expectNoMissingKeys(markup);
    expect(markup).toContain('role="search"');
    expect(markup).toMatch(/<label[^>]*for="new-ticket-search"[^>]*>/);
    expect(markup).toContain(texts[language].management.newTicket.picker.searchLabel);
    expect(tag(markup, 'new-ticket-search')).toContain('aria-describedby="new-ticket-search-hint"');
    expect(markup).toContain('id="new-ticket-search-hint"');
  });

  it.each(LANGUAGES)('lists each active customer as a button with name, phone, settlement and apartment, in %s', (language) => {
    const markup = render(language, createElement(CustomerPickerView, pickerProps({ state: results })));
    expect(markup).toContain(texts[language].management.newTicket.picker.found.replace('{{count}}', '2'));
    const list = /<ul[^>]*>.*<\/ul>/s.exec(markup)?.[0] ?? '';
    expect(list.match(/<li>/g)).toHaveLength(2);
    expect(list.match(/<button type="button"/g)).toHaveLength(2);
    expect(list).toContain('Ruth Levi');
    expect(list).toContain('<bdi dir="ltr">0501234567</bdi>');
    expect(list).toContain('Haifa');
    expect(list).toContain(texts[language].management.newTicket.picker.apartment.replace('{{apartment}}', '12'));
    // The second customer has no settlement and no apartment: nothing is shown for them.
    const second = list.slice(list.indexOf('Dana Cohen'));
    expect(second).toContain('0529876543');
    expect(second).not.toContain(texts[language].management.newTicket.picker.apartment.replace('{{apartment}}', ''));
    expect(markup).not.toContain(texts[language].management.newTicket.picker.refine);
  });

  it('shows a note to narrow the search when more customers match than are listed', () => {
    const markup = render('en', createElement(CustomerPickerView, pickerProps({ state: { ...results, total: 120 } })));
    expect(markup).toContain(en.management.newTicket.picker.refine);
    expect(markup).toContain('Matching customers: 120');
  });

  it('shows loading, the empty message and the error with a retry button', () => {
    const loading = render('en', createElement(CustomerPickerView, pickerProps({ state: { kind: 'loading' } })));
    expect(loading).toContain(en.management.common.loading);
    expect(loading).not.toContain('<ul');
    const empty = render('en', createElement(CustomerPickerView, pickerProps({ state: { kind: 'empty' } })));
    expect(empty).toContain(en.management.newTicket.picker.empty);
    const failed = render('en', createElement(CustomerPickerView, pickerProps({ state: { kind: 'error', code: 'network' } })));
    expect(failed).toContain('role="alert"');
    expect(failed).toContain(en.management.newTicket.picker.loadError);
    expect(failed).toContain(en.management.common.retry);
  });

  it('shows customer text literally', () => {
    const markup = render(
      'en',
      createElement(CustomerPickerView, pickerProps({ state: { kind: 'results', items: [customer({ full_name: '<b>x</b>' })], total: 1 } })),
    );
    expect(markup).not.toContain('<b>x</b>');
    expect(markup).toContain('&lt;b&gt;x&lt;/b&gt;');
  });
});

describe('ChosenCustomerView markup', () => {
  it.each(LANGUAGES)('shows the customer as text with a Change customer button, in %s', (language) => {
    const markup = render(language, createElement(ChosenCustomerView, { customer: customer(), pending: false, onChange: () => undefined }));
    expectNoMissingKeys(markup);
    expect(markup).toContain('Ruth Levi');
    expect(markup).toContain('0501234567');
    expect(markup).toContain(`>${texts[language].management.newTicket.customer.change}</button>`);
    expect(markup).not.toMatch(/<button[^>]*disabled/);
  });

  it('disables the change button while the ticket is sent', () => {
    const markup = render('en', createElement(ChosenCustomerView, { customer: customer(), pending: true, onChange: () => undefined }));
    expect(markup).toMatch(/<button[^>]*disabled/);
  });
});

// --- the form markup (R26.2, R23.12) ---------------------------------------------------------------

describe('NewTicketFormView markup', () => {
  it.each(LANGUAGES)('prefills the customer values and uses the current Field_Labels, in %s', (language) => {
    const markup = render(language, createElement(NewTicketFormView, formProps(language)));
    expectNoMissingKeys(markup);
    expect(tag(markup, 'ticket-form-city')).toContain('value="Haifa"');
    expect(tag(markup, 'ticket-form-apartment')).toContain('value="12"');
    expect(tag(markup, 'ticket-form-phone')).toContain('value="0501234567"');
    // A Field_Label that was changed in the configuration shows; the others fall back to the shipped text.
    expect(markup).toContain(language === 'en' ? 'Custom settlement' : 'ישוב מותאם');
    expect(markup).toContain(language === 'en' ? 'Very urgent' : 'דחוף מאוד');
    expect(markup).toContain(texts[language].fields.apartment);
    expect(markup).toContain(texts[language].fields.phone);
    expect(markup).toContain(language === 'en' ? 'Plumbing' : 'אינסטלציה');
  });

  it.each(LANGUAGES)('ties a visible label to every control and marks required fields as text, in %s', (language) => {
    const markup = render(language, createElement(NewTicketFormView, formProps(language)));
    const ids = controlIds(markup);
    expect(ids).toEqual([
      'ticket-form-city',
      'ticket-form-apartment',
      'ticket-form-phone',
      'ticket-form-category_id',
      'ticket-form-description',
      'ticket-form-photo',
      'ticket-form-urgent',
    ]);
    for (const id of ids) expect(markup).toMatch(new RegExp(`<label[^>]*for="${id}"`));
    const required = texts[language].a11y.required;
    for (const id of ['city', 'apartment', 'phone', 'category_id']) {
      const label = new RegExp(`<label[^>]*for="ticket-form-${id}"[^>]*>(.*?)</label>`, 's').exec(markup)?.[1] ?? '';
      expect(label).toContain(required);
      expect(tag(markup, `ticket-form-${id}`)).toContain('aria-required="true"');
    }
  });

  it('follows the description setting for the required marker', () => {
    const marker = (settings: FormSettings, language: 'en' | 'he'): string => {
      const markup = render(language, createElement(NewTicketFormView, formProps(language, { settings })));
      return new RegExp('<label[^>]*for="ticket-form-description"[^>]*>(.*?)</label>', 's').exec(markup)?.[1] ?? '';
    };
    const optional = { ...SETTINGS, description_required: false };
    expect(marker(SETTINGS, 'en')).toContain(en.a11y.required);
    expect(marker(optional, 'en')).toContain(en.a11y.optional);
    expect(marker(optional, 'en')).not.toContain(en.a11y.required);
    expect(marker(SETTINGS, 'he')).toContain(he.a11y.required);
    expect(marker(optional, 'he')).toContain(he.a11y.optional);
  });

  it('shows the photo control only when photo upload is enabled, accepting JPEG, PNG and WebP', () => {
    const on = render('en', createElement(NewTicketFormView, formProps('en')));
    expect(tag(on, 'ticket-form-photo')).toContain('type="file"');
    expect(tag(on, 'ticket-form-photo')).toContain('accept="image/jpeg,image/png,image/webp"');
    expect(on).toContain(en.management.newTicket.form.photoHint);
    const off = render(
      'en',
      createElement(NewTicketFormView, formProps('en', { settings: { ...SETTINGS, photo_upload_enabled: false } })),
    );
    expect(off).not.toContain('type="file"');
    expect(off).not.toContain('ticket-form-photo');
    expect(controlIds(off)).toHaveLength(6);
  });

  it('names the chosen photo and offers to remove it', () => {
    const markup = render('en', createElement(NewTicketFormView, formProps('en', { photo: { name: 'leak.jpg' } })));
    expect(markup).toContain('leak.jpg');
    expect(markup).toContain(en.management.newTicket.form.photoRemove);
  });

  it.each(LANGUAGES)('marks each failing field, ties its message to it and links the summary, in %s', (language) => {
    const markup = render(
      language,
      createElement(
        NewTicketFormView,
        formProps(language, {
          values: { ...initialValues(customer()), phone: '12', description: 'typed text', categoryId: 'c2' },
          fieldErrors: { phone: 'invalid_phone', category_id: 'invalid_category' },
        }),
      ),
    );
    expectNoMissingKeys(markup);
    for (const id of ['phone', 'category_id']) {
      expect(tag(markup, `ticket-form-${id}`)).toContain('aria-invalid="true"');
      expect(tag(markup, `ticket-form-${id}`)).toContain(`aria-describedby="ticket-form-${id}-error"`);
      expect(markup).toContain(`id="ticket-form-${id}-error"`);
    }
    expect(tag(markup, 'ticket-form-city')).not.toContain('aria-invalid');
    expect(markup).toContain(texts[language].errors.invalid_phone);
    expect(markup).toContain(texts[language].errors.invalid_category);
    expect(markup).toContain('id="ticket-form-error-summary"');
    expect(markup).toContain('href="#ticket-form-phone"');
    expect(markup).toContain('href="#ticket-form-category_id"');
    // Every typed value stays.
    expect(tag(markup, 'ticket-form-phone')).toContain('value="12"');
    expect(markup).toContain('>typed text</textarea>');
    expect(markup).toMatch(/<option value="c2" selected="">/);
  });

  describe('with an active settlement list', () => {
    const withList = (language: 'he' | 'en', overrides: Partial<NewTicketFormViewProps> = {}): string =>
      render(
        language,
        createElement(
          NewTicketFormView,
          formProps(language, {
            values: initialValues(customer({ locality: ACRE }), ACTIVE),
            settlements: settlementSelectOptions(LOCALITIES, language),
            ...overrides,
          }),
        ),
      );

    it.each(LANGUAGES)('shows the City field as a labelled select with the placeholder, in %s', (language) => {
      const markup = withList(language);
      expectNoMissingKeys(markup);
      expect(markup).toMatch(/<select[^>]*id="ticket-form-city"/);
      expect(markup).not.toMatch(/<input[^>]*id="ticket-form-city"/);
      expect(markup).toMatch(/<label[^>]*for="ticket-form-city"/);
      expect(tag(markup, 'ticket-form-city')).toContain('aria-required="true"');
      expect(markup).toContain(`<option value="">${texts[language].portal.form.settlementPlaceholder}</option>`);
      // The ids of the form's controls are unchanged, so focus and the summary links still find the field.
      expect(controlIds(markup)).toContain('ticket-form-city');
      expect(controlIds(markup)).toHaveLength(7);
    });

    it.each(LANGUAGES)("lists the settlements in the language and preselects the customer's, in %s", (language) => {
      const markup = withList(language);
      const names = language === 'en' ? ['Haifa', 'Acre'] : ['חיפה', 'עכו'];
      expect(markup).toContain(`<option value="l-1">${names[0]}</option>`);
      expect(markup).toMatch(new RegExp(`<option value="l-2" selected="">${names[1]}</option>`));
    });

    it('selects nothing when the customer has no settlement in the list', () => {
      const markup = withList('en', { values: initialValues(customer({ locality: null }), ACTIVE) });
      expect(markup).not.toMatch(/<option value="l-\d" selected="">/);
      expect(markup).toMatch(/<option value="" selected="">/);
    });

    it.each(LANGUAGES)('ties an invalid_settlement refusal to the select, in %s', (language) => {
      const markup = withList(language, { fieldErrors: { city: 'invalid_settlement' } });
      expectNoMissingKeys(markup);
      expect(tag(markup, 'ticket-form-city')).toContain('aria-invalid="true"');
      expect(tag(markup, 'ticket-form-city')).toContain('aria-describedby="ticket-form-city-error"');
      expect(markup).toContain('id="ticket-form-city-error"');
      expect(markup).toContain(texts[language].errors.invalid_settlement);
      expect(markup).toContain('href="#ticket-form-city"');
    });
  });

  it('keeps the free-text City field when no settlements are passed or the list is empty', () => {
    for (const markup of [
      render('en', createElement(NewTicketFormView, formProps('en'))),
      render('en', createElement(NewTicketFormView, formProps('en', { settlements: [] }))),
    ]) {
      expect(markup).toMatch(/<input[^>]*id="ticket-form-city"/);
      expect(markup).not.toMatch(/<select[^>]*id="ticket-form-city"/);
      expect(tag(markup, 'ticket-form-city')).toContain('value="Haifa"');
    }
  });

  it('shows no summary and no error state while there are no errors', () => {
    const markup = render('en', createElement(NewTicketFormView, formProps('en')));
    expect(markup).not.toContain('aria-invalid');
    expect(markup).not.toContain('ticket-form-error-summary');
    expect(markup).not.toContain('role="alert"');
  });

  it.each(LANGUAGES)('shows a notice for an inactive customer in %s', (language) => {
    const markup = render(
      language,
      createElement(NewTicketFormView, formProps(language, { notice: { kind: 'error', code: 'customer_inactive' } })),
    );
    expect(markup).toContain('role="alert"');
    expect(markup).toContain(texts[language].errors.customer_inactive);
  });

  it('shows the lost-answer notice that asks to check the ticket list', () => {
    const markup = render('en', createElement(NewTicketFormView, formProps('en', { notice: { kind: 'submit_failed' } })));
    expect(markup).toContain(en.management.newTicket.submitFailed);
  });

  it('disables the submit button while the request is pending, and only then', () => {
    const idle = render('en', createElement(NewTicketFormView, formProps('en')));
    expect(idle).toContain(`>${en.management.newTicket.form.submit}</button>`);
    expect(idle).not.toMatch(/<button[^>]*type="submit"[^>]*disabled/);
    const busy = render('en', createElement(NewTicketFormView, formProps('en', { pending: true })));
    expect(busy).toMatch(/<button[^>]*type="submit"[^>]*disabled/);
    expect(busy).toContain(en.management.newTicket.form.submitting);
    expect(busy).toContain('aria-busy="true"');
  });

  it('links Cancel back to the ticket list', () => {
    const markup = render('en', createElement(NewTicketFormView, formProps('en')));
    expect(markup).toContain('href="/tickets/list"');
  });

  it('shows typed text literally', () => {
    const markup = render(
      'en',
      createElement(NewTicketFormView, formProps('en', { values: { ...initialValues(customer()), description: '<script>x</script>' } })),
    );
    expect(markup).not.toContain('<script>');
    expect(markup).toContain('&lt;script&gt;');
  });
});

// --- locale keys, routing and bundle boundaries ----------------------------------------------------

function lookup(tree: unknown, key: string): unknown {
  return key.split('.').reduce<unknown>((node, part) => (typeof node === 'object' && node !== null ? (node as Record<string, unknown>)[part] : undefined), tree);
}

describe('locale keys', () => {
  const keys = [
    'management.tickets.newTicket',
    'management.ticket.openedByStaff',
    'management.newTicket.title',
    'management.newTicket.back',
    'management.newTicket.submitFailed',
    'management.newTicket.picker.title',
    'management.newTicket.picker.intro',
    'management.newTicket.picker.searchLabel',
    'management.newTicket.picker.searchHint',
    'management.newTicket.picker.found',
    'management.newTicket.picker.resultsLabel',
    'management.newTicket.picker.empty',
    'management.newTicket.picker.loadError',
    'management.newTicket.picker.apartment',
    'management.newTicket.picker.refine',
    'management.newTicket.customer.title',
    'management.newTicket.customer.change',
    'management.newTicket.form.title',
    'management.newTicket.form.requiredNote',
    'management.newTicket.form.errorSummary',
    'management.newTicket.form.categoryPlaceholder',
    'management.newTicket.form.photoHint',
    'management.newTicket.form.photoSelected',
    'management.newTicket.form.photoRemove',
    'management.newTicket.form.urgentHelp',
    'management.newTicket.form.submit',
    'management.newTicket.form.submitting',
    'management.newTicket.form.cancel',
    'management.newTicket.form.loadError',
    'errors.customer_inactive',
    'errors.customer_not_found',
    ...FORM_LABEL_KEYS.map((key) => `fields.${key}`),
  ];

  it.each(keys)('has non-empty text for %s in both languages', (key) => {
    for (const tree of [en, he]) {
      const value = lookup(tree, key);
      expect(typeof value).toBe('string');
      expect((value as string).trim()).not.toBe('');
    }
  });

  it('words the opened-by line as the requirement asks', () => {
    expect(en.management.ticket.openedByStaff).toBe('Opened by staff: {{name}}');
    expect(he.management.ticket.openedByStaff).toBe('נפתחה על ידי הצוות: {{name}}');
  });
});

describe('sources', () => {
  const read = (path: string): string => readFileSync(fileURLToPath(new URL(path, import.meta.url)), 'utf8');

  it('declares /tickets/list/new before /tickets/list/:ticketNumber', () => {
    const app = read('../../App.tsx');
    const fixed = app.indexOf('path="list/new"');
    const dynamic = app.indexOf('path="list/:ticketNumber"');
    expect(fixed).toBeGreaterThan(-1);
    expect(dynamic).toBeGreaterThan(fixed);
  });

  it('imports only pure portal helpers into the management bundle, never a portal screen component', () => {
    for (const file of ['./newTicketModel.ts', './newTicketView.tsx', './pages/NewTicketPage.tsx']) {
      const specifiers = Array.from(read(file).matchAll(/(?:from|import)\s+['"]([^'"]+)['"]/g), (match) => match[1] as string);
      const portal = specifiers.filter((spec) => /(^|\/)portal\//.test(spec));
      for (const spec of portal) {
        expect([
          '../portal/formErrors',
          '../portal/screens/ticketFormModel',
          '../portal/screens/settlementModel',
          '../../portal/formErrors',
          '../../portal/screens/ticketFormModel',
        ]).toContain(spec);
      }
    }
  });

  it('sets no Content-Type of its own on the multipart request', () => {
    expect(read('../api/staff.ts')).not.toMatch(/multipart\/form-data/);
    expect(read('./pages/NewTicketPage.tsx')).not.toContain('Content-Type');
  });
});
