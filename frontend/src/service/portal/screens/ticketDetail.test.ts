// Tests of the ticket detail screen (task 23.4): the pure model, the focus-trap helper and the markup of the
// presentational components in both languages. No DOM and no testing library: `renderToStaticMarkup`.
import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { describe, expect, it } from 'vitest';
import { PortalApiError, GENERIC_CODE, NETWORK_CODE, TIMEOUT_CODE } from '@/service/api/portalFetch';
import {
  getTicketPhoto,
  parseFormLabels,
  parseTicketDetail,
  ticketDetailKeys,
  type CustomerTicketDetail,
  type FormLabels,
} from '@/service/api/ticketDetail';
import { FOCUSABLE_SELECTOR, nextFocusId } from '@/service/portal/focusTrap';
import { createPortalI18n, type PortalLanguage } from '@/service/portal/portalI18n';
import type { TicketStatus } from '@/service/types';
import { CANCEL_DIALOG_IDS, CancelDialog } from './CancelDialog';
import { TicketDetailMessage, TicketDetailView, TicketNotFoundPanel } from './TicketDetailView';
import {
  buildDetailRows,
  canCancel,
  cancelFailureOf,
  cancelFlowReducer,
  categoryName,
  fieldLabel,
  formatCreatedDate,
  ltrIsolate,
  parseTicketNumber,
  photoView,
  screenState,
  statusAnnouncement,
  type CancelPhase,
} from './ticketDetailModel';

// --- fixtures and helpers --------------------------------------------------------------------------

const STATUSES: readonly TicketStatus[] = ['received', 'in_progress', 'closed', 'cancelled'];

function ticketOf(overrides: Partial<CustomerTicketDetail> = {}): CustomerTicketDetail {
  return {
    ticket_number: 12345,
    category: { id: 'c1', name_he: 'דליפה / סתימה', name_en: 'Leak or Blockage' },
    created_date: '2025-03-05',
    status: 'received',
    urgent: false,
    city: 'Haifa',
    apartment: '12',
    phone: '0501234567',
    description: 'The tap leaks',
    has_photo: false,
    ...overrides,
  };
}

function translateIn(language: PortalLanguage): (key: string) => string {
  const instance = createPortalI18n(language);
  return (key) => String(instance.t(key));
}

function render(node: ReactElement, language: PortalLanguage): string {
  return renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n: createPortalI18n(language) },
      createElement(StaticRouter, { location: '/tickets/12345' }, node),
    ),
  );
}

const noop = (): void => undefined;

function viewOf(
  language: PortalLanguage,
  props: Partial<Parameters<typeof TicketDetailView>[0]> = {},
): string {
  return render(
    createElement(TicketDetailView, {
      ticket: ticketOf(),
      language,
      labels: undefined,
      photo: { kind: 'none' },
      staleErrorKeys: null,
      actionErrorKeys: null,
      onRefresh: noop,
      onRetryPhoto: noop,
      onCancel: noop,
      ...props,
    }),
    language,
  );
}

function dialogOf(language: PortalLanguage, pending: boolean): string {
  return render(
    createElement(CancelDialog, { ticketNumber: 12345, pending, onConfirm: noop, onDismiss: noop }),
    language,
  );
}

function buttonTag(markup: string, id: string): string {
  const match = new RegExp(`<button[^>]*id="${id}"[^>]*>`).exec(markup);
  if (match === null) throw new Error(`no button ${id}`);
  return match[0];
}

// --- the ticket number in the address --------------------------------------------------------------

describe('parseTicketNumber', () => {
  it('accepts exactly five digits from 10000 to 99999', () => {
    expect(parseTicketNumber('10000')).toBe(10000);
    expect(parseTicketNumber('99999')).toBe(99999);
    expect(parseTicketNumber('12345')).toBe(12345);
  });

  it('rejects everything else, so the not-found panel shows without a request', () => {
    for (const raw of [undefined, null, '', '9999', '100000', '01234', '1234a', ' 12345', '12345 ', '-12345', '1e4', '12.45', '../1234']) {
      expect(parseTicketNumber(raw)).toBeNull();
    }
  });
});

// --- dates, names, labels --------------------------------------------------------------------------

