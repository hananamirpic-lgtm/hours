// Tests of the management dashboard (task 24.2): the view model, the list links of the three counts, the
// polling options and the markup of every state. No DOM and no testing library: pure functions and
// `renderToStaticMarkup`.
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { describe, expect, it } from 'vitest';
import en from '@/i18n/locales/en/service.json';
import he from '@/i18n/locales/he/service.json';
import type { StaffDashboard } from '@/service/api/staff';
import { createPortalI18n, type PortalLanguage } from '@/service/portal/portalI18n';
import {
  DASHBOARD_COUNTS,
  DASHBOARD_POLL_MS,
  TICKET_LIST_PATH,
  dashboardListFilters,
  dashboardListTarget,
  dashboardQueryOptions,
  dashboardViewState,
  isDashboardResponse,
  isVisibleState,
  type DashboardViewState,
} from './dashboardModel';
import { DashboardView } from './dashboardView';

const data = (over: Partial<StaffDashboard> = {}): StaffDashboard => ({
  open: 7,
  in_progress: 3,
  urgent: 2,
  ticket_number_warning: null,
  ...over,
});

// --- the response ------------------------------------------------------------------------------------------

describe('isDashboardResponse', () => {
  it('accepts three whole counts with a null, absent or numeric warning', () => {
    expect(isDashboardResponse(data())).toBe(true);
    expect(isDashboardResponse({ open: 0, in_progress: 0, urgent: 0 })).toBe(true);
    expect(isDashboardResponse(data({ ticket_number_warning: { remaining: 12 } }))).toBe(true);
  });

  it.each([
    ['null', null],
    ['an array', []],
    ['a missing count', { open: 1, in_progress: 1, ticket_number_warning: null }],
    ['a negative count', data({ open: -1 })],
    ['a fractional count', data({ urgent: 1.5 })],
    ['a string count', { ...data(), in_progress: '3' }],
    ['a warning without remaining', { ...data(), ticket_number_warning: {} }],
    ['a warning of the wrong type', { ...data(), ticket_number_warning: 5 }],
  ])('rejects %s', (_name, body) => {
    expect(isDashboardResponse(body)).toBe(false);
  });
});

// --- what the page shows (R17.1, R17.6, R17.7, R10.9) --------------------------------------------------------

describe('dashboardViewState', () => {
  it('shows the loading text before the first load', () => {
    expect(dashboardViewState({ data: undefined, isError: false, isConfigStaff: true })).toEqual({
      content: 'loading',
      counts: null,
      showError: false,
      stale: false,
      numberWarning: null,
    });
  });

  it('shows the three counts after a load, with no error and no label', () => {
    const state = dashboardViewState({ data: data(), isError: false, isConfigStaff: false });
    expect(state.content).toBe('counts');
    expect(state.counts).toEqual({ open: 7, inProgress: 3, urgent: 2 });
    expect(state.showError).toBe(false);
    expect(state.stale).toBe(false);
  });

  it('shows zero as a count', () => {
    const state = dashboardViewState({
      data: data({ open: 0, in_progress: 0, urgent: 0 }),
      isError: false,
      isConfigStaff: false,
    });
    expect(state.counts).toEqual({ open: 0, inProgress: 0, urgent: 0 });
  });

  it('keeps the old counts with the label and the error after a failed reload (R17.6)', () => {
    const state = dashboardViewState({ data: data(), isError: true, isConfigStaff: false });
    expect(state.counts).toEqual({ open: 7, inProgress: 3, urgent: 2 });
    expect(state.showError).toBe(true);
    expect(state.stale).toBe(true);
  });

  it('shows the error and no count values when the first load fails (R17.6)', () => {
    const state = dashboardViewState({ data: undefined, isError: true, isConfigStaff: true });
    expect(state.content).toBe('none');
    expect(state.counts).toBeNull();
    expect(state.showError).toBe(true);
    expect(state.stale).toBe(false);
  });

  it('replaces the counts and clears both messages when a reload succeeds after a failure (R17.7)', () => {
    const failed = dashboardViewState({ data: data(), isError: true, isConfigStaff: false });
    const recovered = dashboardViewState({
      data: data({ open: 9, in_progress: 4, urgent: 1 }),
      isError: false,
      isConfigStaff: false,
    });
    expect(failed.stale).toBe(true);
    expect(recovered.counts).toEqual({ open: 9, inProgress: 4, urgent: 1 });
    expect(recovered.showError).toBe(false);
    expect(recovered.stale).toBe(false);
  });

  describe('the ticket-number warning (R10.9)', () => {
    const warned = data({ ticket_number_warning: { remaining: 640 } });

    it('shows the remaining count to Config_Staff', () => {
      expect(dashboardViewState({ data: warned, isError: false, isConfigStaff: true }).numberWarning).toBe(640);
    });

    it('never shows it to a technician, even when the answer holds one', () => {
      expect(dashboardViewState({ data: warned, isError: false, isConfigStaff: false }).numberWarning).toBeNull();
    });

    it('shows none when the answer has none', () => {
      expect(dashboardViewState({ data: data(), isError: false, isConfigStaff: true }).numberWarning).toBeNull();
    });
  });
});

