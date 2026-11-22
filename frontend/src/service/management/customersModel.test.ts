// Tests of the Customers screen (task 24.5): the pure view model, and server-rendered markup of the table,
// the form, the confirmation and the page with a filled cache. No DOM, no testing library.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { describe, expect, it } from 'vitest';
import { customerListQuery, staffKeys } from '../api/staff';
import type { StaffCustomer, StaffCustomerPage } from '../api/staffTypes';
import { createPortalI18n, type PortalLanguage } from '../portal/portalI18n';
import { CustomerConfirmView } from './CustomerConfirmDialog';
import { CustomerFormView, FORM_FIELD_ORDER } from './CustomerForm';
import {
  ACTIVE_FILTERS,
  CUSTOMER_FORM_ORDER,
  CUSTOMER_PAGE_SIZE,
  DEFAULT_LIST_STATE,
  EMPTY_FORM_VALUES,
  NO_FORM_ERRORS,
  NO_VALUE,
  actionTexts,
  availableActions,
  cellText,
  customerFormErrors,
  errorTextKey,
  firstFailingField,
  formValuesFromCustomer,
  hasActiveFilters,
  isCustomerGone,
  isolateName,
  listStateFromParams,
  listStateToParams,
  listStateToQuery,
  paginationState,
  parseActiveFilter,
  parsePage,
  pastTheEndPage,
  stateKey,
  toCustomerBody,
  withActive,
  withPage,
  withSearch,
  type CustomerAction,
  type CustomerFormErrors,
} from './customersModel';
import { CustomersTable } from './CustomersTable';
import { CustomersToolbar } from './CustomersToolbar';
import { CustomersPage } from './pages/CustomersPage';

const noop = (): void => undefined;

const dana: StaffCustomer = {
  id: 'c-1',
  full_name: 'Dana Levi',
  phone: '0501234567',
  city: 'Haifa',
  apartment: null,
  is_active: true,
};
const sivan: StaffCustomer = {
  id: 'c-2',
  full_name: 'סיון כהן',
  phone: '0521112222',
  city: null,
  apartment: '12',
  is_active: false,
};

// --- render helpers ---------------------------------------------------------------------------------

function i18nFor(language: PortalLanguage) {
  const instance = createPortalI18n(language);
  return instance;
}

function render(language: PortalLanguage, element: ReactElement, location = '/tickets/customers'): string {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(I18nextProvider, { i18n: i18nFor(language) }, createElement(StaticRouter, { location }, element)),
    ),
  );
}