describe('formatCreatedDate', () => {
  it('writes YYYY-MM-DD as DD/MM/YYYY', () => {
    expect(formatCreatedDate('2025-03-05')).toBe('05/03/2025');
    expect(formatCreatedDate('2024-12-31')).toBe('31/12/2024');
  });

  it('leaves text that is not a date unchanged', () => {
    expect(formatCreatedDate('yesterday')).toBe('yesterday');
  });
});

describe('categoryName', () => {
  it('uses the name of the current language, and the other one when it is empty', () => {
    const category = { name_he: 'חשמל', name_en: 'Electrical' };
    expect(categoryName(category, 'he')).toBe('חשמל');
    expect(categoryName(category, 'en')).toBe('Electrical');
    expect(categoryName({ name_he: 'חשמל', name_en: '  ' }, 'en')).toBe('חשמל');
  });
});

describe('fieldLabel', () => {
  const labels: FormLabels = {
    city: { he: 'ישוב', en: 'Town' },
    phone: { he: '  ', en: '' },
  };

  it('uses the Field_Label of the current language', () => {
    expect(fieldLabel('city', labels, 'he', translateIn('he'))).toBe('ישוב');
    expect(fieldLabel('city', labels, 'en', translateIn('en'))).toBe('Town');
  });

  it('falls back to fields.* when the labels are missing, the key is missing or the text is blank', () => {
    expect(fieldLabel('city', undefined, 'en', translateIn('en'))).toBe('City');
    expect(fieldLabel('apartment', labels, 'en', translateIn('en'))).toBe('Apartment number');
    expect(fieldLabel('phone', labels, 'he', translateIn('he'))).toBe('מספר טלפון');
    expect(fieldLabel('phone', labels, 'en', translateIn('en'))).toBe('Phone number');
  });
});

// --- the rows --------------------------------------------------------------------------------------

describe('buildDetailRows', () => {
  const en = translateIn('en');

  it('lists every customer field in reading order, without the urgent row when the flag is not set', () => {
    const rows = buildDetailRows(ticketOf(), 'en', undefined, en);
    expect(rows.map((row) => row.key)).toEqual([
      'number', 'status', 'created', 'category', 'city', 'apartment', 'phone', 'description',
    ]);
    expect(rows.find((row) => row.key === 'status')?.value).toEqual({ kind: 'plain', text: 'Received' });
    expect(rows.find((row) => row.key === 'created')?.value).toEqual({ kind: 'ltr', text: '05/03/2025' });
    expect(rows.find((row) => row.key === 'phone')?.value).toEqual({ kind: 'ltr', text: '0501234567' });
    expect(rows.find((row) => row.key === 'number')?.value).toEqual({ kind: 'ltr', text: '12345' });
  });

  it('adds the urgent row, as text, only when the flag is set', () => {
    const rows = buildDetailRows(ticketOf({ urgent: true }), 'en', undefined, en);
    expect(rows.map((row) => row.key)).toContain('urgent');
    expect(rows.find((row) => row.key === 'urgent')?.value).toEqual({ kind: 'urgent', text: 'Urgent' });
  });

  it('labels the form fields with the current Field_Labels and the rest with the portal text', () => {
    const labels: FormLabels = { city: { he: 'ישוב', en: 'Town' }, description: { he: 'פירוט', en: 'Details' } };
    const rows = buildDetailRows(ticketOf(), 'en', labels, en);
    const label = (key: string): string | undefined => rows.find((row) => row.key === key)?.label;
    expect(label('city')).toBe('Town');
    expect(label('description')).toBe('Details');
    expect(label('apartment')).toBe('Apartment number');
    expect(label('number')).toBe('Ticket number');
    expect(label('created')).toBe('Date opened');
  });

  it('keeps the description exactly, line breaks included, as description text', () => {
    const description = 'first line\nsecond line\n\n  indented <b>not markup</b>';
    const row = buildDetailRows(ticketOf({ description }), 'en', undefined, en).find((r) => r.key === 'description');
    expect(row?.value).toEqual({ kind: 'description', text: description });
  });

  it('shows the no-description text for an empty or blank description', () => {
    for (const description of ['', '  \n ']) {
      const row = buildDetailRows(ticketOf({ description }), 'en', undefined, en).find((r) => r.key === 'description');
      expect(row?.value).toEqual({ kind: 'plain', text: 'No description was entered.' });
    }
    const he = buildDetailRows(ticketOf({ description: '' }), 'he', undefined, translateIn('he'));
    expect(he.find((r) => r.key === 'description')?.value).toEqual({ kind: 'plain', text: 'לא הוזן תיאור.' });
  });

  it('shows the status as text in the current language', () => {
    const he = buildDetailRows(ticketOf({ status: 'in_progress' }), 'he', undefined, translateIn('he'));
    expect(he.find((row) => row.key === 'status')?.value).toEqual({ kind: 'plain', text: 'בטיפול' });
  });
});

