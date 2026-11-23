// Tests of the ticket list screen (task 23.2): the pure view model, and server-rendered markup of the
// presentational `TicketListView` and of the container with a filled cache. No DOM, no testing library.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { describe, expect, it } from 'vitest';
import enService from '../../../i18n/locales/en/service.json';
import heService from '../../../i18n/locales/he/service.json';
import { PortalApiError } from '../../api/portalFetch';
import { isTicketPage, ticketKeys } from '../../api/tickets';
import type { TicketPage, TicketRow } from '../../api/ticketTypes';
import type { TicketStatus } from '../../types';
import { createPortalI18n, type PortalLanguage } from '../portalI18n';
import { TicketListScreen, TicketListView, type TicketListViewProps } from './TicketListScreen';
import {
  ANNOUNCEMENT_GAP_MS,
  APARTMENT_LABEL_KEY,
  EMPTY_VALUE,
  PAGE_SIZE,
  POLL_INTERVAL_MS,
  TICKET_LIST_QUERY_OPTIONS,
  announcementTexts,
  apartmentText,
  categoryName,
  formatCreatedDate,
  listViewState,
  pageSearch,
  paginationState,
  parsePageParam,
  pastTheEndPage,
  pollAnnouncements,
  pollInterval,
  scheduleAnnouncements,
  shouldFallBackToFirstPage,
  statusTextKey,
  ticketPath,
  urgentMarkerKey,
  type StatusSnapshot,
} from './ticketListModel';

const STATUSES: TicketStatus[] = ['received', 'in_progress', 'closed', 'cancelled'];

function row(number: number, status: TicketStatus = 'received', extra: Partial<TicketRow> = {}): TicketRow {
  return {
    ticket_number: number,
    category: { id: `c-${number}`, name_he: 'אינסטלציה', name_en: 'Plumbing' },
    created_date: '2024-03-05',
    apartment: '12',
    status,
    urgent: false,
    ...extra,
  };
}

function page(items: TicketRow[], total = items.length, pageNumber = 1): TicketPage {
  return { items, page: pageNumber, page_size: PAGE_SIZE, total };
}

// --- model: page parameter ------------------------------------------------------------------------

describe('parsePageParam', () => {
  it('treats a missing parameter as page 1 without a notice', () => {
    expect(parsePageParam(null)).toEqual({ page: 1, valid: true });
    expect(parsePageParam(undefined)).toEqual({ page: 1, valid: true });
  });

  it.each(['1', '2', '17', '100000'])('accepts %s', (raw) => {
    expect(parsePageParam(raw)).toEqual({ page: Number(raw), valid: true });
  });

  it.each(['0', '-1', 'x', '2.5', '', ' 2', '2 ', '1e2', '01', '+2', '0x10', '99999999999999999999'])(
    'treats %j as page 1 and says so',
    (raw) => {
      expect(parsePageParam(raw)).toEqual({ page: 1, valid: false });
    },
  );
});

describe('pageSearch and ticketPath', () => {
  it('leaves page 1 without a parameter', () => {
    expect(pageSearch(1)).toBe('');
    expect(pageSearch(2)).toBe('?page=2');
    expect(pageSearch(0)).toBe('');
  });

  it('builds the detail path relative to the basename', () => {
    expect(ticketPath(10000)).toBe('/tickets/10000');
  });
});

// --- model: row fields ----------------------------------------------------------------------------

describe('formatCreatedDate', () => {
  it('rearranges YYYY-MM-DD into DD/MM/YYYY', () => {
    expect(formatCreatedDate('2024-03-05')).toBe('05/03/2024');
    expect(formatCreatedDate('1999-12-31')).toBe('31/12/1999');
    expect(formatCreatedDate('2025-01-01')).toBe('01/01/2025');
  });

  it('keeps the leap day and rejects a leap day in a common year', () => {
    expect(formatCreatedDate('2024-02-29')).toBe('29/02/2024');
    expect(formatCreatedDate('2000-02-29')).toBe('29/02/2000');
    expect(formatCreatedDate('2023-02-29')).toBe('2023-02-29');
    expect(formatCreatedDate('1900-02-29')).toBe('1900-02-29');
  });

  it('pads single-digit month and day', () => {
    expect(formatCreatedDate('2024-1-5')).toBe('05/01/2024');
  });

  it('returns a value that is not a date unchanged', () => {
    for (const bad of ['', 'x', '2024-13-01', '2024-00-10', '2024-04-31', '05/03/2024', '2024-03-05T10:00:00Z']) {
      expect(formatCreatedDate(bad)).toBe(bad);
    }
  });

  it('does not depend on the time zone (the day never shifts)', () => {
    expect(formatCreatedDate('2024-03-31')).toBe('31/03/2024');
    expect(formatCreatedDate('2024-10-27')).toBe('27/10/2024');
  });
});