function renderPage(language: PortalLanguage, location: string, page: StaffCustomerPage | null): string {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const url = new URL(location, 'http://localhost');
  const state = listStateFromParams(url.searchParams);
  if (page !== null) queryClient.setQueryData(staffKeys.customerList(listStateToQuery(state)), page);
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(
        I18nextProvider,
        { i18n: i18nFor(language) },
        createElement(StaticRouter, { location }, createElement(CustomersPage)),
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

const LANGUAGES: readonly PortalLanguage[] = ['en', 'he'];

// --- list state -------------------------------------------------------------------------------------

describe('list state in the address (R3.10)', () => {
  it('reads page, search text and state filter, with defaults for anything missing or malformed', () => {
    expect(listStateFromParams(new URLSearchParams(''))).toEqual(DEFAULT_LIST_STATE);
    expect(listStateFromParams(new URLSearchParams('page=3&q=dana&active=inactive'))).toEqual({
      page: 3,
      search: 'dana',
      active: 'inactive',
    });
    expect(listStateFromParams(new URLSearchParams('page=0&active=banana'))).toEqual(DEFAULT_LIST_STATE);
  });

  it.each([
    [null, 1],
    ['', 1],
    ['0', 1],
    ['-2', 1],
    ['2.5', 1],
    ['01', 1],
    ['abc', 1],
    ['99999999999999999999', 1],
    ['7', 7],
  ])('parsePage(%j) is %i', (raw, expected) => {
    expect(parsePage(raw)).toBe(expected);
  });

  it('parses only the three filter values', () => {
    expect(ACTIVE_FILTERS).toEqual(['all', 'active', 'inactive']);
    expect(parseActiveFilter('active')).toBe('active');
    expect(parseActiveFilter('inactive')).toBe('inactive');
    expect(parseActiveFilter('all')).toBe('all');
    expect(parseActiveFilter('true')).toBe('all');
    expect(parseActiveFilter(null)).toBe('all');
  });

  it('leaves default values out of the address, and round-trips the rest', () => {
    expect(listStateToParams(DEFAULT_LIST_STATE).toString()).toBe('');
    const state = { page: 2, search: '050-555', active: 'inactive' } as const;
    expect(listStateToParams(state).toString()).toBe('page=2&q=050-555&active=inactive');
    expect(listStateFromParams(listStateToParams(state))).toEqual(state);
  });

  it('builds the request of the list: a blank search and the all filter are not sent', () => {
    expect(customerListQuery(listStateToQuery(DEFAULT_LIST_STATE))).toBe('?page=1');
    expect(customerListQuery(listStateToQuery({ page: 1, search: '   ', active: 'all' }))).toBe('?page=1');
    const query = new URLSearchParams(
      customerListQuery(listStateToQuery({ page: 2, search: '(050) 555-66', active: 'inactive' })),
    );
    expect(query.get('page')).toBe('2');
    // The search text is sent as typed; the server removes spaces, hyphens and parentheses for phones (R3.10, R4.6).
    expect(query.get('search')).toBe('(050) 555-66');
    expect(query.get('active')).toBe('false');
    expect(new URLSearchParams(customerListQuery(listStateToQuery({ page: 1, search: '', active: 'active' }))).get('active')).toBe('true');
  });

  it('starts again at page 1 when the search or the filter changes, and keeps the rest on a page change', () => {
    const state = { page: 4, search: 'dana', active: 'active' } as const;
    expect(withSearch(state, 'levi')).toEqual({ page: 1, search: 'levi', active: 'active' });
    expect(withActive(state, 'inactive')).toEqual({ page: 1, search: 'dana', active: 'inactive' });
    expect(withPage(state, 5)).toEqual({ page: 5, search: 'dana', active: 'active' });
    expect(withPage(state, 0).page).toBe(1);
  });

  it('knows when a search or filter narrows the list', () => {
    expect(hasActiveFilters(DEFAULT_LIST_STATE)).toBe(false);
    expect(hasActiveFilters({ ...DEFAULT_LIST_STATE, search: ' x ' })).toBe(true);
    expect(hasActiveFilters({ ...DEFAULT_LIST_STATE, active: 'active' })).toBe(true);
  });
});

describe('pagination (R3.10: 50 per page)', () => {
  it('uses 50 per page', () => {
    expect(CUSTOMER_PAGE_SIZE).toBe(50);
  });

  it('computes pages, row range and controls', () => {
    expect(paginationState(2, 120, 50)).toMatchObject({ pages: 3, first: 51, last: 100, hasPrevious: true, hasNext: true, showControls: true });
    expect(paginationState(3, 120, 20)).toMatchObject({ pages: 3, first: 101, last: 120, hasPrevious: true, hasNext: false });
    expect(paginationState(1, 50, 50)).toMatchObject({ pages: 1, hasNext: false, showControls: false });
    expect(paginationState(1, 51, 50)).toMatchObject({ pages: 2, hasNext: true, showControls: true });
    expect(paginationState(1, 0, 0)).toMatchObject({ pages: 1, first: 0, last: 0, showControls: false });
  });

  it('sends a page past the end to the last page', () => {
    expect(pastTheEndPage(5, 120)).toBe(3);
    expect(pastTheEndPage(3, 120)).toBeNull();
    expect(pastTheEndPage(2, 0)).toBeNull();
    expect(pastTheEndPage(1, 0)).toBeNull();
  });
});

// --- rows and actions -------------------------------------------------------------------------------

describe('row actions (R3.7, R3.8, R3.9)', () => {
  it('offers deactivate to an active customer, reactivate to an inactive one, and reset to both', () => {
    expect(availableActions({ is_active: true })).toEqual(['deactivate', 'reset']);
    expect(availableActions({ is_active: false })).toEqual(['reactivate', 'reset']);
  });

  it('has a title, a prompt, a label and a confirmation text for every action', () => {
    const actions: CustomerAction[] = ['deactivate', 'reactivate', 'reset'];
    for (const action of actions) {
      const texts = actionTexts(action);
      for (const key of Object.values(texts)) expect(key).toMatch(/^management\.customers\./);
    }
  });

  it('shows the state as text and a missing city or apartment as a dash', () => {
    expect(stateKey({ is_active: true })).toBe('management.customers.active');
    expect(stateKey({ is_active: false })).toBe('management.customers.inactive');
    expect(cellText(null)).toBe(NO_VALUE);
    expect(cellText('  ')).toBe(NO_VALUE);
    expect(cellText('Haifa')).toBe('Haifa');
  });

  it('isolates a name inside a sentence', () => {
    expect(isolateName('Dana')).toBe('\u2068Dana\u2069');
  });
});

// --- the form ---------------------------------------------------------------------------------------

describe('form values and body (R3.2)', () => {
  it('builds the edit values from the customer, with blank text for an absent city or apartment', () => {
    expect(formValuesFromCustomer(dana)).toEqual({ phone: '0501234567', fullName: 'Dana Levi', city: 'Haifa', apartment: '' });
  });

  it('sends the phone and name as typed, and a blank city or apartment as null', () => {
    expect(toCustomerBody({ phone: '050-123 4567', fullName: ' Dana ', city: '  ', apartment: '' })).toEqual({
      phone: '050-123 4567',
      full_name: ' Dana ',
      city: null,
      apartment: null,
    });
    expect(toCustomerBody({ phone: '0501234567', fullName: 'Dana', city: 'Haifa', apartment: '4B' })).toMatchObject({
      city: 'Haifa',
      apartment: '4B',
    });
  });

  it('lists the form fields in the order the server reports them', () => {
    expect(CUSTOMER_FORM_ORDER).toEqual(['phone', 'full_name', 'city', 'apartment']);
    expect(FORM_FIELD_ORDER).toEqual(CUSTOMER_FORM_ORDER);
  });
});

describe('per-field errors (R3.3, R3.4, R3.5)', () => {
  it('puts every failing field of a 422 on its field', () => {
    const errors = customerFormErrors('invalid_phone', {
      phone: 'invalid_phone',
      full_name: 'invalid_full_name',
      city: 'invalid_city',
      apartment: 'invalid_apartment',
    });
    expect(errors).toEqual({
      fields: {
        phone: 'errors.invalid_phone',
        full_name: 'errors.invalid_full_name',
        city: 'errors.invalid_city',
        apartment: 'errors.invalid_apartment',
      },
      form: null,
    });
  });

  it('puts a lone invalid_phone and the 409 phone_already_registered on the phone field', () => {
    expect(customerFormErrors('invalid_phone')).toEqual({ fields: { phone: 'errors.invalid_phone' }, form: null });
    expect(customerFormErrors('phone_already_registered')).toEqual({
      fields: { phone: 'errors.phone_already_registered' },
      form: null,
    });
    expect(customerFormErrors('invalid_full_name')).toEqual({ fields: { full_name: 'errors.invalid_full_name' }, form: null });
  });

  it('keeps an error that belongs to no field on the form', () => {
    expect(customerFormErrors('service_unavailable')).toEqual({ fields: {}, form: 'errors.service_unavailable' });
    expect(customerFormErrors('customer_not_found')).toEqual({ fields: {}, form: 'errors.customer_not_found' });
    expect(customerFormErrors('generic', { unknown_field_name: 'x' })).toEqual({ fields: {}, form: 'errors.generic' });
    expect(customerFormErrors('network')).toEqual({ fields: {}, form: 'errors.generic' });
  });

  it('falls back to the generic text for a code the API does not define', () => {
    expect(errorTextKey('made_up')).toBe('errors.generic');
    expect(errorTextKey(undefined)).toBe('errors.generic');
    expect(customerFormErrors('invalid_phone', { phone: 'made_up' }).fields.phone).toBe('errors.generic');
  });

  it('focuses the first failing field in form order', () => {
    expect(firstFailingField(NO_FORM_ERRORS)).toBeNull();
    expect(firstFailingField(customerFormErrors('x', { apartment: 'invalid_apartment', city: 'invalid_city' }))).toBe('city');
    expect(firstFailingField(customerFormErrors('x', { full_name: 'invalid_full_name', phone: 'invalid_phone' }))).toBe('phone');
  });

  it('recognises an unknown customer', () => {
    expect(isCustomerGone('customer_not_found')).toBe(true);
    expect(isCustomerGone('invalid_phone')).toBe(false);
  });
});

// --- markup -----------------------------------------------------------------------------------------


describe('CustomersTable', () => {
  it.each(LANGUAGES)('shows every column, the state as text and row buttons that name the customer in %s', (language) => {
    const html = render(language, createElement(CustomersTable, { rows: [dana, sivan], onEdit: noop, onAction: noop }));
    expectNoRawKeys(html);
    expect(html).toContain('Dana Levi');
    expect(html).toContain('סיון כהן');
    expect(html).toContain('<bdi dir="ltr">0501234567</bdi>');
    expect(html).toContain('<bdi dir="ltr">0521112222</bdi>');
    expect(html.split(NO_VALUE)).toHaveLength(3); // one absent apartment, one absent city
    expect(html.match(/<th scope="col">/g)).toHaveLength(6);
    expect(html).toContain('pill pill--active');
    expect(html).toContain('pill pill--muted');
  });

  it('names the action and the customer on every row button, and offers the right action per state', () => {
    const html = render('en', createElement(CustomersTable, { rows: [dana, sivan], onEdit: noop, onAction: noop }));
    expect(html).toContain('aria-label="Edit: Dana Levi"');
    expect(html).toContain('aria-label="Deactivate: Dana Levi"');
    expect(html).toContain('aria-label="Reset password: Dana Levi"');
    expect(html).not.toContain('aria-label="Reactivate: Dana Levi"');
    expect(html).toContain('aria-label="Reactivate: סיון כהן"');
    expect(html).toContain('aria-label="Reset password: סיון כהן"');
    expect(html).not.toContain('aria-label="Deactivate: סיון כהן"');
    expect(html).toContain('>Active<');
    expect(html).toContain('>Inactive<');
  });
});

describe('CustomersToolbar', () => {
  it.each(LANGUAGES)('labels the search box and the filter, and lists the three filters in %s', (language) => {
    const html = render(
      language,
      createElement(CustomersToolbar, {
        searchDraft: 'dana',
        active: 'inactive',
        canClear: true,
        onSearchDraftChange: noop,
        onSearchSubmit: noop,
        onActiveChange: noop,
        onClear: noop,
      }),
    );
    expectNoRawKeys(html);
    expect(html).toContain('<label class="field__label" for="customers-search">');
    expect(html).toContain('<label class="field__label" for="customers-filter">');
    expect(html).toContain('role="search"');
    expect(html.match(/<option /g)).toHaveLength(3);
    expect(html).toContain('value="dana"');
    expect(html).toMatch(/<option value="inactive" selected/);
  });

  it('offers clear filters only when something narrows the list', () => {
    const props = {
      searchDraft: '',
      active: 'all' as const,
      onSearchDraftChange: noop,
      onSearchSubmit: noop,
      onActiveChange: noop,
      onClear: noop,
    };
    expect(render('en', createElement(CustomersToolbar, { ...props, canClear: false }))).not.toContain('Clear filters');
    expect(render('en', createElement(CustomersToolbar, { ...props, canClear: true }))).toContain('Clear filters');
  });
});

describe('CustomerFormView', () => {
  const view = (language: PortalLanguage, mode: 'create' | 'edit', errors: CustomerFormErrors, pending = false): string =>
    render(
      language,
      createElement(CustomerFormView, {
        mode,
        values: EMPTY_FORM_VALUES,
        errors,
        pending,
        onChange: noop,
        onSubmit: noop,
        onCancel: noop,
      }),
    );

  it.each(LANGUAGES)('gives every input a label and marks the required ones in text and programmatically in %s', (language) => {
    const html = view(language, 'create', NO_FORM_ERRORS);
    expectNoRawKeys(html);
    for (const id of ['phone', 'full_name', 'city', 'apartment']) {
      expect(html).toContain(`for="customer-form-${id}"`);
      expect(html).toContain(`id="customer-form-${id}"`);
    }
    expect(html.match(/aria-required="true"/g)).toHaveLength(2);
    expect(html.match(/aria-required="false"/g)).toHaveLength(2);
    expect(html).toContain('novalidate');
    expect(html.indexOf('customer-form-phone"')).toBeLessThan(html.indexOf('customer-form-full_name"'));
    expect(html.indexOf('customer-form-full_name"')).toBeLessThan(html.indexOf('customer-form-city"'));
    expect(html.indexOf('customer-form-city"')).toBeLessThan(html.indexOf('customer-form-apartment"'));
  });

  it('shows the required and optional markers in the label text', () => {
    const html = view('en', 'create', NO_FORM_ERRORS);
    expect(html).toMatch(/Phone number<span class="customers-marker"> \(required\)<\/span>/);
    expect(html).toMatch(/City<span class="customers-marker"> \(optional\)<\/span>/);
  });

  it('shows no error attributes before a refusal', () => {
    const html = view('en', 'create', NO_FORM_ERRORS);
    expect(html.match(/aria-invalid="false"/g)).toHaveLength(4);
    expect(html).not.toContain('aria-invalid="true"');
    expect(html).not.toContain('role="alert"');
  });

  it.each(LANGUAGES)('ties each field error to its input with aria-invalid and aria-describedby in %s', (language) => {
    const errors = customerFormErrors('invalid_phone', { phone: 'invalid_phone', full_name: 'invalid_full_name' });
    const html = view(language, 'create', errors);
    expectNoRawKeys(html);
    expect(html.match(/aria-invalid="true"/g)).toHaveLength(2);
    expect(html).toContain('aria-describedby="customer-form-phone-hint customer-form-phone-error"');
    expect(html).toContain('aria-describedby="customer-form-full_name-hint customer-form-full_name-error"');
    expect(html).toContain('aria-describedby="customer-form-city-hint"');
    expect(html).toContain('id="customer-form-phone-error"');
    expect(html).toContain('id="customer-form-full_name-error"');
    expect(html).not.toContain('id="customer-form-city-error"');
  });

  it('shows the phone_already_registered message on the phone field', () => {
    const html = view('en', 'create', customerFormErrors('phone_already_registered'));
    expect(html).toContain('id="customer-form-phone-error"');
    expect(html).toContain('This phone number is already registered to another customer.');
  });

  it('announces an error that belongs to no field as an alert', () => {
    const html = view('en', 'edit', customerFormErrors('service_unavailable'));
    expect(html).toContain('role="alert"');
    expect(html).toContain('The service is not available at the moment.');
    expect(html).not.toContain('aria-invalid="true"');
  });

  it('uses a telephone keypad, left to right, for the phone', () => {
    const html = view('he', 'create', NO_FORM_ERRORS);
    expect(html).toMatch(/<input[^>]*id="customer-form-phone"[^>]*type="tel"/);
    expect(html).toMatch(/<input[^>]*id="customer-form-phone"[^>]*dir="ltr"/);
    expect(html).toMatch(/<input[^>]*id="customer-form-phone"[^>]*inputMode="numeric"/);
  });

  it('names the initial password rule for a new customer only, and disables the form while saving', () => {
    expect(view('en', 'create', NO_FORM_ERRORS)).toContain('initial password 1234');
    expect(view('en', 'edit', NO_FORM_ERRORS)).not.toContain('initial password');
    const pending = view('en', 'edit', NO_FORM_ERRORS, true);
    expect(pending.match(/disabled=""/g)?.length).toBeGreaterThanOrEqual(6);
    expect(pending).toContain('Saving…');
  });
});

describe('CustomerConfirmView', () => {
  const confirm = (
    language: PortalLanguage,
    action: CustomerAction,
    extra: { pending?: boolean; error?: CustomerFormErrors['form']; gone?: boolean } = {},
  ): string =>
    render(
      language,
      createElement(CustomerConfirmView, {
        action,
        customer: dana,
        pending: extra.pending ?? false,
        error: extra.error ?? null,
        gone: extra.gone ?? false,
        onConfirm: noop,
        onDismiss: noop,
      }),
    );

  it.each(LANGUAGES)('names the customer for every action in %s', (language) => {
    for (const action of ['deactivate', 'reactivate', 'reset'] as const) {
      const html = confirm(language, action);
      expectNoRawKeys(html);
      expect(html).toContain('Dana Levi');
    }
  });

  it.each(LANGUAGES)('names the initial password rule for a reset, and shows no hash, in %s', (language) => {
    const html = confirm(language, 'reset');
    expect(html).toContain('1234');
    expect(html).not.toMatch(/hash/i);
    expect(html).not.toMatch(/[0-9a-f]{32}/i);
  });

  it('puts the safe choice, Cancel, after the confirm button and both ids in place for the focus start', () => {
    const html = confirm('en', 'deactivate');
    expect(html).toContain('id="customer-confirm-yes"');
    expect(html).toContain('id="customer-confirm-no"');
    expect(html).toContain('id="customer-confirm-prompt"');
    expect(html).toContain('>Deactivate<');
    expect(html).toContain('>Cancel<');
  });

  it('disables both buttons while the request is pending', () => {
    const html = confirm('en', 'reset', { pending: true });
    expect(html.match(/disabled=""/g)).toHaveLength(2);
    expect(html).toContain('Working…');
  });

  it('shows a refusal as an alert, and leaves only Close when the customer no longer exists (R3.14)', () => {
    const failed = confirm('en', 'deactivate', { error: 'errors.service_unavailable' });
    expect(failed).toContain('role="alert"');
    expect(failed).toContain('id="customer-confirm-yes"');
    const gone = confirm('en', 'deactivate', { error: 'errors.customer_not_found', gone: true });
    expect(gone).toContain('The customer was not found.');
    expect(gone).not.toContain('id="customer-confirm-yes"');
    expect(gone).toContain('>Close<');
  });
});

describe('CustomersPage', () => {
  const rows = [dana, sivan];
  const page = (total: number, pageNumber = 1, items: readonly StaffCustomer[] = rows): StaffCustomerPage => ({
    items,
    page: pageNumber,
    page_size: 50,
    total,
  });

  it.each(LANGUAGES)('shows the title, the new-customer button, the search and the table in %s', (language) => {
    const html = renderPage(language, '/tickets/customers', page(2));
    expectNoRawKeys(html);
    expect(html).toContain('<h1');
    expect(html).toContain('Dana Levi');
    expect(html).toContain('סיון כהן');
    expect(html).toContain('role="search"');
    expect(html).toContain('<table');
    // 50 per page: two customers need no page controls.
    expect(html).not.toContain('<nav class="pagination"');
  });

  it('shows the total in English and the page controls when the list needs more than one page', () => {
    const html = renderPage('en', '/tickets/customers', page(120));
    expect(html).toContain('Total: 120');
    expect(html).toContain('<nav class="pagination" aria-label="List pages"');
    expect(html).toContain('1–2 of 120');
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Previous<\/button>/);
    expect(html).toMatch(/<button type="button" class="button button--small">Next<\/button>/);
  });

  it('reads the page and the filter from the address', () => {
    const html = renderPage('en', '/tickets/customers?page=2&active=inactive', page(120, 2));
    expect(html).toMatch(/<option value="inactive" selected/);
    expect(html).toContain('Clear filters');
    expect(html).toMatch(/<button type="button" class="button button--small">Previous<\/button>/);
  });

  it('says so when nothing matches', () => {
    const html = renderPage('en', '/tickets/customers?q=zzz', page(0, 1, []));
    expect(html).toContain('No matching customers.');
    expect(html).toContain('Total: 0');
    expect(html).toContain('value="zzz"');
    expect(html).not.toContain('<table');
  });

  it('shows the loading text before the first answer', () => {
    const html = renderPage('en', '/tickets/customers', null);
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain('Loading…');
    expect(html).not.toContain('<table');
  });

  it('keeps the success notice region in the page for a screen reader', () => {
    const html = renderPage('en', '/tickets/customers', page(2));
    expect(html).toContain('role="status" aria-live="polite"');
  });
});