// --- cancel availability ---------------------------------------------------------------------------

describe('canCancel', () => {
  it('is true for received and in progress only', () => {
    expect(STATUSES.map((status) => [status, canCancel(status)])).toEqual([
      ['received', true],
      ['in_progress', true],
      ['closed', false],
      ['cancelled', false],
    ]);
  });
});

// --- error mapping ---------------------------------------------------------------------------------

describe('cancelFailureOf', () => {
  it('ticket_status_changed reloads and shows the status-changed text', () => {
    expect(cancelFailureOf(new PortalApiError(409, 'ticket_status_changed'))).toEqual({
      messageKeys: ['errors.ticket_status_changed'],
      reload: true,
    });
  });

  it('invalid_transition reloads and shows its text', () => {
    expect(cancelFailureOf(new PortalApiError(409, 'invalid_transition'))).toEqual({
      messageKeys: ['errors.invalid_transition'],
      reload: true,
    });
  });

  it('404 ticket_not_found reloads, which then shows the not-found panel', () => {
    const failure = cancelFailureOf(new PortalApiError(404, 'ticket_not_found'));
    expect(failure.reload).toBe(true);
    expect(failure.messageKeys).toEqual(['portal.detail.cancelFailed', 'errors.ticket_not_found']);
  });

  it('422 invalid_expected_status reloads with its text after the lead message', () => {
    expect(cancelFailureOf(new PortalApiError(422, 'invalid_expected_status'))).toEqual({
      messageKeys: ['portal.detail.cancelFailed', 'errors.invalid_expected_status'],
      reload: true,
    });
  });

  it('503 and rate limits show the lead message and the code text, without a reload', () => {
    expect(cancelFailureOf(new PortalApiError(503, 'service_unavailable'))).toEqual({
      messageKeys: ['portal.detail.cancelFailed', 'errors.service_unavailable'],
      reload: false,
    });
    expect(cancelFailureOf(new PortalApiError(429, 'rate_limited')).reload).toBe(false);
  });

  it('network, timeout, an unreadable body and a non-API error show only the lead message', () => {
    for (const error of [
      new PortalApiError(0, NETWORK_CODE),
      new PortalApiError(0, TIMEOUT_CODE),
      new PortalApiError(502, GENERIC_CODE),
      new Error('boom'),
    ]) {
      expect(cancelFailureOf(error)).toEqual({ messageKeys: ['portal.detail.cancelFailed'], reload: false });
    }
  });
});

describe('screenState', () => {
  it('shows the not-found panel for a malformed number without any data', () => {
    expect(screenState({ numberValid: false, hasData: false, error: null })).toEqual({ kind: 'not-found' });
  });

  it('shows the not-found panel for 404 ticket_not_found, even when an earlier load had data', () => {
    const error = new PortalApiError(404, 'ticket_not_found');
    expect(screenState({ numberValid: true, hasData: false, error })).toEqual({ kind: 'not-found' });
    expect(screenState({ numberValid: true, hasData: true, error })).toEqual({ kind: 'not-found' });
  });

  it('is loading until the first answer', () => {
    expect(screenState({ numberValid: true, hasData: false, error: null })).toEqual({ kind: 'loading' });
  });

  it('shows the error with retry and no data when the first load fails (503, network, framework 404)', () => {
    expect(screenState({ numberValid: true, hasData: false, error: new PortalApiError(503, 'service_unavailable') })).toEqual({
      kind: 'error',
      messageKeys: ['portal.detail.loadError', 'errors.service_unavailable'],
    });
    expect(screenState({ numberValid: true, hasData: false, error: new PortalApiError(0, NETWORK_CODE) })).toEqual({
      kind: 'error',
      messageKeys: ['portal.detail.loadError'],
    });
    expect(screenState({ numberValid: true, hasData: false, error: new PortalApiError(404, GENERIC_CODE) }).kind).toBe('error');
  });

  it('keeps the ticket shown when a reload fails, and clears the error once a reload succeeds', () => {
    expect(screenState({ numberValid: true, hasData: true, error: new PortalApiError(0, TIMEOUT_CODE) })).toEqual({
      kind: 'ready',
      staleErrorKeys: ['portal.detail.loadError'],
    });
    expect(screenState({ numberValid: true, hasData: true, error: null })).toEqual({ kind: 'ready', staleErrorKeys: null });
  });
});

