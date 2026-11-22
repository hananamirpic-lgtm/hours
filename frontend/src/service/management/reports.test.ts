// Tests of the management report screen (task 24.6): the pure model and the markup of the view. No DOM and no
// testing library: `renderToStaticMarkup`. The container (`ReportsPage`) holds queries and is not rendered
// here; everything it decides is a pure function of `reportsModel`.
import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { describe, expect, it } from 'vitest';
import type {
  StaffCategory,
  StaffReport,
  StaffTicketRow,
  TicketStatus,
} from '../api/staffTypes';
import { reportQuery } from '../api/staff';
import { createPortalI18n, type PortalLanguage } from '../portal/portalI18n';
import en from '@/i18n/locales/en/service.json';
import he from '@/i18n/locales/he/service.json';
import {
  EMPTY_REPORT_FILTERS,
  INITIAL_REPORT,
  categoryCountEntries,
  hasActiveReportFilters,
  isEmptyReport,
  reportErrorField,
  reportErrorKey,
  sameReportRequest,
  shownReport,
  statusCountEntries,
  toReportQuery,
} from './reportsModel';
import { ReportsView, type ReportsViewProps } from './reportsView';
import { PAGE_SIZE } from './ticketListModel';

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

function report(items: StaffTicketRow[], overrides: Partial<StaffReport> = {}, pageNumber = 1): StaffReport {
  return {
    total: items.length,
    open: 2,
    by_status: { received: 1, in_progress: 1, closed: 1, cancelled: 1 },
    by_category: [
      { category: { id: 'c1', name_he: 'אינסטלציה', name_en: 'Plumbing' }, count: 3 },
      { category: { id: 'c2', name_he: 'חשמל ישן', name_en: 'Old wiring' }, count: 1 },
    ],
    tickets: { items, page: pageNumber, page_size: PAGE_SIZE, total: items.length },
    ...overrides,
  };
}

const EMPTY_REPORT: StaffReport = {
  total: 0,
  open: 0,
  by_status: { received: 0, in_progress: 0, closed: 0, cancelled: 0 },
  by_category: [],
  tickets: { items: [], page: 1, page_size: PAGE_SIZE, total: 0 },
};

// --- request ---------------------------------------------------------------------------------------------

describe('toReportQuery (R18.1)', () => {
  it('sends only the page when every filter is empty, so the report covers all tickets', () => {
    expect(toReportQuery(INITIAL_REPORT)).toEqual({ page: 1 });
    expect(reportQuery(toReportQuery(INITIAL_REPORT))).toBe('?page=1');
  });

  it('sends every set filter, with the city trimmed', () => {
    const applied = {
      filters: { dateFrom: '2025-01-01', dateTo: '2025-01-31', city: ' Haifa ', categoryId: 'c2' },
      page: 2,
    };
    expect(toReportQuery(applied)).toEqual({
      page: 2,
      dateFrom: '2025-01-01',
      dateTo: '2025-01-31',
      city: 'Haifa',
      categoryId: 'c2',
    });
    expect(reportQuery(toReportQuery(applied))).toBe(
      '?page=2&date_from=2025-01-01&date_to=2025-01-31&city=Haifa&category_id=c2',
    );
  });

  it('sends a single date as the same value in both fields, and leaves a blank city out', () => {
    const query = toReportQuery({
      filters: { ...EMPTY_REPORT_FILTERS, dateFrom: '2025-03-28', dateTo: '2025-03-28', city: '   ' },
      page: 1,
    });
    expect(query).toEqual({ page: 1, dateFrom: '2025-03-28', dateTo: '2025-03-28' });
  });

  it('knows when a re-submit repeats the request and when a filter is set', () => {
    expect(sameReportRequest(INITIAL_REPORT, { ...INITIAL_REPORT })).toBe(true);
    expect(sameReportRequest(INITIAL_REPORT, { ...INITIAL_REPORT, page: 2 })).toBe(false);
    expect(
      sameReportRequest(INITIAL_REPORT, { ...INITIAL_REPORT, filters: { ...EMPTY_REPORT_FILTERS, city: ' ' } }),
    ).toBe(true);
    expect(hasActiveReportFilters(EMPTY_REPORT_FILTERS)).toBe(false);
    expect(hasActiveReportFilters({ ...EMPTY_REPORT_FILTERS, categoryId: 'c1' })).toBe(true);
  });
});

// --- errors ----------------------------------------------------------------------------------------------

