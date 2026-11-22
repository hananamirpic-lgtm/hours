// View model of the Customers screen (task 24.5; R3.1 to R3.5, R3.10, R3.12, R3.14, R23.12).
// Pure: no React, no DOM, no i18n instance, no network. The screen turns the keys and values below into text.
//
// What lives here: the list state kept in the address (page, search text, state filter), the pagination
// arithmetic, the row actions a customer allows, the form values and the mapping of an API refusal to
// per-field messages. The server stays the authority for every rule (phone normalisation, length limits,
// uniqueness); nothing here repeats a rule except to choose which field a code belongs to.
import { isErrorCode, type ErrorCode } from '../api/errorCodes';
import type { CustomerBody, CustomerListQuery, StaffCustomer } from '../api/staffTypes';

// --- list state in the address (R3.10) --------------------------------------------------------------

/** 50 customers per page (R3.10). The server decides the size; this is used for the page arithmetic. */
export const CUSTOMER_PAGE_SIZE = 50;

/** The state filter: every customer (the default), active only, inactive only. */
export type ActiveFilter = 'all' | 'active' | 'inactive';

export const ACTIVE_FILTERS: readonly ActiveFilter[] = ['all', 'active', 'inactive'];

export interface CustomersListState {
  /** The page shown, 1-based. */
  readonly page: number;
  /** The search text as applied (the server trims it and compares names and phones). Empty means none. */
  readonly search: string;
  readonly active: ActiveFilter;
}

export const DEFAULT_LIST_STATE: CustomersListState = { page: 1, search: '', active: 'all' };

/** The longest search text the box accepts. Names stop at 100 characters, so more cannot match anything. */
export const SEARCH_MAX_LENGTH = 100;

const POSITIVE_INTEGER = /^[1-9][0-9]*$/;

/** A page number from the `page` parameter: a plain positive integer, otherwise page 1. */
export function parsePage(raw: string | null | undefined): number {
  if (raw === null || raw === undefined || !POSITIVE_INTEGER.test(raw)) return 1;
  const page = Number(raw);
  return Number.isSafeInteger(page) ? page : 1;
}

/** The state filter from the `active` parameter; anything unknown is `all`. */
export function parseActiveFilter(raw: string | null | undefined): ActiveFilter {
  return raw === 'active' || raw === 'inactive' ? raw : 'all';
}

/** The list state held in the search parameters `page`, `q` and `active`. */
export function listStateFromParams(params: URLSearchParams): CustomersListState {
  return {
    page: parsePage(params.get('page')),
    search: (params.get('q') ?? '').slice(0, SEARCH_MAX_LENGTH),
    active: parseActiveFilter(params.get('active')),
  };
}

/** The search parameters of a state. A default value is left out, so the first view has the plain address. */
export function listStateToParams(state: CustomersListState): URLSearchParams {
  const params = new URLSearchParams();
  if (state.page > 1) params.set('page', String(state.page));
  if (state.search.trim() !== '') params.set('q', state.search);
  if (state.active !== 'all') params.set('active', state.active);
  return params;
}

/** The request of `GET /staff/customers` for a state. A blank search and the `all` filter are not sent. */
export function listStateToQuery(state: CustomersListState): CustomerListQuery {
  const query: { page: number; search?: string; active?: boolean } = { page: state.page };
  if (state.search.trim() !== '') query.search = state.search;
  if (state.active === 'active') query.active = true;
  if (state.active === 'inactive') query.active = false;
  return query;
}

/** True when a search text or a state filter narrows the list (the "clear filters" control is shown). */
export function hasActiveFilters(state: CustomersListState): boolean {
  return state.search.trim() !== '' || state.active !== 'all';
}

/** A changed search or filter starts again at page 1; only a page change keeps the other values. */
export function withSearch(state: CustomersListState, search: string): CustomersListState {
  return { ...state, page: 1, search };
}

export function withActive(state: CustomersListState, active: ActiveFilter): CustomersListState {
  return { ...state, page: 1, active };
}

export function withPage(state: CustomersListState, page: number): CustomersListState {
  return { ...state, page: Math.max(1, Math.floor(page)) };
}

// --- pagination -------------------------------------------------------------------------------------

