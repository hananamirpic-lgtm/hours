// Tests of the management ticket list (task 24.3): the pure model and the markup of the view. No DOM and no
// testing library: `renderToStaticMarkup`. The container (`TicketListPage`) holds queries and timers and is
// not rendered here; everything it decides is a pure function of `ticketListModel`.
import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { describe, expect, it } from 'vitest';
import type { StaffCategory, StaffTicketPage, StaffTicketRow, TicketStatus } from '../api/staffTypes';
import { createPortalI18n, type PortalLanguage } from '../portal/portalI18n';
import en from '@/i18n/locales/en/service.json';
import he from '@/i18n/locales/he/service.json';
import { dashboardListFilters, dashboardListTarget } from './dashboardModel';
import {
  EMPTY_FILTERS,
  INITIAL_LIST,
  PAGE_SIZE,
  allowedStatusTargets,
  categoryName,
  clampPage,
  displayedPage,
  formatJerusalemMinute,
  hasActiveFilters,
  isUrgentOpen,
  listErrorField,
  listErrorKey,
  listLanguage,
  listStateFromParams,
  pageInfo,
  replaceRow,
  rowClassName,
  sameRequest,
  statusActionKey,
  statusActions,
  statusFailureReasonKey,
  ticketDetailPath,
  toListQuery,
  toggleStatus,
  type TicketListFilters,
} from './ticketListModel';
import { TicketListView, type TicketListViewProps } from './ticketListView';

const STATUSES: TicketStatus[] = ['received', 'in_progress', 'closed', 'cancelled'];

function row(number: number, status: TicketStatus, urgent = false, extra: Partial<StaffTicketRow> = {}): StaffTicketRow {
  return {
    ticket_number: number,
    created_at: '2025-01-15T10:30:45Z',
    customer_name: 'Dana Levi',
    city: 'Haifa',
    apartment: '12',
    category: { id: 'c1', name_he: 'אינסטלציה', name_en: 'Plumbing' },
    status,
    assigned_technician_name: '',
    urgent,
    ...extra,
  };
}

function page(items: StaffTicketRow[], total = items.length, pageNumber = 1): StaffTicketPage {
  return { items, page: pageNumber, page_size: PAGE_SIZE, total };
}

// --- status lifecycle ----------------------------------------------------------------------------------

describe('allowedStatusTargets (R14.2, R15.9)', () => {
  it('offers exactly the moves Requirement 14 allows Ticket_Staff', () => {
    expect(allowedStatusTargets('received')).toEqual(['in_progress']);
    expect(allowedStatusTargets('in_progress')).toEqual(['closed']);
    expect(allowedStatusTargets('closed')).toEqual(['in_progress']);
  });

  it('offers nothing from cancelled (R15.11), and never cancelled or the current status as a target', () => {
    expect(allowedStatusTargets('cancelled')).toEqual([]);
    for (const status of STATUSES) {
      expect(allowedStatusTargets(status)).not.toContain('cancelled');
      expect(allowedStatusTargets(status)).not.toContain(status);
    }
  });

  it('names each move with the existing action label and refuses a move that is not allowed', () => {
    expect(statusActionKey('received', 'in_progress')).toBe('management.tickets.actions.toInProgress');
    expect(statusActionKey('in_progress', 'closed')).toBe('management.tickets.actions.close');
    expect(statusActionKey('closed', 'in_progress')).toBe('management.tickets.actions.reopen');
    expect(statusActionKey('received', 'closed')).toBeNull();
    expect(statusActionKey('cancelled', 'in_progress')).toBeNull();
    expect(statusActions('cancelled')).toEqual([]);
    expect(statusActions('received')).toEqual([
      { target: 'in_progress', labelKey: 'management.tickets.actions.toInProgress' },
    ]);
  });
});