describe('categoryName', () => {
  const category = { name_he: 'חשמל', name_en: 'Electric' };

  it('uses the name in the current language', () => {
    expect(categoryName(category, 'he')).toBe('חשמל');
    expect(categoryName(category, 'en')).toBe('Electric');
  });

  it('falls back to the other language when the name is empty', () => {
    expect(categoryName({ name_he: '', name_en: 'Electric' }, 'he')).toBe('Electric');
    expect(categoryName({ name_he: 'חשמל', name_en: '' }, 'en')).toBe('חשמל');
  });
});

describe('apartmentText', () => {
  it('shows the apartment stored on the ticket as text, trimmed', () => {
    expect(apartmentText({ apartment: '12' })).toBe('12');
    expect(apartmentText({ apartment: ' 4ב ' })).toBe('4ב');
  });

  it('shows a dash for an empty apartment', () => {
    expect(EMPTY_VALUE).toBe('—');
    expect(apartmentText({ apartment: '' })).toBe(EMPTY_VALUE);
    expect(apartmentText({ apartment: '   ' })).toBe(EMPTY_VALUE);
  });

  it('labels the column with the locale field name, present in both languages', () => {
    expect(APARTMENT_LABEL_KEY).toBe('fields.apartment');
    expect(enService.fields.apartment).not.toBe('');
    expect(heService.fields.apartment).not.toBe('');
  });
});

describe('status and urgent marker', () => {
  it('has a text in both locale files for every status key', () => {
    for (const status of STATUSES) {
      expect(statusTextKey(status)).toBe(`status.${status}`);
      expect(enService.status[status]).not.toBe('');
      expect(heService.status[status]).not.toBe('');
    }
  });

  it('gives the urgent text key for an urgent ticket in every status, and none otherwise', () => {
    for (const status of STATUSES) {
      expect(urgentMarkerKey(row(10000, status, { urgent: true }))).toBe('portal.urgent.marker');
      expect(urgentMarkerKey(row(10000, status))).toBeNull();
    }
    expect(enService.portal.urgent.marker).not.toBe('');
    expect(heService.portal.urgent.marker).not.toBe('');
  });
});

// --- model: pagination ----------------------------------------------------------------------------

describe('paginationState', () => {
  it.each([
    [0, 1, false],
    [1, 1, false],
    [50, 1, false],
    [51, 2, true],
    [100, 2, true],
    [101, 3, true],
  ])('total %i gives %i page(s), controls shown: %s', (total, pages, controls) => {
    const state = paginationState(1, total);
    expect(state.pages).toBe(pages);
    expect(state.showControls).toBe(controls);
    expect(state.hasPrevious).toBe(false);
    expect(state.hasNext).toBe(pages > 1);
  });

  it('gives previous and next pages in the middle and at the ends', () => {
    expect(paginationState(2, 101)).toMatchObject({ hasPrevious: true, hasNext: true, previousPage: 1, nextPage: 3 });
    expect(paginationState(3, 101)).toMatchObject({ hasPrevious: true, hasNext: false, previousPage: 2, nextPage: null });
    expect(paginationState(1, 101)).toMatchObject({ hasPrevious: false, hasNext: true, previousPage: null, nextPage: 2 });
  });

  it('never reports fewer than one page and ignores a bad total', () => {
    expect(paginationState(1, -5).pages).toBe(1);
    expect(paginationState(1, Number.NaN).pages).toBe(1);
  });

  it('finds the page to show when the address is past the end', () => {
    expect(pastTheEndPage(1, 0)).toBeNull();
    expect(pastTheEndPage(3, 0)).toBe(1);
    expect(pastTheEndPage(2, 100)).toBeNull();
    expect(pastTheEndPage(3, 100)).toBe(2);
    expect(pastTheEndPage(9, 101)).toBe(3);
  });
});