export interface PaginationState {
  readonly page: number;
  /** At least 1, also for an empty list. */
  readonly pages: number;
  readonly total: number;
  readonly hasPrevious: boolean;
  readonly hasNext: boolean;
  /** The first and last row number shown, both 0 for an empty page. */
  readonly first: number;
  readonly last: number;
  /** Page controls are shown when the list does not fit one page. */
  readonly showControls: boolean;
}

/** The pagination of the shown `page`, the `total` of matches and the number of rows on this page. */
export function paginationState(page: number, total: number, rowCount: number): PaginationState {
  const safeTotal = Number.isFinite(total) && total > 0 ? Math.floor(total) : 0;
  const pages = Math.max(1, Math.ceil(safeTotal / CUSTOMER_PAGE_SIZE));
  const first = rowCount > 0 ? (page - 1) * CUSTOMER_PAGE_SIZE + 1 : 0;
  return {
    page,
    pages,
    total: safeTotal,
    hasPrevious: page > 1,
    hasNext: page < pages,
    first,
    last: rowCount > 0 ? first + rowCount - 1 : 0,
    showControls: safeTotal > CUSTOMER_PAGE_SIZE,
  };
}

/**
 * Where to go when the shown page is past the last one (an address typed by hand, or the last row of the last
 * page removed by a filter change): the last page. Null while the page exists or nothing matches at all.
 */
export function pastTheEndPage(page: number, total: number): number | null {
  if (!Number.isFinite(total) || total <= 0) return null;
  const pages = Math.ceil(total / CUSTOMER_PAGE_SIZE);
  return page > pages ? pages : null;
}

// --- rows and row actions (R3.7, R3.8, R3.9) --------------------------------------------------------

/** What a confirmed action does to a customer. Edit has no confirmation; it opens the form. */
export type CustomerAction = 'deactivate' | 'reactivate' | 'reset';

/** The confirmed actions a row offers: an active customer can be deactivated, an inactive one reactivated; both can be reset. */
export function availableActions(customer: Pick<StaffCustomer, 'is_active'>): readonly CustomerAction[] {
  return customer.is_active ? ['deactivate', 'reset'] : ['reactivate', 'reset'];
}

export interface ActionTexts {
  /** The label of the row button, and of the confirm button. */
  readonly labelKey: string;
  readonly titleKey: string;
  /** The confirmation text. It takes `{{name}}`. */
  readonly promptKey: string;
  /** The confirmation of success, shown on the page. */
  readonly doneKey: string;
}

const ACTION_TEXTS: Readonly<Record<CustomerAction, ActionTexts>> = {
  deactivate: {
    labelKey: 'management.customers.deactivate',
    titleKey: 'management.customers.deactivateTitle',
    promptKey: 'management.customers.deactivatePrompt',
    doneKey: 'management.customers.deactivated',
  },
  reactivate: {
    labelKey: 'management.customers.reactivate',
    titleKey: 'management.customers.reactivateTitle',
    promptKey: 'management.customers.reactivatePrompt',
    doneKey: 'management.customers.reactivated',
  },
  // The reset prompt names the initial password rule (R3.9); no hash or password value ever comes from the API (R3.12).
  reset: {
    labelKey: 'management.customers.resetPassword',
    titleKey: 'management.customers.resetTitle',
    promptKey: 'management.customers.resetPrompt',
    doneKey: 'management.customers.resetDone',
  },
};

export function actionTexts(action: CustomerAction): ActionTexts {
  return ACTION_TEXTS[action];
}

/**
 * A customer's name placed inside a sentence of the other direction: wrapped in first-strong isolates so a
 * Latin name inside a Hebrew sentence (or the reverse) keeps its own order and does not reorder the words around it.
 */
export function isolateName(name: string): string {
  return `\u2068${name}\u2069`;
}

/** The text shown for a city or an apartment number the customer does not have. */
export const NO_VALUE = '\u2014';

/** A city or apartment cell: the value, or the placeholder when absent or blank. */
export function cellText(value: string | null | undefined): string {
  return value === null || value === undefined || value.trim() === '' ? NO_VALUE : value;
}

/** The state of a row as text keys, so it is never shown by colour alone (R23.12). */
export function stateKey(customer: Pick<StaffCustomer, 'is_active'>): 'management.customers.active' | 'management.customers.inactive' {
  return customer.is_active ? 'management.customers.active' : 'management.customers.inactive';
}