describe('photoView', () => {
  const base = { hasPhoto: true, status: 'pending', error: null, url: null } as const;

  it('shows nothing for a ticket without a photo, whatever the upload setting is', () => {
    expect(photoView({ ...base, hasPhoto: false })).toEqual({ kind: 'none' });
  });

  it('is loading while the request runs and before the object URL exists', () => {
    expect(photoView(base)).toEqual({ kind: 'loading' });
    expect(photoView({ ...base, status: 'success' })).toEqual({ kind: 'loading' });
  });

  it('is ready with the object URL, and stays ready if something fails afterwards', () => {
    expect(photoView({ ...base, status: 'success', url: 'blob:x' })).toEqual({ kind: 'ready', url: 'blob:x' });
    expect(photoView({ ...base, status: 'error', error: new PortalApiError(0, NETWORK_CODE), url: 'blob:x' }).kind).toBe('ready');
  });

  it('shows an error with retry for 503 photo_storage_unavailable and for network failures', () => {
    expect(photoView({ ...base, status: 'error', error: new PortalApiError(503, 'photo_storage_unavailable') })).toEqual({
      kind: 'error',
      messageKeys: ['portal.detail.photoError', 'errors.photo_storage_unavailable'],
      retryable: true,
    });
    expect(photoView({ ...base, status: 'error', error: new PortalApiError(0, NETWORK_CODE) })).toEqual({
      kind: 'error',
      messageKeys: ['portal.detail.photoError'],
      retryable: true,
    });
  });

  it('shows an error without retry when the photo is gone (404)', () => {
    const view = photoView({ ...base, status: 'error', error: new PortalApiError(404, 'ticket_not_found') });
    expect(view.kind).toBe('error');
    expect(view.kind === 'error' && view.retryable).toBe(false);
  });
});

describe('statusAnnouncement', () => {
  const entry = (status: TicketStatus, ticketNumber = 12345) => ({ ticketNumber, status });

  it('announces nothing on the first load and for identical data', () => {
    expect(statusAnnouncement(null, entry('received'))).toBeNull();
    expect(statusAnnouncement(entry('received'), entry('received'))).toBeNull();
  });

  it('announces the new status when a poll finds a change', () => {
    expect(statusAnnouncement(entry('received'), entry('in_progress'))).toEqual({ ticketNumber: 12345, status: 'in_progress' });
    expect(statusAnnouncement(entry('in_progress'), entry('closed'))).toEqual({ ticketNumber: 12345, status: 'closed' });
  });

  it('announces nothing for another ticket', () => {
    expect(statusAnnouncement(entry('received', 10001), entry('closed', 10002))).toBeNull();
  });
});

describe('cancelFlowReducer', () => {
  it('opens, dismisses, confirms and settles', () => {
    expect(cancelFlowReducer('closed', 'open')).toBe('confirming');
    expect(cancelFlowReducer('confirming', 'dismiss')).toBe('closed');
    expect(cancelFlowReducer('confirming', 'confirm')).toBe('pending');
    expect(cancelFlowReducer('pending', 'settled')).toBe('closed');
  });

  it('accepts confirm only while confirming, so one confirmation sends one request', () => {
    expect(cancelFlowReducer('pending', 'confirm')).toBe('pending');
    expect(cancelFlowReducer('closed', 'confirm')).toBe('closed');
  });

  it('ignores dismiss while the request is pending', () => {
    expect(cancelFlowReducer('pending', 'dismiss')).toBe('pending');
  });

  it('survives a reload: the phase is a function of the four actions only, so a data reload cannot change it', () => {
    const phases: CancelPhase[] = ['confirming', 'pending'];
    // A reload dispatches nothing; any sequence of non-dialog events leaves the phase as it was.
    for (const phase of phases) expect(cancelFlowReducer(phase, 'open')).toBe(phase);
  });
});