describe('urgent rows (R15.7)', () => {
  it('highlights only an urgent ticket that is received or in progress', () => {
    expect(isUrgentOpen({ urgent: true, status: 'received' })).toBe(true);
    expect(isUrgentOpen({ urgent: true, status: 'in_progress' })).toBe(true);
    expect(isUrgentOpen({ urgent: true, status: 'closed' })).toBe(false);
    expect(isUrgentOpen({ urgent: true, status: 'cancelled' })).toBe(false);
    expect(isUrgentOpen({ urgent: false, status: 'received' })).toBe(false);
    expect(rowClassName({ urgent: true, status: 'received' })).toContain('ticket-list__row--urgent-open');
    expect(rowClassName({ urgent: false, status: 'received' })).not.toContain('urgent-open');
  });
});

// --- time ------------------------------------------------------------------------------------------------

describe('formatJerusalemMinute (R15.2)', () => {
  it('shows the Asia/Jerusalem wall clock to the minute, in winter (UTC+2) and summer (UTC+3)', () => {
    expect(formatJerusalemMinute('2025-01-15T10:30:45Z')).toBe('15/01/2025 12:30');
    expect(formatJerusalemMinute('2025-07-15T10:30:45Z')).toBe('15/07/2025 13:30');
  });

  it('moves to the next Jerusalem day and writes midnight as 00, not 24', () => {
    expect(formatJerusalemMinute('2025-01-15T22:05:00Z')).toBe('16/01/2025 00:05');
  });

  it('follows the daylight-saving change of 2025-03-28 (clocks forward at 02:00)', () => {
    expect(formatJerusalemMinute('2025-03-27T23:59:00Z')).toBe('28/03/2025 01:59');
    expect(formatJerusalemMinute('2025-03-28T00:00:00Z')).toBe('28/03/2025 03:00');
  });

  it('reads a value without a zone as UTC and a value with an offset as that instant', () => {
    expect(formatJerusalemMinute('2025-01-15T10:30:45')).toBe('15/01/2025 12:30');
    expect(formatJerusalemMinute('2025-01-15T12:30:45+02:00')).toBe('15/01/2025 12:30');
    expect(formatJerusalemMinute('2025-01-15T10:30:45.123456+00:00')).toBe('15/01/2025 12:30');
  });

  it('gives an empty string for an unreadable value', () => {
    expect(formatJerusalemMinute('')).toBe('');
    expect(formatJerusalemMinute('not a date')).toBe('');
  });
});

// --- request ---------------------------------------------------------------------------------------------

describe('toListQuery (R15.3, R15.5, R15.8)', () => {
  it('sends only the page when nothing is set', () => {
    expect(toListQuery(INITIAL_LIST)).toEqual({ page: 1 });
  });

  it('sends every set value, the statuses in lifecycle order, and the order toggle', () => {
    const filters: TicketListFilters = {
      search: ' 12345 ',
      statuses: ['closed', 'received'],
      categoryId: 'cat-1',
      city: ' Haifa ',
      urgentOnly: true,
      dateFrom: '2025-01-01',
      dateTo: '2025-01-31',
    };
    expect(toListQuery({ filters, urgentFirst: true, page: 3 })).toEqual({
      page: 3,
      search: ' 12345 ',
      status: ['received', 'closed'],
      categoryId: 'cat-1',
      city: 'Haifa',
      urgentOnly: true,
      dateFrom: '2025-01-01',
      dateTo: '2025-01-31',
      urgentFirst: true,
    });
  });

  it('leaves blank text out, and sends a search the server must reject exactly as typed', () => {
    expect(toListQuery({ ...INITIAL_LIST, filters: { ...EMPTY_FILTERS, search: '   ', city: ' ' } })).toEqual({
      page: 1,
    });
    expect(toListQuery({ ...INITIAL_LIST, filters: { ...EMPTY_FILTERS, search: '12a45' } }).search).toBe('12a45');
  });

  it('knows when a re-submit repeats the request, and that the order toggle is not a filter', () => {
    expect(sameRequest(INITIAL_LIST, { ...INITIAL_LIST })).toBe(true);
    expect(sameRequest(INITIAL_LIST, { ...INITIAL_LIST, urgentFirst: true })).toBe(false);
    expect(hasActiveFilters(EMPTY_FILTERS)).toBe(false);
    expect(hasActiveFilters({ ...EMPTY_FILTERS, urgentOnly: true })).toBe(true);
    expect(hasActiveFilters({ ...EMPTY_FILTERS, search: ' ' })).toBe(false);
  });

  it('toggles a status on and off in lifecycle order', () => {
    expect(toggleStatus([], 'closed')).toEqual(['closed']);
    expect(toggleStatus(['closed'], 'received')).toEqual(['received', 'closed']);
    expect(toggleStatus(['received', 'closed'], 'received')).toEqual(['closed']);
  });
});