describe('shouldFallBackToFirstPage', () => {
  it('falls back on 422 invalid_page and on 404 when a later page was asked for', () => {
    expect(shouldFallBackToFirstPage(new PortalApiError(422, 'invalid_page'), 3)).toBe(true);
    expect(shouldFallBackToFirstPage(new PortalApiError(404, 'generic'), 3)).toBe(true);
  });

  it('does not fall back on page 1 or on other failures', () => {
    expect(shouldFallBackToFirstPage(new PortalApiError(422, 'invalid_page'), 1)).toBe(false);
    expect(shouldFallBackToFirstPage(new PortalApiError(422, 'invalid_category'), 3)).toBe(false);
    expect(shouldFallBackToFirstPage(new PortalApiError(500, 'generic'), 3)).toBe(false);
    expect(shouldFallBackToFirstPage(new PortalApiError(0, 'network'), 3)).toBe(false);
    expect(shouldFallBackToFirstPage(new Error('x'), 3)).toBe(false);
    expect(shouldFallBackToFirstPage(null, 3)).toBe(false);
  });
});

// --- model: what the screen shows -----------------------------------------------------------------

describe('listViewState', () => {
  it('shows loading while nothing is loaded and nothing failed', () => {
    expect(listViewState({ data: undefined, isError: false })).toEqual({ content: 'loading', rows: [], showError: false });
  });

  it('shows only the error when nothing is loaded and the request failed', () => {
    expect(listViewState({ data: undefined, isError: true })).toEqual({ content: 'none', rows: [], showError: true });
  });

  it('shows the empty state when the customer owns no ticket', () => {
    expect(listViewState({ data: page([], 0), isError: false })).toEqual({ content: 'empty', rows: [], showError: false });
  });

  it('keeps the loaded rows when a reload fails and drops the error when it succeeds', () => {
    const data = page([row(10002), row(10001)], 2);
    const failed = listViewState({ data, isError: true });
    expect(failed.content).toBe('rows');
    expect(failed.rows).toHaveLength(2);
    expect(failed.showError).toBe(true);
    expect(listViewState({ data, isError: false }).showError).toBe(false);
  });

  it('does not call a page past the end empty', () => {
    expect(listViewState({ data: page([], 120, 5), isError: false }).content).toBe('loading');
  });
});

describe('query options', () => {
  it('polls every 30 seconds, not in the background, and on every focus', () => {
    expect(TICKET_LIST_QUERY_OPTIONS.refetchInterval).toBe(30_000);
    expect(TICKET_LIST_QUERY_OPTIONS.refetchIntervalInBackground).toBe(false);
    expect(TICKET_LIST_QUERY_OPTIONS.refetchOnWindowFocus).toBe('always');
    expect(typeof TICKET_LIST_QUERY_OPTIONS.placeholderData).toBe('function');
  });

  it('restarts the timer after each load by alternating the interval by one millisecond', () => {
    expect(pollInterval(0)).toBe(POLL_INTERVAL_MS);
    expect(pollInterval(1)).toBe(POLL_INTERVAL_MS + 1);
    expect(pollInterval(2)).toBe(POLL_INTERVAL_MS);
    expect(pollInterval(7)).toBe(POLL_INTERVAL_MS + 1);
  });
});