// --- the links of the three counts (R17.4) -------------------------------------------------------------------

describe('dashboard list links', () => {
  it('lists the three counts in the order open, in progress, urgent', () => {
    expect(DASHBOARD_COUNTS.map((entry) => entry.key)).toEqual(['open', 'inProgress', 'urgent']);
  });

  it('filters Open by Received and In Progress', () => {
    expect(dashboardListFilters('open')).toEqual({ status: ['received', 'in_progress'] });
  });

  it('filters In Progress by In Progress', () => {
    expect(dashboardListFilters('inProgress')).toEqual({ status: ['in_progress'] });
  });

  it('filters Urgent by urgent only together with Received and In Progress', () => {
    expect(dashboardListFilters('urgent')).toEqual({ status: ['received', 'in_progress'], urgentOnly: true });
  });

  it('opens the ticket list with the filters as the staff API names them', () => {
    expect(dashboardListTarget('open')).toEqual({
      pathname: TICKET_LIST_PATH,
      search: '?status=received&status=in_progress',
    });
    expect(dashboardListTarget('inProgress').search).toBe('?status=in_progress');
    expect(dashboardListTarget('urgent').search).toBe('?status=received&status=in_progress&urgent_only=true');
  });

  it('the list path is the ticket-list entry of the navigation', () => {
    expect(TICKET_LIST_PATH).toBe('/tickets/list');
  });
});

// --- polling (R17.5) -----------------------------------------------------------------------------------------

describe('dashboardQueryOptions', () => {
  it('reloads every 30 seconds, never in the background', () => {
    const options = dashboardQueryOptions(true);
    expect(DASHBOARD_POLL_MS).toBe(30_000);
    expect(options.refetchInterval).toBe(30_000);
    expect(options.refetchIntervalInBackground).toBe(false);
  });

  it('reloads at once when the tab becomes visible, whatever the age of the counts', () => {
    const options = dashboardQueryOptions(true);
    expect(options.refetchOnWindowFocus).toBe('always');
    expect(options.staleTime).toBe(0);
  });

  it('sends no request while the tab is hidden and enables the query when it is visible', () => {
    expect(dashboardQueryOptions(false).enabled).toBe(false);
    expect(dashboardQueryOptions(true).enabled).toBe(true);
  });

  it('leaves the retry to the next reload and keeps no counts of an earlier visit', () => {
    const options = dashboardQueryOptions(true);
    expect(options.retry).toBe(false);
    expect(options.gcTime).toBe(0);
  });

  it('reads the visibility state', () => {
    expect(isVisibleState('visible')).toBe(true);
    expect(isVisibleState('hidden')).toBe(false);
    expect(isVisibleState(undefined)).toBe(true);
  });
});

// --- markup --------------------------------------------------------------------------------------------------

const text = { en, he };

function render(language: PortalLanguage, state: DashboardViewState): string {
  return renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n: createPortalI18n(language) },
      createElement(StaticRouter, { location: '/tickets' }, createElement(DashboardView, { state })),
    ),
  );
}