describe('listStateFromParams (R17.4, R15.5)', () => {
  const fromSearch = (search: string) => listStateFromParams(new URLSearchParams(search));

  it('opens with no filters for an empty query', () => {
    expect(fromSearch('')).toEqual({ draft: EMPTY_FILTERS, applied: INITIAL_LIST });
  });

  it('reads the three dashboard links into the draft and the applied filters', () => {
    const open = fromSearch('?status=received&status=in_progress');
    expect(open.draft.statuses).toEqual(['received', 'in_progress']);
    expect(open.draft.urgentOnly).toBe(false);
    expect(fromSearch('?status=in_progress').draft.statuses).toEqual(['in_progress']);
    const urgent = fromSearch('?status=received&status=in_progress&urgent_only=true');
    expect(urgent.draft).toEqual({ ...EMPTY_FILTERS, statuses: ['received', 'in_progress'], urgentOnly: true });
    expect(urgent.applied.filters).toEqual(urgent.draft);
    expect(urgent.applied.page).toBe(1);
  });

  it.each(['open', 'inProgress', 'urgent'] as const)('round-trips the dashboard link of %s to its request', (key) => {
    const { search } = dashboardListTarget(key);
    expect(toListQuery(listStateFromParams(new URLSearchParams(search)).applied)).toEqual({
      ...dashboardListFilters(key),
      page: 1,
    });
  });

  it('reads the other filters, the order toggle and the page with the names of the API', () => {
    const state = fromSearch(
      '?search=12345&category_id=c1&city=Haifa&date_from=2025-01-02&date_to=2025-02-03&urgent_first=true&page=3&status=closed',
    );
    expect(state.draft).toEqual({
      search: '12345',
      statuses: ['closed'],
      categoryId: 'c1',
      city: 'Haifa',
      urgentOnly: false,
      dateFrom: '2025-01-02',
      dateTo: '2025-02-03',
    });
    expect(state.applied).toEqual({ filters: state.draft, urgentFirst: true, page: 3 });
  });

  it('ignores unknown statuses, duplicates, other flag values, bad dates and bad pages', () => {
    const state = fromSearch(
      '?status=bogus&status=closed&status=closed&status=&urgent_only=1&urgent_first=yes&date_from=2025-02-30&date_to=tomorrow&page=0',
    );
    expect(state.draft).toEqual({ ...EMPTY_FILTERS, statuses: ['closed'] });
    expect(state.applied.urgentFirst).toBe(false);
    expect(state.applied.page).toBe(1);
    expect(fromSearch('?page=-2').applied.page).toBe(1);
    expect(fromSearch('?page=2.5').applied.page).toBe(1);
    expect(fromSearch('?page=abc').applied.page).toBe(1);
    expect(fromSearch('?urgent_only=true&status=cancelled').draft.urgentOnly).toBe(true);
  });
});