describe('report errors (R18.12)', () => {
  it('names the specific message for a rejected date range or filter, else the general one', () => {
    expect(reportErrorKey('invalid_date_range')).toBe('errors.invalid_date_range');
    expect(reportErrorKey('invalid_filter')).toBe('errors.invalid_filter');
    expect(reportErrorKey('network')).toBe('management.reports.loadError');
    expect(reportErrorKey('generic')).toBe('management.reports.loadError');
    expect(reportErrorField('invalid_date_range')).toBe('dates');
    expect(reportErrorField('invalid_filter')).toBe('filters');
    expect(reportErrorField('network')).toBeNull();
  });

  it('shows no figures after a failure, and the loaded report otherwise', () => {
    const loaded = report([row(10001, 'received')]);
    expect(shownReport(loaded, false)).toBe(loaded);
    expect(shownReport(loaded, true)).toBeNull();
    expect(shownReport(undefined, false)).toBeNull();
  });
});

// --- figures ---------------------------------------------------------------------------------------------

describe('figures (R18.7, R18.11)', () => {
  it('lists all four statuses in lifecycle order, with 0 for each one that has no tickets', () => {
    expect(statusCountEntries({ received: 4, in_progress: 0, closed: 2, cancelled: 0 })).toEqual([
      { status: 'received', count: 4 },
      { status: 'in_progress', count: 0 },
      { status: 'closed', count: 2 },
      { status: 'cancelled', count: 0 },
    ]);
    expect(statusCountEntries({ closed: 5 }).map((entry) => entry.count)).toEqual([0, 0, 5, 0]);
    expect(statusCountEntries(undefined).map((entry) => entry.count)).toEqual([0, 0, 0, 0]);
  });

  it('lists every category with a ticket, active or inactive, in the server order and in the language', () => {
    const counts = report([]).by_category;
    expect(categoryCountEntries(counts, 'en')).toEqual([
      { id: 'c1', name: 'Plumbing', count: 3 },
      { id: 'c2', name: 'Old wiring', count: 1 },
    ]);
    expect(categoryCountEntries(counts, 'he').map((entry) => entry.name)).toEqual(['אינסטלציה', 'חשמל ישן']);
  });

  it('leaves out a category whose count is 0, and handles no list', () => {
    const zero = [{ category: { id: 'c3', name_he: 'כללי', name_en: 'General' }, count: 0 }];
    expect(categoryCountEntries(zero, 'en')).toEqual([]);
    expect(categoryCountEntries(undefined, 'en')).toEqual([]);
  });

  it('calls a report with a total of 0 empty', () => {
    expect(isEmptyReport(EMPTY_REPORT)).toBe(true);
    expect(isEmptyReport({ total: 1 })).toBe(false);
  });
});

// --- markup ----------------------------------------------------------------------------------------------

const CATEGORIES: StaffCategory[] = [
  { id: 'c1', name_he: 'אינסטלציה', name_en: 'Plumbing', is_active: true },
  { id: 'c2', name_he: 'חשמל ישן', name_en: 'Old wiring', is_active: false },
];

function view(overrides: Partial<ReportsViewProps> = {}): ReportsViewProps {
  return {
    language: 'en',
    draft: EMPTY_REPORT_FILTERS,
    onDraftChange: () => undefined,
    onSubmit: () => undefined,
    onClear: () => undefined,
    categories: CATEGORIES,
    report: null,
    busy: false,
    errorCode: null,
    onRetry: () => undefined,
    onPageChange: () => undefined,
    ...overrides,
  };
}

function render(language: PortalLanguage, props: ReportsViewProps): string {
  const i18n = createPortalI18n(language);
  const element: ReactElement = createElement(ReportsView, props);
  return renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n },
      createElement(StaticRouter, { location: '/tickets/reports' }, element),
    ),
  );
}

const text = { en, he };

/** The `<dd>` value that follows a `<dt>` label. */
function figure(html: string, label: string): string {
  const match = new RegExp(`<dt>(?:<bdi>)?${label}(?:</bdi>)?</dt><dd class="reports__value"><bdi dir="ltr">(\\d+)</bdi>`).exec(
    html,
  );
  if (match === null) throw new Error(`no figure for ${label}`);
  return match[1] ?? '';
}

/** The `<tr>` markup of one ticket. */
function rowHtml(html: string, number: number): string {
  const match = html.match(new RegExp(`<tr>(?:(?!</tr>).)*?>${number}</bdi>.*?</tr>`, 's'));
  if (match === null) throw new Error(`no row for ${number}`);
  return match[0];
}