// --- API module (pure parts) -----------------------------------------------------------------------

describe('ticketDetail api module', () => {
  it('keys start with the portal root, so sign-out drops them', () => {
    expect(ticketDetailKeys.detail(12345)[0]).toBe('portal');
    expect(ticketDetailKeys.photo(12345)[0]).toBe('portal');
    expect(ticketDetailKeys.labels[0]).toBe('portal');
  });

  it('accepts a detail answer and rejects other shapes as the generic error', () => {
    expect(parseTicketDetail(ticketOf()).ticket_number).toBe(12345);
    for (const bad of [null, [], {}, { ...ticketOf(), status: 'open' }, { ...ticketOf(), has_photo: 'yes' }, { ...ticketOf(), category: null }]) {
      expect(() => parseTicketDetail(bad)).toThrow(PortalApiError);
    }
  });

  it('reads the labels of form-config and drops malformed entries', () => {
    const labels = parseFormLabels({
      labels: { city: { he: 'ישוב', en: 'Town' }, phone: { he: 1, en: 'Phone' }, unknown: { he: 'x', en: 'y' } },
      categories: [],
    });
    expect(labels).toEqual({ city: { he: 'ישוב', en: 'Town' } });
    expect(parseFormLabels(null)).toEqual({});
  });

  it('sends the photo request with the bearer header and returns the Blob', async () => {
    const original = globalThis.fetch;
    const seen: { url: string; authorization: string | undefined }[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      seen.push({
        url: String(input),
        authorization: (init?.headers as Record<string, string> | undefined)?.Authorization,
      });
      return new Response(new Blob([new Uint8Array([1, 2, 3])], { type: 'image/jpeg' }), { status: 200 });
    }) as typeof fetch;
    try {
      const tokens = { getToken: () => 'customer-token', clearToken: noop, expireSession: noop };
      const blob = await getTicketPhoto(12345, undefined, tokens);
      expect(blob.type).toBe('image/jpeg');
      expect(blob.size).toBe(3);
      expect(seen).toHaveLength(1);
      expect(seen[0]?.url.endsWith('/service/tickets/12345/photo')).toBe(true);
      expect(seen[0]?.authorization).toBe('Bearer customer-token');
    } finally {
      globalThis.fetch = original;
    }
  });

  it('turns a photo answer that is not an image into the generic error', async () => {
    const original = globalThis.fetch;
    globalThis.fetch = (async () => new Response('<html></html>', { status: 200, headers: { 'Content-Type': 'text/html' } })) as typeof fetch;
    try {
      const tokens = { getToken: () => 'customer-token', clearToken: noop, expireSession: noop };
      await expect(getTicketPhoto(12345, undefined, tokens)).rejects.toMatchObject({ code: GENERIC_CODE });
    } finally {
      globalThis.fetch = original;
    }
  });
});

// --- focus trap helper -----------------------------------------------------------------------------

describe('nextFocusId', () => {
  const ids = ['a', 'b', 'c'] as const;

  it('moves forward and wraps from the last to the first', () => {
    expect(nextFocusId(ids, 'a', false)).toBe('b');
    expect(nextFocusId(ids, 'b', false)).toBe('c');
    expect(nextFocusId(ids, 'c', false)).toBe('a');
  });

  it('moves backward and wraps from the first to the last', () => {
    expect(nextFocusId(ids, 'c', true)).toBe('b');
    expect(nextFocusId(ids, 'b', true)).toBe('a');
    expect(nextFocusId(ids, 'a', true)).toBe('c');
  });

  it('stays on the only element in both directions', () => {
    expect(nextFocusId(['only'], 'only', false)).toBe('only');
    expect(nextFocusId(['only'], 'only', true)).toBe('only');
  });

  it('returns null when nothing can take focus', () => {
    expect(nextFocusId([], null, false)).toBeNull();
    expect(nextFocusId([], 'a', true)).toBeNull();
  });

  it('enters at the first element going forward and at the last going backward when focus is elsewhere', () => {
    expect(nextFocusId(ids, null, false)).toBe('a');
    expect(nextFocusId(ids, null, true)).toBe('c');
    expect(nextFocusId(ids, 'outside', false)).toBe('a');
    expect(nextFocusId(ids, 'outside', true)).toBe('c');
  });

  it('leaves disabled controls and tabindex -1 out of the focusable selector', () => {
    expect(FOCUSABLE_SELECTOR).toContain('button:not([disabled])');
    expect(FOCUSABLE_SELECTOR).toContain('[tabindex]:not([tabindex="-1"])');
  });
});