// --- the form (R3.2, R3.3, R3.5) --------------------------------------------------------------------

/** The four fields of the form, in the order the server checks and reports them (and the order on screen). */
export type CustomerFieldName = 'phone' | 'full_name' | 'city' | 'apartment';

export const CUSTOMER_FORM_ORDER: readonly CustomerFieldName[] = ['phone', 'full_name', 'city', 'apartment'];

export interface CustomerFormValues {
  readonly phone: string;
  readonly fullName: string;
  readonly city: string;
  readonly apartment: string;
}

export const EMPTY_FORM_VALUES: CustomerFormValues = { phone: '', fullName: '', city: '', apartment: '' };

/** The values of the edit form. The phone is the stored, normalised one. */
export function formValuesFromCustomer(customer: StaffCustomer): CustomerFormValues {
  return {
    phone: customer.phone,
    fullName: customer.full_name,
    city: customer.city ?? '',
    apartment: customer.apartment ?? '',
  };
}

/**
 * The body of a create or an edit. The phone and the name go as typed: the server removes the allowed phone
 * separators and trims the name (R3.2, R4), and answers with the field codes when they fail. A blank city or
 * apartment goes as null, which the server stores as absent.
 */
export function toCustomerBody(values: CustomerFormValues): CustomerBody {
  return {
    phone: values.phone,
    full_name: values.fullName,
    city: values.city.trim() === '' ? null : values.city,
    apartment: values.apartment.trim() === '' ? null : values.apartment,
  };
}

/** `errors.<code>` for a code the API defines, `errors.generic` for anything else. */
export type ErrorTextKey = `errors.${ErrorCode}` | 'errors.generic';

export function errorTextKey(code: string | null | undefined): ErrorTextKey {
  return isErrorCode(code) ? `errors.${code}` : 'errors.generic';
}

export interface CustomerFormErrors {
  /** The text key of each failing field. */
  readonly fields: Readonly<Partial<Record<CustomerFieldName, ErrorTextKey>>>;
  /** A refusal that belongs to no single field (a server error, an unknown customer). Null when there is none. */
  readonly form: ErrorTextKey | null;
}

export const NO_FORM_ERRORS: CustomerFormErrors = { fields: {}, form: null };

/** The field a code belongs to when the answer names no field (a 409 carries only the code). */
const CODE_FIELD: Readonly<Record<string, CustomerFieldName>> = {
  invalid_phone: 'phone',
  phone_already_registered: 'phone',
  invalid_full_name: 'full_name',
  invalid_city: 'city',
  invalid_apartment: 'apartment',
};

function isFieldName(name: string): name is CustomerFieldName {
  return (CUSTOMER_FORM_ORDER as readonly string[]).includes(name);
}

/**
 * The messages of a refused create or edit (R3.3, R3.4, R3.5). A 422 names every failing field with its code
 * (`fields`), and each becomes the message of that field. The 409 `phone_already_registered` and a lone
 * `invalid_phone` carry only the code, and the code says which field it is. Any other code, and an answer whose
 * fields are all unknown, is a message for the whole form.
 */
export function customerFormErrors(code: string, fields: Readonly<Record<string, string>> = {}): CustomerFormErrors {
  const byField: Partial<Record<CustomerFieldName, ErrorTextKey>> = {};
  for (const [name, fieldCode] of Object.entries(fields)) {
    if (isFieldName(name)) byField[name] = errorTextKey(fieldCode);
  }
  if (Object.keys(byField).length > 0) return { fields: byField, form: null };
  const field = CODE_FIELD[code];
  if (field !== undefined) return { fields: { [field]: errorTextKey(code) }, form: null };
  return { fields: {}, form: errorTextKey(code) };
}

/** The first failing field in form order, to take keyboard focus after a refusal (R23.12). */
export function firstFailingField(errors: CustomerFormErrors): CustomerFieldName | null {
  return CUSTOMER_FORM_ORDER.find((name) => errors.fields[name] !== undefined) ?? null;
}

/** True when the refusal means the customer no longer exists, so the list is out of date (R3.14). */
export function isCustomerGone(code: string): boolean {
  return code === 'customer_not_found';
}