describe('paging (R15.1)', () => {
  it('uses 25 per page and reports the range', () => {
    expect(PAGE_SIZE).toBe(25);
    expect(pageInfo(1, 0)).toEqual({ pages: 1, first: 0, last: 0, hasPrevious: false, hasNext: false });
    expect(pageInfo(1, 25)).toMatchObject({ pages: 1, first: 1, last: 25, hasNext: false });
    expect(pageInfo(2, 26)).toMatchObject({ pages: 2, first: 26, last: 26, hasPrevious: true, hasNext: false });
    expect(pageInfo(1, 51)).toMatchObject({ pages: 3, hasNext: true });
  });

  it('moves a page past the end back to the last page', () => {
    expect(clampPage(5, 51)).toBe(3);
    expect(clampPage(2, 51)).toBe(2);
    expect(clampPage(4, 0)).toBe(1);
  });
});

// --- errors and rows -------------------------------------------------------------------------------------

describe('list errors (R15.4, R15.6, R15.12)', () => {
  it('names the specific message for a rejected search, date range or filter, else the general one', () => {
    expect(listErrorKey('invalid_ticket_number')).toBe('errors.invalid_ticket_number');
    expect(listErrorKey('invalid_date_range')).toBe('errors.invalid_date_range');
    expect(listErrorKey('invalid_filter')).toBe('errors.invalid_filter');
    expect(listErrorKey('network')).toBe('management.tickets.loadError');
    expect(listErrorKey('generic')).toBe('management.tickets.loadError');
    expect(listErrorField('invalid_ticket_number')).toBe('search');
    expect(listErrorField('invalid_date_range')).toBe('dates');
    expect(listErrorField('network')).toBeNull();
  });

  it('keeps the last loaded page when the new request has no data', () => {
    const last = page([row(10001, 'received')]);
    expect(displayedPage(undefined, last)).toBe(last);
    const loaded = page([row(10002, 'closed')]);
    expect(displayedPage(loaded, last)).toBe(loaded);
    expect(displayedPage(undefined, null)).toBeNull();
  });

  it('gives a reason for a refused status change and falls back to the general error', () => {
    expect(statusFailureReasonKey('invalid_transition')).toBe('errors.invalid_transition');
    expect(statusFailureReasonKey('ticket_status_changed')).toBe('errors.ticket_status_changed');
    expect(statusFailureReasonKey('network')).toBe('errors.generic');
  });
});

describe('rows', () => {
  it('replaces only the row of the reloaded ticket, keeps the total, and ignores a ticket not on the page', () => {
    const before = page([row(10001, 'received'), row(10002, 'closed')], 40);
    const after = replaceRow(before, { ...row(10001, 'in_progress'), assigned_technician_name: 'Moshe' });
    expect(after.items.map((item) => item.status)).toEqual(['in_progress', 'closed']);
    expect(after.items[0]?.assigned_technician_name).toBe('Moshe');
    expect(after.total).toBe(40);
    expect(replaceRow(before, row(99999, 'closed'))).toBe(before);
  });

  it('drops the extra fields of a ticket detail', () => {
    const detail = { ...row(10001, 'closed'), phone: '0501234567', history: [] };
    const after = replaceRow(page([row(10001, 'in_progress')]), detail);
    expect(after.items[0]).not.toHaveProperty('phone');
    expect(after.items[0]).not.toHaveProperty('history');
  });

  it('shows the category name in the language, falling back to the other when it is blank', () => {
    const category = { name_he: 'אינסטלציה', name_en: 'Plumbing' };
    expect(categoryName(category, 'he')).toBe('אינסטלציה');
    expect(categoryName(category, 'en')).toBe('Plumbing');
    expect(categoryName({ name_he: ' ', name_en: 'Plumbing' }, 'he')).toBe('Plumbing');
    expect(listLanguage('he-IL')).toBe('he');
    expect(listLanguage('en')).toBe('en');
    expect(ticketDetailPath(12345)).toBe('/tickets/list/12345');
  });
});

// --- markup ----------------------------------------------------------------------------------------------

const CATEGORIES: StaffCategory[] = [
  { id: 'c1', name_he: 'אינסטלציה', name_en: 'Plumbing', is_active: true },
  { id: 'c2', name_he: 'חשמל ישן', name_en: 'Old wiring', is_active: false },
];