describe('isTicketPage', () => {
  it('accepts the list response and rejects other shapes', () => {
    expect(isTicketPage(page([row(10000)]))).toBe(true);
    expect(isTicketPage(page([]))).toBe(true);
    expect(isTicketPage(null)).toBe(false);
    expect(isTicketPage({ items: [{ ticket_number: 1 }], page: 1, page_size: 50, total: 1 })).toBe(false);
    expect(isTicketPage({ ...page([row(10000, 'received')]), total: '1' })).toBe(false);
    expect(isTicketPage(page([{ ...row(10000), status: 'open' as TicketStatus }]))).toBe(false);
  });

  it('requires the apartment as a string on every row (an empty string is a value)', () => {
    expect(isTicketPage(page([row(10000, 'received', { apartment: '' })]))).toBe(true);
    const withoutApartment: Record<string, unknown> = { ...row(10000) };
    delete withoutApartment.apartment;
    expect(isTicketPage({ ...page([]), items: [withoutApartment] })).toBe(false);
    expect(isTicketPage({ ...page([]), items: [{ ...row(10000), apartment: 12 }] })).toBe(false);
    expect(isTicketPage({ ...page([]), items: [{ ...row(10000), apartment: null }] })).toBe(false);
  });

  it('keeps its query keys under the portal root', () => {
    expect(ticketKeys.list(2)).toEqual(['portal', 'tickets', 'list', 2]);
    expect(ticketKeys.list(2).slice(0, 3)).toEqual([...ticketKeys.lists]);
  });
});

// --- model: announcements -------------------------------------------------------------------------

describe('pollAnnouncements', () => {
  it('announces nothing on the first load and keeps the snapshot', () => {
    const result = pollAnnouncements(null, 1, [row(10002, 'received'), row(10001, 'closed')]);
    expect(result.announcements).toEqual([]);
    expect(result.snapshot.page).toBe(1);
    expect(result.snapshot.entries).toHaveLength(2);
  });

  it('announces nothing when the data is unchanged', () => {
    const rows = [row(10002), row(10001, 'in_progress')];
    const first = pollAnnouncements(null, 1, rows);
    expect(pollAnnouncements(first.snapshot, 1, rows).announcements).toEqual([]);
  });

  it('announces one changed status', () => {
    const first = pollAnnouncements(null, 1, [row(10002), row(10001)]);
    const second = pollAnnouncements(first.snapshot, 1, [row(10002), row(10001, 'in_progress')]);
    expect(second.announcements).toEqual([{ ticketNumber: 10001, status: 'in_progress' }]);
  });

  it('announces several changes, one per ticket in ticket-number order', () => {
    const first = pollAnnouncements(null, 1, [row(10003), row(10002), row(10001)]);
    const second = pollAnnouncements(first.snapshot, 1, [row(10003, 'closed'), row(10002), row(10001, 'cancelled')]);
    expect(second.announcements).toEqual([
      { ticketNumber: 10001, status: 'cancelled' },
      { ticketNumber: 10003, status: 'closed' },
    ]);
  });

  it('does not announce a ticket that is new on the page', () => {
    const first = pollAnnouncements(null, 1, [row(10001)]);
    expect(pollAnnouncements(first.snapshot, 1, [row(10002), row(10001)]).announcements).toEqual([]);
  });

  it('does not announce on a page change and starts a new snapshot', () => {
    const first = pollAnnouncements(null, 1, [row(10001, 'received')]);
    const moved = pollAnnouncements(first.snapshot, 2, [row(10001, 'closed')]);
    expect(moved.announcements).toEqual([]);
    expect(moved.snapshot.page).toBe(2);
    const next = pollAnnouncements(moved.snapshot, 2, [row(10001, 'cancelled')]);
    expect(next.announcements).toEqual([{ ticketNumber: 10001, status: 'cancelled' }]);
  });

  it('keeps announcing across a run of polls', () => {
    let snapshot: StatusSnapshot | null = null;
    const heard: number[] = [];
    const loads: TicketRow[][] = [
      [row(10001, 'received')],
      [row(10001, 'received')],
      [row(10001, 'in_progress')],
      [row(10001, 'in_progress')],
      [row(10001, 'closed')],
    ];
    for (const rows of loads) {
      const result = pollAnnouncements(snapshot, 1, rows);
      snapshot = result.snapshot;
      heard.push(result.announcements.length);
    }
    expect(heard).toEqual([0, 0, 1, 0, 1]);
  });
});