const view = (
  input: { data: StaffDashboard | undefined; isError: boolean; isConfigStaff?: boolean },
): DashboardViewState => dashboardViewState({ isConfigStaff: false, ...input });

describe('DashboardView', () => {
  it.each(['en', 'he'] as const)('shows the three counts with their names and links in %s', (language) => {
    const html = render(language, view({ data: data(), isError: false }));
    const d = text[language].management.dashboard;
    expect(html).toContain(d.title);
    expect(html).toContain(d.open);
    expect(html).toContain(d.inProgress);
    expect(html).toContain(d.urgent);
    expect(html.match(new RegExp(d.showTickets, 'g'))).toHaveLength(3);
    expect(html).toContain('<bdi dir="ltr">7</bdi>');
    expect(html).toContain('<bdi dir="ltr">3</bdi>');
    expect(html).toContain('<bdi dir="ltr">2</bdi>');
    expect(html).toContain('href="/tickets/list?status=received&amp;status=in_progress"');
    expect(html).toContain('href="/tickets/list?status=in_progress"');
    expect(html).toContain('href="/tickets/list?status=received&amp;status=in_progress&amp;urgent_only=true"');
    expect(html).not.toContain(d.stale);
    expect(html).not.toContain(d.loadError);
  });

  it('shows zero counts as 0', () => {
    const html = render('en', view({ data: data({ open: 0, in_progress: 0, urgent: 0 }), isError: false }));
    expect(html.match(/<bdi dir="ltr">0<\/bdi>/g)).toHaveLength(3);
  });

  it('gives each link a name made of the count name, its value and the link text', () => {
    const html = render('en', view({ data: data(), isError: false }));
    expect(html.match(/aria-labelledby="[^"]+-open-label [^"]+-open-value [^"]+-open-link"/g)).toHaveLength(1);
  });

  it.each(['en', 'he'] as const)('keeps the counts and adds the error and the label after a failure in %s', (language) => {
    const html = render(language, view({ data: data(), isError: true }));
    const d = text[language].management.dashboard;
    expect(html).toContain(d.loadError);
    expect(html).toContain(d.stale);
    expect(html).toContain('<bdi dir="ltr">7</bdi>');
    expect(html).toContain('role="alert"');
  });

  it('shows the error and no counts, no links and no label when the first load fails', () => {
    const html = render('en', view({ data: undefined, isError: true }));
    expect(html).toContain(en.management.dashboard.loadError);
    expect(html).not.toContain(en.management.dashboard.stale);
    expect(html).not.toContain('<bdi');
    expect(html).not.toContain('<a ');
  });

  it('shows the loading text and nothing else before the first load', () => {
    const html = render('en', view({ data: undefined, isError: false }));
    expect(html).toContain(en.management.common.loading);
    expect(html).not.toContain('<bdi');
    expect(html).not.toContain(en.management.dashboard.loadError);
  });

  it('shows the recovered counts with no error and no label (R17.7)', () => {
    const html = render('en', view({ data: data({ open: 9, in_progress: 4, urgent: 1 }), isError: false }));
    expect(html).toContain('<bdi dir="ltr">9</bdi>');
    expect(html).not.toContain(en.management.dashboard.stale);
    expect(html).not.toContain(en.management.dashboard.loadError);
  });

  it('shows the warning with the remaining count to Config_Staff only', () => {
    const warned = data({ ticket_number_warning: { remaining: 640 } });
    const forConfig = render('en', view({ data: warned, isError: false, isConfigStaff: true }));
    const forTechnician = render('en', view({ data: warned, isError: false, isConfigStaff: false }));
    expect(forConfig).toContain('Only 640 ticket numbers are left.');
    expect(forTechnician).not.toContain('640');
    expect(render('he', view({ data: warned, isError: false, isConfigStaff: true }))).toContain('640');
  });

  it('has one h1 and one h2 per count', () => {
    const html = render('en', view({ data: data(), isError: false }));
    expect(html.match(/<h1/g)).toHaveLength(1);
    expect(html.match(/<h2/g)).toHaveLength(3);
  });
});