// --- markup: the detail view -----------------------------------------------------------------------

describe('TicketDetailView markup', () => {
  it('sets lang and dir from the language', () => {
    expect(viewOf('he')).toContain('lang="he" dir="rtl"');
    expect(viewOf('en')).toContain('lang="en" dir="ltr"');
  });

  it('puts the ticket number, the phone and the date in bdi dir="ltr"', () => {
    for (const language of ['he', 'en'] as const) {
      const markup = viewOf(language);
      expect(markup).toContain('<bdi dir="ltr">12345</bdi>');
      expect(markup).toContain('<bdi dir="ltr">0501234567</bdi>');
      expect(markup).toContain('<bdi dir="ltr">05/03/2025</bdi>');
    }
  });

  it('isolates the number in the heading', () => {
    expect(viewOf('en')).toContain(`<h1 tabindex="-1" class="portal-page-heading">Ticket ${ltrIsolate('12345')}</h1>`);
    expect(viewOf('he')).toContain(ltrIsolate('12345'));
  });

  it('labels the fields with the current Field_Labels, in each language', () => {
    const labels: FormLabels = { city: { he: 'ישוב', en: 'Town' }, urgent: { he: 'בהול', en: 'Rush' } };
    const he = viewOf('he', { labels, ticket: ticketOf({ urgent: true }) });
    const en = viewOf('en', { labels, ticket: ticketOf({ urgent: true }) });
    expect(he).toContain('<dt>ישוב</dt>');
    expect(he).toContain('<dt>בהול</dt>');
    expect(en).toContain('<dt>Town</dt>');
    expect(en).toContain('<dt>Rush</dt>');
    expect(en).toContain('<dt>Apartment number</dt>');
  });

  it('shows the category in the current language and the status as text', () => {
    expect(viewOf('he')).toContain('דליפה / סתימה');
    expect(viewOf('he')).toContain('נקלטה');
    expect(viewOf('en')).toContain('Leak or Blockage');
    expect(viewOf('en')).toContain('Received');
  });

  it('shows urgency as an icon plus text only when the flag is set', () => {
    const urgent = viewOf('en', { ticket: ticketOf({ urgent: true }) });
    expect(urgent).toContain('aria-hidden="true"');
    expect(urgent).toContain('<span>Urgent</span>');
    expect(viewOf('en')).not.toContain('ticket-detail-urgent');
  });

  it('renders the description as escaped text with its line breaks and a pre-wrap class, never as markup', () => {
    const markup = viewOf('en', { ticket: ticketOf({ description: 'line one\nline two <script>alert(1)</script>' }) });
    expect(markup).toContain('<span class="ticket-detail-description">line one\nline two &lt;script&gt;alert(1)&lt;/script&gt;</span>');
    expect(markup).not.toContain('<script>');
  });

  it('shows the no-description text in the current language', () => {
    expect(viewOf('en', { ticket: ticketOf({ description: '' }) })).toContain('No description was entered.');
    expect(viewOf('he', { ticket: ticketOf({ description: '' }) })).toContain('לא הוזן תיאור.');
  });

  it('shows the photo with an alt text that has the ticket number and the category', () => {
    const photo = { kind: 'ready', url: 'blob:http://localhost/abc' } as const;
    const en = viewOf('en', { ticket: ticketOf({ has_photo: true }), photo });
    expect(en).toContain('src="blob:http://localhost/abc"');
    expect(en).toContain('alt="Photo for ticket 12345, Leak or Blockage"');
    const he = viewOf('he', { ticket: ticketOf({ has_photo: true }), photo });
    expect(he).toContain('alt="תמונה לקריאה 12345, דליפה / סתימה"');
  });

  it('shows no photo area for a ticket without a photo', () => {
    const markup = viewOf('en');
    expect(markup).not.toContain('<img');
    expect(markup).not.toContain('ticket-detail-photo-section');
  });

  it('shows the photo loading text, and the error with a retry control, except for a gone photo', () => {
    const ticket = ticketOf({ has_photo: true });
    expect(viewOf('en', { ticket, photo: { kind: 'loading' } })).toContain('Loading the photo…');
    const failed = viewOf('en', {
      ticket,
      photo: { kind: 'error', messageKeys: ['portal.detail.photoError', 'errors.photo_storage_unavailable'], retryable: true },
    });
    expect(failed).toContain('Could not load the photo.');
    expect(failed).toContain('The photo could not be saved right now. Try again later.');
    expect(failed).toContain('Try again');
    const gone = viewOf('en', {
      ticket,
      photo: { kind: 'error', messageKeys: ['portal.detail.photoError'], retryable: false },
    });
    expect(gone).toContain('Could not load the photo.');
    expect(gone).not.toContain('>Try again<');
  });

  it('offers the cancel control only for received and in progress', () => {
    for (const status of STATUSES) {
      const markup = viewOf('en', { ticket: ticketOf({ status }) });
      expect(markup.includes('Cancel ticket')).toBe(canCancel(status));
      expect(markup.includes('aria-haspopup="dialog"')).toBe(canCancel(status));
    }
    expect(viewOf('he', { ticket: ticketOf({ status: 'received' }) })).toContain('ביטול הקריאה');
    expect(viewOf('he', { ticket: ticketOf({ status: 'closed' }) })).not.toContain('ביטול הקריאה');
    expect(viewOf('he', { ticket: ticketOf({ status: 'cancelled' }) })).not.toContain('ביטול הקריאה');
  });

  it('keeps the ticket and shows the reload error with a retry control', () => {
    const markup = viewOf('en', { staleErrorKeys: ['portal.detail.loadError'] });
    expect(markup).toContain('Could not load the ticket.');
    expect(markup).toContain('Leak or Blockage');
    expect(markup).toContain('>Try again<');
  });

  it('shows the error of a failed cancellation with the ticket still displayed', () => {
    const markup = viewOf('en', { actionErrorKeys: ['errors.ticket_status_changed'] });
    expect(markup).toContain('The ticket status changed in the meantime. The current status is shown.');
    expect(markup).toContain('Leak or Blockage');
  });

  it('has a back control to the list and a refresh control', () => {
    const markup = viewOf('en');
    expect(markup).toContain('href="/"');
    expect(markup).toContain('Back to the list');
    expect(markup).toContain('>Refresh<');
  });

  it('has exactly one h1', () => {
    expect(viewOf('en').match(/<h1[ >]/g)).toHaveLength(1);
  });
});