describe('announcementTexts and scheduleAnnouncements', () => {
  it.each<[PortalLanguage]>([['he'], ['en']])('writes one text per changed status in %s', (language) => {
    const i18n = createPortalI18n(language);
    const texts = announcementTexts(
      [
        { ticketNumber: 10001, status: 'in_progress' },
        { ticketNumber: 10002, status: 'closed' },
      ],
      (key, values) => (values === undefined ? i18n.t(key) : i18n.t(key, values)),
    );
    const service = language === 'he' ? heService : enService;
    expect(texts).toHaveLength(2);
    expect(texts[0]).toBe(
      service.a11y.live.statusChanged.replace('{{number}}', '10001').replace('{{status}}', service.status.in_progress),
    );
    expect(texts[1]).toContain('10002');
    expect(texts[1]).toContain(service.status.closed);
  });

  it('spaces several messages apart, the first one at once', () => {
    expect(scheduleAnnouncements([])).toEqual([]);
    expect(scheduleAnnouncements(['a', 'b', 'c'])).toEqual([
      { text: 'a', delayMs: 0 },
      { text: 'b', delayMs: ANNOUNCEMENT_GAP_MS },
      { text: 'c', delayMs: 2 * ANNOUNCEMENT_GAP_MS },
    ]);
  });
});

// --- markup ---------------------------------------------------------------------------------------

function i18nFor(language: PortalLanguage) {
  const instance = createPortalI18n(language);
  return instance;
}

const NOOP = (): void => undefined;

function viewProps(overrides: Partial<TicketListViewProps> & { data?: TicketPage; isError?: boolean; pageNumber?: number }): TicketListViewProps {
  const data = overrides.data;
  const state = overrides.state ?? listViewState({ data, isError: overrides.isError ?? false });
  return {
    state,
    pagination: overrides.pagination ?? paginationState(overrides.pageNumber ?? 1, data?.total ?? 0),
    fallbackPage: overrides.fallbackPage ?? null,
    onRetry: NOOP,
    onRefresh: NOOP,
    headingRef: null,
  };
}

function renderView(language: PortalLanguage, props: TicketListViewProps): string {
  return renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n: i18nFor(language) },
      createElement(StaticRouter, { location: '/service', basename: '/service' }, createElement(TicketListView, props)),
    ),
  );
}

const SAMPLE = page(
  [
    row(10003, 'in_progress', { urgent: true }),
    row(10002, 'closed'),
    row(10001, 'cancelled', { urgent: true, category: { id: 'c', name_he: 'חשמל', name_en: 'Electric' } }),
  ],
  3,
);