function view(overrides: Partial<TicketListViewProps> = {}): TicketListViewProps {
  return {
    language: 'en',
    draft: EMPTY_FILTERS,
    onDraftChange: () => undefined,
    onSubmit: () => undefined,
    onClear: () => undefined,
    urgentFirst: false,
    onUrgentFirstChange: () => undefined,
    categories: CATEGORIES,
    page: page([]),
    busy: false,
    listErrorCode: null,
    onRetry: () => undefined,
    statusFailure: null,
    onDismissFailure: () => undefined,
    pendingTickets: new Set<number>(),
    onChangeStatus: () => undefined,
    onPageChange: () => undefined,
    ...overrides,
  };
}

function render(language: PortalLanguage, element: ReactElement): string {
  const i18n = createPortalI18n(language);
  return renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n },
      createElement(StaticRouter, { location: '/tickets/list' }, element),
    ),
  );
}

const text = { en, he };

/** The `<tr>` markup of one ticket. */
function rowHtml(html: string, number: number): string {
  const match = html.match(new RegExp(`<tr class="[^"]*">(?:(?!</tr>).)*?>${number}</bdi>.*?</tr>`, 's'));
  if (match === null) throw new Error(`no row for ${number}`);
  return match[0];
}

describe('TicketListView', () => {
  const items = [
    row(10001, 'received', true),
    row(10002, 'in_progress', false, { assigned_technician_name: 'Moshe' }),
    row(10003, 'closed', true),
    row(10004, 'cancelled', true),
  ];
  const html = render('en', createElement(TicketListView, view({ page: page(items, 120) })));

  it('shows the total of matching tickets and no empty message (R15.1)', () => {
    expect(html).toContain('Total: 120');
    expect(html).not.toContain(text.en.management.tickets.empty);
    expect(html).toContain('1–4 of 120');
  });

  it('shows the no-match message when the total is 0 (R15.1)', () => {
    const empty = render('en', createElement(TicketListView, view({ page: page([], 0) })));
    expect(empty).toContain('Total: 0');
    expect(empty).toContain(text.en.management.tickets.empty);
    expect(empty).not.toContain('<table');
  });

  it('shows the columns of R15.2 with the time in Jerusalem and a link to the detail', () => {
    const first = rowHtml(html, 10002);
    expect(first).toContain('href="/tickets/list/10002"');
    expect(first).toContain('15/01/2025 12:30');
    expect(first).toContain('Dana Levi');
    expect(first).toContain('Haifa');
    expect(first).toContain('Plumbing');
    expect(first).toContain('In Progress');
    expect(first).toContain('Moshe');
    expect(rowHtml(render('he', createElement(TicketListView, view({ language: 'he', page: page(items) }))), 10002)).toContain(
      'אינסטלציה',
    );
  });

  it('marks every urgent ticket with an icon and the text SOS, and highlights only urgent open rows (R15.7)', () => {
    for (const number of [10001, 10003, 10004]) {
      const urgent = rowHtml(html, number);
      expect(urgent).toContain('ticket-list__sos');
      expect(urgent).toContain('>SOS<');
      expect(urgent).toContain('aria-hidden="true"');
    }
    expect(rowHtml(html, 10002)).not.toContain('SOS');
    expect(rowHtml(html, 10001)).toContain('ticket-list__row--urgent-open');
    expect(rowHtml(html, 10002)).not.toContain('urgent-open');
    expect(rowHtml(html, 10003)).not.toContain('urgent-open');
    expect(rowHtml(html, 10004)).not.toContain('urgent-open');
  });

  it('offers only the allowed targets per row, and nothing for a cancelled ticket (R15.9, R15.11)', () => {
    expect(rowHtml(html, 10001)).toContain('Move to in progress');
    expect(rowHtml(html, 10001)).not.toContain('Mark as resolved');
    expect(rowHtml(html, 10002)).toContain('Mark as resolved');
    expect(rowHtml(html, 10002)).not.toContain('Move to in progress');
    expect(rowHtml(html, 10003)).toContain('Reopen');
    const cancelled = rowHtml(html, 10004);
    expect(cancelled).not.toContain('<button');
    expect(cancelled).toContain('Cancelled');
    expect(cancelled).toContain('No actions');
  });

  it('gives each action button an accessible name with the ticket number and disables a pending ticket', () => {
    expect(html).toContain('aria-label="Mark as resolved, ticket 10002"');
    const pending = render('en', createElement(TicketListView, view({ page: page(items), pendingTickets: new Set([10002]) })));
    expect(rowHtml(pending, 10002)).toMatch(/<button[^>]*disabled/);
    expect(rowHtml(pending, 10001)).not.toMatch(/<button[^>]*disabled/);
  });

  it('labels every input and offers inactive categories (R15.5, R23.12)', () => {
    expect(html).toContain('Search by ticket number');
    expect(html).toContain('Plumbing');
    expect(html).toContain('Old wiring (inactive)');
    expect(html).toContain('From date');
    expect(html).toContain('To date');
    expect(html).toContain('Urgent only');
    expect(html).toContain('Urgent open tickets first');
    for (const status of ['Received', 'In Progress', 'Resolved', 'Cancelled']) {
      expect(html).toContain(`<span>${status}</span>`);
    }
    const inputs = html.match(/<(input|select)\b[^>]*>/g) ?? [];
    expect(inputs.length).toBeGreaterThan(8);
    // Every input with an id has a label for it; checkboxes sit inside their label.
    for (const tag of inputs.filter((input) => input.includes(' id='))) {
      const id = /id="([^"]+)"/.exec(tag)?.[1] ?? '';
      expect(html).toContain(`for="${id}"`);
    }
  });

  it('keeps the order toggle off by default and on when set (R15.8)', () => {
    const toggle = (checked: boolean) =>
      render('en', createElement(TicketListView, view({ urgentFirst: checked, page: page(items) })));
    const around = (markup: string) => /<input type="checkbox"([^>]*)\/><span>Urgent open tickets first/.exec(markup)?.[1] ?? '';
    expect(around(toggle(false))).not.toContain('checked');
    expect(around(toggle(true))).toContain('checked');
  });

  it('shows a list error and keeps the rows already displayed (R15.4, R15.12)', () => {
    const failed = render(
      'en',
      createElement(TicketListView, view({ page: page(items, 4), listErrorCode: 'invalid_ticket_number' })),
    );
    expect(failed).toContain('role="alert"');
    expect(failed).toContain(text.en.errors.invalid_ticket_number);
    expect(failed).toContain('aria-invalid="true"');
    expect(rowHtml(failed, 10001)).toContain('10001');
    const network = render('en', createElement(TicketListView, view({ page: page(items, 4), listErrorCode: 'network' })));
    expect(network).toContain(text.en.management.tickets.loadError);
    expect(rowHtml(network, 10002)).toContain('Moshe');
  });

  it('names the failed change and the reason in the error (R15.10)', () => {
    const failed = render(
      'en',
      createElement(
        TicketListView,
        view({
          page: page(items),
          statusFailure: { ticketNumber: 10002, from: 'in_progress', target: 'closed', code: 'ticket_status_changed' },
        }),
      ),
    );
    expect(failed).toContain('The action &quot;Mark as resolved&quot; on ticket 10002 failed.');
    expect(failed).toContain(text.en.errors.ticket_status_changed);
  });

  it('shows the loading text only while nothing has been loaded, and shows paging controls for several pages', () => {
    const loading = render('en', createElement(TicketListView, view({ page: null, busy: true })));
    expect(loading).toContain('Loading…');
    const paged = render('en', createElement(TicketListView, view({ page: page(items, 60, 2) })));
    expect(paged).toContain('26–29 of 60');
    expect(paged).toContain('Previous');
    expect(paged).toContain('Next');
  });
});