describe('TicketDetailMessage and TicketNotFoundPanel markup', () => {
  it('shows the loading text, or the error with retry and no data', () => {
    const loading = render(createElement(TicketDetailMessage, { ticketNumber: 12345, language: 'en', state: { kind: 'loading' }, onRetry: noop }), 'en');
    expect(loading).toContain('Loading…');
    const failed = render(
      createElement(TicketDetailMessage, {
        ticketNumber: 12345,
        language: 'he',
        state: { kind: 'error', messageKeys: ['portal.detail.loadError'] },
        onRetry: noop,
      }),
      'he',
    );
    expect(failed).toContain('לא ניתן לטעון את הקריאה.');
    expect(failed).toContain('ניסיון חוזר');
    expect(failed).not.toContain('<dl');
  });

  it('shows the not-found panel with a way back and no ticket data, in both languages', () => {
    const en = render(createElement(TicketNotFoundPanel, { language: 'en' }), 'en');
    expect(en).toContain('Ticket not found');
    expect(en).toContain('href="/"');
    expect(en).toContain('Back to my tickets');
    expect(en).toContain('lang="en" dir="ltr"');
    expect(en).not.toContain('<dl');
    const he = render(createElement(TicketNotFoundPanel, { language: 'he' }), 'he');
    expect(he).toContain('הקריאה לא נמצאה');
    expect(he).toContain('href="/"');
    expect(he).toContain('lang="he" dir="rtl"');
  });
});