describe('ReportsView', () => {
  const items = [
    row(10001, 'received', true),
    row(10002, 'in_progress', false, { assigned_technician_name: 'Moshe' }),
    row(10003, 'closed'),
    row(10004, 'cancelled', true),
  ];
  const loaded = report(items, { total: 120, tickets: { items, page: 1, page_size: PAGE_SIZE, total: 120 } });
  const html = render('en', view({ report: loaded }));

  it('labels every filter and offers inactive categories (R18.1, R18.6, R23.12)', () => {
    for (const label of ['From date', 'To date', 'City', 'Category', 'All categories', 'Show report', 'Clear filters']) {
      expect(html).toContain(label);
    }
    expect(html).toContain('Plumbing');
    expect(html).toContain('Old wiring (inactive)');
    const inputs = html.match(/<(input|select)\b[^>]*>/g) ?? [];
    expect(inputs).toHaveLength(4);
    for (const tag of inputs) {
      const id = /id="([^"]+)"/.exec(tag)?.[1] ?? '';
      expect(html).toContain(`for="${id}"`);
    }
  });

  it('shows the total, the open figure and the four status counts (R18.7)', () => {
    expect(figure(html, 'Total tickets')).toBe('120');
    expect(figure(html, 'Open tickets')).toBe('2');
    expect(figure(html, 'Received')).toBe('1');
    expect(figure(html, 'In Progress')).toBe('1');
    expect(figure(html, 'Closed')).toBe('1');
    expect(figure(html, 'Cancelled')).toBe('1');
  });

  it('shows the count per category that has tickets (R18.7)', () => {
    expect(figure(html, 'Plumbing')).toBe('3');
    expect(figure(html, 'Old wiring')).toBe('1');
    const hebrew = render('he', view({ language: 'he', report: loaded }));
    expect(figure(hebrew, 'אינסטלציה')).toBe('3');
    expect(figure(hebrew, text.he.status.received)).toBe('1');
  });

  it('shows the row fields of the ticket list, with the time in Jerusalem and a link to the detail (R18.7)', () => {
    const second = rowHtml(html, 10002);
    expect(second).toContain('href="/tickets/list/10002"');
    expect(second).toContain('15/01/2025 12:30');
    expect(second).toContain('Dana Levi');
    expect(second).toContain('Haifa');
    expect(second).toContain('>12<');
    expect(second).toContain('Plumbing');
    expect(second).toContain('In Progress');
    expect(second).toContain('Moshe');
    expect(rowHtml(render('he', view({ language: 'he', report: loaded })), 10002)).toContain('אינסטלציה');
  });

  it('marks an urgent ticket with an icon and the text SOS, and no other ticket (R15.2, R23.12)', () => {
    for (const number of [10001, 10004]) {
      const urgent = rowHtml(html, number);
      expect(urgent).toContain('>SOS<');
      expect(urgent).toContain('aria-hidden="true"');
    }
    expect(rowHtml(html, 10002)).not.toContain('SOS');
  });

  it('pages the table 25 per page (R18.7)', () => {
    const second = report(items, { total: 60, tickets: { items, page: 2, page_size: PAGE_SIZE, total: 60 } });
    const paged = render('en', view({ report: second }));
    expect(paged).toContain('26–29 of 60');
    expect(paged).toContain('Previous');
    expect(paged).toContain('Next');
    expect(html).toContain('1–4 of 120');
  });

  it('shows a total of 0, every status as 0 and the no-match message in place of the table (R18.11)', () => {
    for (const language of ['en', 'he'] as const) {
      const empty = render(language, view({ language, report: EMPTY_REPORT }));
      const t = text[language];
      expect(figure(empty, t.management.reports.total)).toBe('0');
      expect(figure(empty, t.management.reports.open)).toBe('0');
      for (const status of ['received', 'in_progress', 'closed', 'cancelled'] as const) {
        expect(figure(empty, t.status[status])).toBe('0');
      }
      expect(empty).toContain(t.management.reports.empty);
      expect(empty).not.toContain('<table');
      expect(empty).not.toContain('pagination');
    }
  });

  it('shows an error and keeps the entered filters (R18.12)', () => {
    const entered = { dateFrom: '2025-02-10', dateTo: '2025-02-01', city: 'Haifa', categoryId: 'c2' };
    for (const language of ['en', 'he'] as const) {
      const failed = render(language, view({ language, draft: entered, errorCode: 'network' }));
      expect(failed).toContain('role="alert"');
      expect(failed).toContain(text[language].management.reports.loadError);
      expect(failed).toContain('value="2025-02-10"');
      expect(failed).toContain('value="2025-02-01"');
      expect(failed).toContain('value="Haifa"');
      expect(failed).toMatch(/<option value="c2" selected/);
      expect(failed).not.toContain('<table');
    }
  });

  it('names a rejected date range and ties it to the date fields (R18.4, R23.12)', () => {
    const failed = render('en', view({ errorCode: 'invalid_date_range' }));
    expect(failed).toContain(text.en.errors.invalid_date_range);
    expect(failed.match(/aria-invalid="true"/g)).toHaveLength(2);
    const errorId = /<div class="feedback feedback--error reports__alert" role="alert" id="([^"]+)"/.exec(failed)?.[1];
    expect(errorId).toBeTruthy();
    expect(failed.match(new RegExp(`aria-describedby="${errorId}"`, 'g'))).toHaveLength(2);
  });

  it('shows the loading text only while there is nothing to show', () => {
    expect(render('en', view({ busy: true }))).toContain('Loading…');
    expect(render('en', view({ busy: true, errorCode: 'network' }))).not.toContain('Loading…');
  });
});