describe('TicketListView markup', () => {
  it.each<[PortalLanguage, 'rtl' | 'ltr']>([
    ['he', 'rtl'],
    ['en', 'ltr'],
  ])('renders the rows in %s with direction %s', (language, direction) => {
    const html = renderView(language, viewProps({ data: SAMPLE }));
    const service = language === 'he' ? heService : enService;
    expect(html).toContain(`dir="${direction}"`);
    expect(html).toContain(`lang="${language}"`);
    expect(html).toContain(`<h1`);
    expect(html).toContain(service.portal.list.title);
    // Caption and column headers.
    expect(html).toContain(`<caption class="portal-visually-hidden">${service.portal.list.tableLabel}</caption>`);
    expect(html).toContain(service.portal.list.number);
    expect(html).toContain(service.portal.list.category);
    expect(html).toContain(service.portal.list.created);
    expect(html).toContain(service.portal.list.status);
    // Three rows in the order given, each number in an LTR isolate and a link to its detail.
    expect(html.match(/<tr[ >]/g)).toHaveLength(4);
    for (const number of [10003, 10002, 10001]) {
      expect(html).toContain(`<bdi dir="ltr">${number}</bdi>`);
      expect(html).toContain(`href="/service/tickets/${number}"`);
    }
    expect(html.indexOf('10003')).toBeLessThan(html.indexOf('10002'));
    expect(html.indexOf('10002')).toBeLessThan(html.indexOf('10001'));
    // Date as DD/MM/YYYY, also isolated.
    expect(html).toContain('<bdi dir="ltr">05/03/2024</bdi>');
    // Category in the current language.
    expect(html).toContain(language === 'he' ? 'אינסטלציה' : 'Plumbing');
    expect(html).toContain(language === 'he' ? 'חשמל' : 'Electric');
    expect(html).not.toContain(language === 'he' ? 'Plumbing' : 'אינסטלציה');
  });

  it.each<[PortalLanguage]>([['he'], ['en']])('prints every status as text and urgent as icon plus text in %s', (language) => {
    const service = language === 'he' ? heService : enService;
    const rows = STATUSES.map((status, index) => row(10010 + index, status, { urgent: index % 2 === 0 }));
    const html = renderView(language, viewProps({ data: page(rows) }));
    for (const status of STATUSES) {
      expect(html).toContain(`>${service.status[status]}</span>`);
      expect(html).toContain(`portal-status--${status}`);
    }
    // Urgent rows 0 and 2: marker text appears twice, each with a hidden icon; the other rows have none.
    expect(html.split(`<span>${service.portal.urgent.marker}</span>`)).toHaveLength(3);
    expect(html.match(/<svg[^>]*aria-hidden="true"/g)).toHaveLength(2);
    expect(html.match(/<svg/g)).toHaveLength(2);
  });

  it.each<[PortalLanguage]>([['he'], ['en']])(
    'shows the apartment column after the date and before the status in %s, a dash when empty',
    (language) => {
      const service = language === 'he' ? heService : enService;
      const rows = [row(10002, 'received', { apartment: '7ב' }), row(10001, 'received', { apartment: '' })];
      const html = renderView(language, viewProps({ data: page(rows) }));
      const header = html.slice(html.indexOf('<thead'), html.indexOf('</thead>'));
      const headers = [...header.matchAll(/<th[^>]*>([^<]*)<\/th>/g)].map((match) => match[1]);
      expect(headers).toEqual([
        service.portal.list.number,
        service.portal.list.category,
        service.portal.list.created,
        service.fields.apartment,
        service.portal.list.status,
      ]);
      expect(html).toContain(`data-label="${service.fields.apartment}"><bdi>7ב</bdi></td>`);
      expect(html).toContain(`data-label="${service.fields.apartment}"><bdi>${EMPTY_VALUE}</bdi></td>`);
      const firstRow = html.slice(html.indexOf('10002'), html.indexOf('10001'));
      expect(firstRow.indexOf('05/03/2024')).toBeLessThan(firstRow.indexOf('7ב'));
      expect(firstRow.indexOf('7ב')).toBeLessThan(firstRow.indexOf('portal-status'));
    },
  );

  it('puts an aria-label with the ticket number on each detail link', () => {
    const html = renderView('en', viewProps({ data: page([row(12345)]) }));
    expect(html).toContain('aria-label="Open ticket 12345"');
  });

  it('shows the empty state with a control to open a new ticket', () => {
    for (const language of ['he', 'en'] as const) {
      const service = language === 'he' ? heService : enService;
      const html = renderView(language, viewProps({ data: page([], 0) }));
      expect(html).toContain(service.portal.list.empty);
      expect(html).toContain(`href="/service/new"`);
      expect(html).toContain(service.portal.list.emptyAction);
      expect(html).not.toContain('<table');
      expect(html).not.toContain('<nav');
    }
  });

  it('shows the loading text and no table while nothing is loaded', () => {
    const html = renderView('en', viewProps({}));
    expect(html).toContain(enService.portal.loading);
    expect(html).not.toContain('<table');
    expect(html).not.toContain('portal-list-error');
    expect(html).not.toContain(enService.portal.list.empty);
  });

  it('shows only the error and retry control when nothing is loaded and the request failed', () => {
    const html = renderView('en', viewProps({ isError: true }));
    expect(html).toContain(enService.portal.list.loadError);
    expect(html).toContain(`>${enService.portal.list.retry}</button>`);
    expect(html).not.toContain('<table');
    expect(html).not.toContain(enService.portal.loading);
  });

  it('keeps the loaded rows beside the error and retry control', () => {
    for (const language of ['he', 'en'] as const) {
      const service = language === 'he' ? heService : enService;
      const html = renderView(language, viewProps({ data: SAMPLE, isError: true }));
      expect(html).toContain(service.portal.list.loadError);
      expect(html).toContain(`>${service.portal.list.retry}</button>`);
      expect(html).toContain('<bdi dir="ltr">10003</bdi>');
      expect(html).toContain('<bdi dir="ltr">10001</bdi>');
    }
  });

  it('has a refresh control and a new-ticket control with 44px-target classes, no focus or key hooks', () => {
    const html = renderView('en', viewProps({ data: SAMPLE }));
    expect(html).toContain(`>${enService.portal.list.refresh}</button>`);
    expect(html).toContain(`class="portal-button" href="/service/new"`);
    expect(html).not.toContain('autofocus');
    expect(html).not.toContain('autoFocus');
  });

  it('shows page controls only above 50 tickets, with the page position and real links', () => {
    const few = renderView('en', viewProps({ data: page([row(10001)], 50) }));
    expect(few).not.toContain('<nav');

    const many = page([row(10001)], 101, 2);
    const html = renderView('en', viewProps({ data: many, pageNumber: 2 }));
    expect(html).toContain(`<nav class="portal-pagination" aria-label="${enService.portal.list.pagination.label}"`);
    expect(html).toContain('Page 2 of 3');
    expect(html).toContain('href="/service"');
    expect(html).toContain('href="/service?page=3"');
    expect(html).toContain(enService.portal.list.pagination.previous);
    expect(html).toContain(enService.portal.list.pagination.next);

    const first = renderView('he', viewProps({ data: page([row(10001)], 101, 1), pageNumber: 1 }));
    expect(first).toContain('עמוד 1 מתוך 3');
    expect(first).not.toContain(heService.portal.list.pagination.previous);
    expect(first).toContain('href="/service?page=2"');

    const last = renderView('en', viewProps({ data: page([row(10001)], 101, 3), pageNumber: 3 }));
    expect(last).toContain('Page 3 of 3');
    expect(last).not.toContain(enService.portal.list.pagination.next);
  });

  it('says that a page was not available and which page is shown', () => {
    const en = renderView('en', viewProps({ data: SAMPLE, fallbackPage: 1 }));
    expect(en).toContain('That page is not available. Showing page 1.');
    expect(en).toContain('role="status"');
    const he = renderView('he', viewProps({ data: SAMPLE, fallbackPage: 1 }));
    expect(he).toContain(heService.portal.list.pageFallback.replace('{{page}}', '1'));
    expect(renderView('en', viewProps({ data: SAMPLE }))).not.toContain('That page is not available');
  });
});