// --- markup: the cancellation dialog ---------------------------------------------------------------

describe('CancelDialog markup', () => {
  it('is a modal dialog labelled and described by elements that exist', () => {
    for (const language of ['he', 'en'] as const) {
      const markup = dialogOf(language, false);
      expect(markup).toContain('role="dialog"');
      expect(markup).toContain('aria-modal="true"');
      expect(markup).toContain(`aria-labelledby="${CANCEL_DIALOG_IDS.title}"`);
      expect(markup).toContain(`aria-describedby="${CANCEL_DIALOG_IDS.description}"`);
      expect(markup).toContain(`id="${CANCEL_DIALOG_IDS.title}"`);
      expect(markup).toContain(`id="${CANCEL_DIALOG_IDS.description}"`);
    }
  });

  it('shows the ticket number in the dialog text, in both languages', () => {
    const en = dialogOf('en', false);
    expect(en).toContain(`Cancel ticket number ${ltrIsolate('12345')}?`);
    const he = dialogOf('he', false);
    expect(he).toContain(ltrIsolate('12345'));
    expect(he).toContain('לבטל את קריאה מספר');
  });

  it('has a confirm and a dismiss control, both enabled before the request', () => {
    const markup = dialogOf('en', false);
    expect(markup).toContain('Yes, cancel the ticket');
    expect(markup).toContain('No, keep the ticket');
    expect(buttonTag(markup, CANCEL_DIALOG_IDS.confirm)).not.toContain('disabled');
    expect(buttonTag(markup, CANCEL_DIALOG_IDS.dismiss)).not.toContain('disabled');
    expect(markup).toContain('aria-busy="false"');
  });

  it('disables confirm and shows the pending label while the request waits', () => {
    const en = dialogOf('en', true);
    expect(buttonTag(en, CANCEL_DIALOG_IDS.confirm)).toContain('disabled=""');
    expect(en).toContain('Cancelling…');
    expect(en).not.toContain('Yes, cancel the ticket');
    expect(en).toContain('aria-busy="true"');
    const he = dialogOf('he', true);
    expect(buttonTag(he, CANCEL_DIALOG_IDS.confirm)).toContain('disabled=""');
    expect(he).toContain('מבטל…');
  });
});

// --- locale keys used by the screen ----------------------------------------------------------------

describe('locale keys', () => {
  const keys = [
    'portal.pageTitle.detail',
    'portal.loading',
    'portal.urgent.marker',
    'portal.detail.back',
    'portal.detail.refresh',
    'portal.detail.number',
    'portal.detail.status',
    'portal.detail.created',
    'portal.detail.noDescription',
    'portal.detail.photoAlt',
    'portal.detail.photoLoading',
    'portal.detail.photoError',
    'portal.detail.retry',
    'portal.detail.loadError',
    'portal.detail.cancelTicket',
    'portal.detail.cancelDialog.title',
    'portal.detail.cancelDialog.message',
    'portal.detail.cancelDialog.confirm',
    'portal.detail.cancelDialog.dismiss',
    'portal.detail.cancelDialog.pending',
    'portal.detail.cancelFailed',
    'portal.detail.notFound.title',
    'portal.detail.notFound.body',
    'portal.detail.notFound.back',
    'a11y.live.ticketCancelled',
    'a11y.live.statusChanged',
    'errors.ticket_status_changed',
    'errors.invalid_transition',
    'errors.invalid_expected_status',
    'errors.ticket_not_found',
    'errors.service_unavailable',
    'errors.photo_storage_unavailable',
    'errors.rate_limited',
    ...STATUSES.map((status) => `status.${status}`),
    'fields.city',
    'fields.apartment',
    'fields.phone',
    'fields.category',
    'fields.description',
    'fields.photo',
    'fields.urgent',
  ];

  it('are all present in Hebrew and English, so keys-23-4.json is not needed', () => {
    for (const language of ['he', 'en'] as const) {
      const instance = createPortalI18n(language);
      const missing = keys.filter((key) => !instance.exists(key));
      expect(missing).toEqual([]);
    }
  });
});