describe('TicketListScreen markup (container with a filled cache)', () => {
  function renderScreen(language: PortalLanguage, location: string, data: TicketPage, pageNumber: number): string {
    const client = new QueryClient();
    client.setQueryData(ticketKeys.list(pageNumber), data);
    return renderToStaticMarkup(
      createElement(
        I18nextProvider,
        { i18n: i18nFor(language) },
        createElement(
          QueryClientProvider,
          { client },
          createElement(StaticRouter, { location, basename: '/service' }, createElement(TicketListScreen)),
        ),
      ),
    );
  }

  it('shows the cached rows for the page in the URL', () => {
    const html = renderScreen('en', '/service?page=2', page([row(10077)], 120, 2), 2);
    expect(html).toContain('<bdi dir="ltr">10077</bdi>');
    expect(html).toContain('Page 2 of 3');
  });

  it('uses page 1 for an invalid page parameter', () => {
    const html = renderScreen('en', '/service?page=x', page([row(10001)], 1, 1), 1);
    expect(html).toContain('<bdi dir="ltr">10001</bdi>');
  });

  it('shows loading when the cache is empty', () => {
    const html = renderToStaticMarkup(
      createElement(
        I18nextProvider,
        { i18n: i18nFor('he') },
        createElement(
          QueryClientProvider,
          { client: new QueryClient() },
          createElement(StaticRouter, { location: '/service', basename: '/service' }, createElement(TicketListScreen)),
        ),
      ),
    );
    expect(html).toContain(heService.portal.loading);
    expect(html).toContain('dir="rtl"');
  });
});
