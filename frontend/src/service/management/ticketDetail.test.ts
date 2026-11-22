// Tests of the management ticket detail page (task 24.4): the pure model, and the markup of the
// presentational sections in both languages. No DOM and no testing library: `renderToStaticMarkup`.
import i18next, { type i18n as I18nInstance } from 'i18next';
import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { describe, expect, it } from 'vitest';
import en from '@/i18n/locales/en/service.json';
import he from '@/i18n/locales/he/service.json';
import { StaffApiError } from '../api/staff';
import type { StaffHistoryEntry, StaffTicketDetail, TicketStatus } from '../api/staffTypes';
import {
  actionKey,
  addFormValues,
  allowedTargets,
  buildDetailRows,
  categoryName,
  codeOf,
  editFormValues,
  entryFailureOf,
  entryTimes,
  errorKeyOf,
  formatJerusalemMinute,
  instantMs,
  isApproval,
  languageOf,
  parseTicketNumber,
  photoView,
  screenState,
  sortHistory,
  statusChangeBody,
  statusFailureOf,
  withEntry,
} from './ticketDetailModel';
import {
  AssignedTechnicianEditor,
  HistoryEntryFormView,
  HistorySection,
  PhotoSection,
  StatusActions,
  TicketFields,
} from './ticketDetailSections';

// --- fixtures --------------------------------------------------------------------------------------

function entryOf(overrides: Partial<StaffHistoryEntry> = {}): StaffHistoryEntry {
  return {
    id: 'e1',
    entry_text: 'Replaced the washer',
    technician_name: 'Dana',
    created_at: '2025-07-01T09:05:00Z',
    edited_at: null,
    ...overrides,
  };
}

function ticketOf(overrides: Partial<StaffTicketDetail> = {}): StaffTicketDetail {
  return {
    ticket_number: 12345,
    created_at: '2025-07-01T09:05:00Z',
    customer_name: 'Ruth Levi',
    city: 'Haifa',
    apartment: '12',
    category: { id: 'c1', name_he: 'דליפה / סתימה', name_en: 'Leak or Blockage' },
    status: 'received',
    assigned_technician_name: '',
    urgent: false,
    phone: '0501234567',
    description: 'The tap leaks',
    has_photo: false,
    history: [],
    updated_at: '2025-07-01T09:05:00Z',
    ...overrides,
  };
}

function instanceFor(language: 'he' | 'en'): I18nInstance {
  const instance = i18next.createInstance();
  void instance.init({
    resources: { he: { service: he }, en: { service: en } },
    lng: language,
    defaultNS: 'service',
    ns: ['service'],
    interpolation: { escapeValue: false },
    initAsync: false,
    react: { useSuspense: false },
  });
  return instance;
}

function render(language: 'he' | 'en', element: ReactElement): string {
  return renderToStaticMarkup(createElement(I18nextProvider, { i18n: instanceFor(language) }, element));
}

/** A translation key that was not found is rendered as the key itself. */
function expectNoMissingKeys(markup: string): void {
  expect(markup).not.toMatch(/\b(management|errors|a11y|status)\.[a-zA-Z_.]+/);
}

const LANGUAGES = ['he', 'en'] as const;
const noop = (): void => undefined;

// --- the model -------------------------------------------------------------------------------------

describe('parseTicketNumber', () => {
  it('accepts 5 digits from 10000 to 99999 and nothing else', () => {
    expect(parseTicketNumber('10000')).toBe(10000);
    expect(parseTicketNumber('99999')).toBe(99999);
    for (const bad of ['09999', '9999', '100000', '1234a', ' 12345', '+12345', '12345.0', '', undefined, null]) {
      expect(parseTicketNumber(bad)).toBeNull();
    }
  });
});

describe('formatJerusalemMinute (R16.1)', () => {
  it('shows the Asia/Jerusalem wall clock to the minute, in summer and winter time', () => {
    expect(formatJerusalemMinute('2025-07-01T09:05:30Z')).toBe('01/07/2025 12:05');
    expect(formatJerusalemMinute('2025-01-15T09:05:00Z')).toBe('15/01/2025 11:05');
  });

  it('crosses midnight and shows 00 for the hour, not 24', () => {
    expect(formatJerusalemMinute('2025-01-15T22:30:00Z')).toBe('16/01/2025 00:30');
  });

  it('reads an instant with an offset and one with no zone (UTC)', () => {
    expect(formatJerusalemMinute('2025-07-01T12:05:00+03:00')).toBe('01/07/2025 12:05');
    expect(formatJerusalemMinute('2025-07-01T09:05:00')).toBe('01/07/2025 12:05');
    expect(formatJerusalemMinute('2025-07-01T09:05:00.123456')).toBe('01/07/2025 12:05');
  });

  it('returns text that is not an instant unchanged', () => {
    expect(formatJerusalemMinute('not a time')).toBe('not a time');
    expect(Number.isNaN(instantMs('not a time'))).toBe(true);
  });
});

describe('language and names', () => {
  it('reads the language', () => {
    expect(languageOf('en')).toBe('en');
    expect(languageOf('en-US')).toBe('en');
    expect(languageOf('he')).toBe('he');
    expect(languageOf(undefined)).toBe('he');
  });

  it('names the category in the current language, falling back to the other', () => {
    const category = { name_he: 'דליפה', name_en: 'Leak' };
    expect(categoryName(category, 'he')).toBe('דליפה');
    expect(categoryName(category, 'en')).toBe('Leak');
    expect(categoryName({ name_he: 'דליפה', name_en: ' ' }, 'en')).toBe('דליפה');
  });
});

describe('buildDetailRows (R16.1)', () => {
  it('lists every field in reading order and leaves out SOS when the ticket is not urgent', () => {
    const rows = buildDetailRows(ticketOf(), 'en');
    expect(rows.map((row) => row.key)).toEqual([
      'number',
      'status',
      'customer',
      'phone',
      'city',
      'apartment',
      'category',
      'description',
      'created',
    ]);
    expect(rows.find((row) => row.key === 'created')?.text).toBe('01/07/2025 12:05');
    expect(rows.find((row) => row.key === 'category')?.text).toBe('Leak or Blockage');
    expect(rows.find((row) => row.key === 'customer')?.text).toBe('Ruth Levi');
  });

  it('adds the SOS row, after the status, only when the urgent flag is set', () => {
    const rows = buildDetailRows(ticketOf({ urgent: true }), 'he');
    expect(rows.map((row) => row.key).slice(0, 3)).toEqual(['number', 'status', 'sos']);
  });
});

describe('status actions (R15.9, R15.11)', () => {
  it('offers only the targets Requirement 14 allows staff', () => {
    expect(allowedTargets('received')).toEqual(['in_progress']);
    expect(allowedTargets('in_progress')).toEqual(['closed']);
    expect(allowedTargets('closed')).toEqual(['in_progress']);
    expect(allowedTargets('cancelled')).toEqual([]);
  });

  it('names each allowed move and none of the others', () => {
    expect(actionKey('received', 'in_progress')).toBe('management.tickets.actions.toInProgress');
    expect(actionKey('in_progress', 'closed')).toBe('management.tickets.actions.close');
    expect(actionKey('closed', 'in_progress')).toBe('management.tickets.actions.reopen');
    expect(actionKey('received', 'closed')).toBeNull();
    expect(actionKey('received', 'cancelled')).toBeNull();
    expect(actionKey('cancelled', 'in_progress')).toBeNull();
  });

  it('sends the displayed status as the expected status', () => {
    expect(statusChangeBody('in_progress', 'closed', '')).toEqual({ expected_status: 'in_progress', target_status: 'closed' });
  });

  it('sends the trimmed technician name only on the approval step', () => {
    expect(isApproval('received', 'in_progress')).toBe(true);
    expect(isApproval('closed', 'in_progress')).toBe(false);
    expect(statusChangeBody('received', 'in_progress', '  Dana ')).toEqual({
      expected_status: 'received',
      target_status: 'in_progress',
      assigned_technician_name: 'Dana',
    });
    expect(statusChangeBody('received', 'in_progress', '   ')).toEqual({ expected_status: 'received', target_status: 'in_progress' });
    expect(statusChangeBody('closed', 'in_progress', 'Dana')).toEqual({ expected_status: 'closed', target_status: 'in_progress' });
  });

  it('keeps the name of the failed action, so a reload that changes the status cannot lose it (R15.10)', () => {
    const failure = statusFailureOf('in_progress', 'closed', new StaffApiError('ticket_status_changed'));
    expect(failure).toEqual({ actionKey: 'management.tickets.actions.close', code: 'ticket_status_changed' });
    expect(statusFailureOf('received', 'in_progress', new TypeError('x')).code).toBe('generic');
  });
});

describe('error codes', () => {
  it('maps a known code to its text key and anything else to the generic text', () => {
    expect(errorKeyOf('invalid_history_text')).toBe('errors.invalid_history_text');
    expect(errorKeyOf('network')).toBe('errors.generic');
    expect(errorKeyOf('something_new')).toBe('errors.generic');
    expect(codeOf(new StaffApiError('ticket_not_found'))).toBe('ticket_not_found');
    expect(codeOf(new Error('x'))).toBe('generic');
  });
});

describe('history (R16.4, R16.6, R16.9)', () => {
  it('prefills the add form with the acting username and the edit form with the stored values', () => {
    expect(addFormValues('avi')).toEqual({ entry_text: '', technician_name: 'avi' });
    expect(addFormValues(undefined)).toEqual({ entry_text: '', technician_name: '' });
    expect(editFormValues(entryOf())).toEqual({ entry_text: 'Replaced the washer', technician_name: 'Dana' });
  });

  it('sorts oldest first and keeps the given order for equal times', () => {
    const a = entryOf({ id: 'a', created_at: '2025-07-02T10:00:00Z' });
    const b = entryOf({ id: 'b', created_at: '2025-07-01T10:00:00Z' });
    const c = entryOf({ id: 'c', created_at: '2025-07-01T10:00:00Z' });
    expect(sortHistory([a, b, c]).map((entry) => entry.id)).toEqual(['b', 'c', 'a']);
  });

  it('adds a new entry and replaces an existing one by id', () => {
    const first = entryOf({ id: 'a', created_at: '2025-07-01T10:00:00Z' });
    const ticket = ticketOf({ history: [first] });
    const second = entryOf({ id: 'b', created_at: '2025-07-03T10:00:00Z' });
    const added = withEntry(ticket, second);
    expect(added.history.map((entry) => entry.id)).toEqual(['a', 'b']);
    const edited = withEntry(added, { ...first, entry_text: 'new', edited_at: '2025-07-04T10:00:00Z' });
    expect(edited.history).toHaveLength(2);
    expect(edited.history[0]?.entry_text).toBe('new');
    expect(ticket.history).toHaveLength(1);
  });

  it('shows the last-edited time only for an edited entry', () => {
    expect(entryTimes(entryOf()).edited).toBeNull();
    expect(entryTimes(entryOf({ edited_at: '2025-07-01T10:30:00Z' }))).toEqual({
      created: '01/07/2025 12:05',
      edited: '01/07/2025 13:30',
    });
  });

  it('splits a 422 into one code per failing field', () => {
    const both = entryFailureOf(
      new StaffApiError('invalid_history_text', { entry_text: 'invalid_history_text', technician_name: 'invalid_technician_name' }),
    );
    expect(both).toEqual({
      fields: { entry_text: 'invalid_history_text', technician_name: 'invalid_technician_name' },
      code: null,
    });
    const one = entryFailureOf(new StaffApiError('invalid_technician_name', { technician_name: 'invalid_technician_name' }));
    expect(one.fields).toEqual({ technician_name: 'invalid_technician_name' });
  });

  it('gives a form-level code when no form field failed', () => {
    expect(entryFailureOf(new StaffApiError('ticket_not_found'))).toEqual({ fields: {}, code: 'ticket_not_found' });
    expect(entryFailureOf(new StaffApiError('history_entry_not_found', { other: 'x' })).code).toBe('history_entry_not_found');
    expect(entryFailureOf(new TypeError('offline'))).toEqual({ fields: {}, code: 'generic' });
  });
});

describe('photoView (R16.1)', () => {
  it('shows nothing when the ticket has no photo, whatever else is set', () => {
    expect(photoView({ hasPhoto: false, status: 'pending', error: null, url: null })).toEqual({ kind: 'none' });
    expect(photoView({ hasPhoto: false, status: 'error', error: new StaffApiError('x'), url: 'blob:x' })).toEqual({ kind: 'none' });
  });

  it('loads, shows, and offers a retry only where retrying can help', () => {
    expect(photoView({ hasPhoto: true, status: 'pending', error: null, url: null })).toEqual({ kind: 'loading' });
    expect(photoView({ hasPhoto: true, status: 'success', error: null, url: null })).toEqual({ kind: 'loading' });
    expect(photoView({ hasPhoto: true, status: 'success', error: null, url: 'blob:x' })).toEqual({ kind: 'ready', url: 'blob:x' });
    expect(photoView({ hasPhoto: true, status: 'error', error: new StaffApiError('network'), url: null })).toEqual({
      kind: 'error',
      code: 'network',
      retryable: true,
    });
    expect(photoView({ hasPhoto: true, status: 'error', error: new StaffApiError('ticket_not_found'), url: null })).toMatchObject({
      retryable: false,
    });
  });
});

describe('screenState', () => {
  it('covers loading, error, not found, ready and a failed reload', () => {
    expect(screenState({ numberValid: false, hasData: false, error: null })).toEqual({ kind: 'not-found' });
    expect(screenState({ numberValid: true, hasData: false, error: new StaffApiError('ticket_not_found') })).toEqual({ kind: 'not-found' });
    expect(screenState({ numberValid: true, hasData: false, error: null })).toEqual({ kind: 'loading' });
    expect(screenState({ numberValid: true, hasData: false, error: new StaffApiError('network') })).toEqual({ kind: 'error', code: 'network' });
    expect(screenState({ numberValid: true, hasData: true, error: null })).toEqual({ kind: 'ready', staleErrorCode: null });
    expect(screenState({ numberValid: true, hasData: true, error: new StaffApiError('network') })).toEqual({
      kind: 'ready',
      staleErrorCode: 'network',
    });
  });
});

// --- the markup ------------------------------------------------------------------------------------

describe('TicketFields markup', () => {
  it.each(LANGUAGES)('shows every field with its label in %s', (language) => {
    const markup = render(language, createElement(TicketFields, { ticket: ticketOf({ urgent: true }), language }));
    expectNoMissingKeys(markup);
    for (const text of ['12345', 'Ruth Levi', '0501234567', 'Haifa', '12', 'The tap leaks', '01/07/2025 12:05']) {
      expect(markup).toContain(text);
    }
    expect(markup).toContain(language === 'en' ? 'Leak or Blockage' : 'דליפה / סתימה');
    expect(markup).toContain(language === 'en' ? en.management.tickets.sos : he.management.tickets.sos);
    expect(markup).toContain(language === 'en' ? en.status.received : he.status.received);
  });

  it('shows no SOS indicator for a ticket that is not urgent', () => {
    const markup = render('en', createElement(TicketFields, { ticket: ticketOf(), language: 'en' }));
    expect(markup).not.toContain('SOS');
  });

  it('shows markup typed by a user literally and keeps line breaks (R9.13)', () => {
    const ticket = ticketOf({
      city: '<b>x</b>',
      apartment: '<script>alert(1)</script>',
      description: '<img src=x onerror=alert(1)>\nsecond line',
    });
    const markup = render('en', createElement(TicketFields, { ticket, language: 'en' }));
    expect(markup).not.toContain('<b>x</b>');
    expect(markup).not.toContain('<script>');
    expect(markup).not.toContain('<img src=x');
    expect(markup).toContain('&lt;b&gt;x&lt;/b&gt;');
    expect(markup).toContain('&lt;script&gt;');
    expect(markup).toContain('second line');
    expect(markup).toContain('ticket-detail-description');
  });

  it('shows the no-description text for an empty description', () => {
    const markup = render('en', createElement(TicketFields, { ticket: ticketOf({ description: '  ' }), language: 'en' }));
    expect(markup).toContain(en.management.ticket.noDescription);
  });
});

describe('PhotoSection markup (R16.1)', () => {
  const props = { ticketNumber: 12345, categoryLabel: 'Leak or Blockage', onRetry: noop };

  it('renders nothing for a ticket without a photo: no preview, no placeholder', () => {
    expect(render('en', createElement(PhotoSection, { ...props, view: { kind: 'none' } }))).toBe('');
  });

  it.each(LANGUAGES)('renders a preview that opens the stored photo, with a text alternative, in %s', (language) => {
    const markup = render(language, createElement(PhotoSection, { ...props, view: { kind: 'ready', url: 'blob:abc' } }));
    expectNoMissingKeys(markup);
    expect(markup).toContain('<a ');
    expect(markup).toContain('href="blob:abc"');
    expect(markup).toContain('target="_blank"');
    expect(markup).toContain('rel="noopener noreferrer"');
    expect(markup).toContain('src="blob:abc"');
    expect(markup).toMatch(/alt="[^"]*12345[^"]*"/);
  });

  it('renders no image while loading or after an error, and a retry control only when it can help', () => {
    const loading = render('en', createElement(PhotoSection, { ...props, view: { kind: 'loading' } }));
    expect(loading).not.toContain('<img');
    const failed = render('en', createElement(PhotoSection, { ...props, view: { kind: 'error', code: 'network', retryable: true } }));
    expect(failed).not.toContain('<img');
    expect(failed).toContain(en.management.common.retry);
    const gone = render('en', createElement(PhotoSection, { ...props, view: { kind: 'error', code: 'ticket_not_found', retryable: false } }));
    expect(gone).not.toContain(en.management.common.retry);
  });
});

describe('StatusActions markup', () => {
  function actions(status: TicketStatus, extra: Record<string, unknown> = {}): string {
    return render(
      'en',
      createElement(StatusActions, {
        ticketNumber: 12345,
        status,
        pending: null,
        technicianName: '',
        onTechnicianNameChange: noop,
        onChange: noop,
        failure: null,
        ...extra,
      }),
    );
  }

  it('offers the approval step with the optional technician name for a received ticket', () => {
    const markup = actions('received');
    expect(markup).toContain(en.management.tickets.actions.toInProgress);
    expect(markup).toContain(en.management.ticket.statusSection.technicianOnApproval);
    expect(markup).toMatch(/<label[^>]*for="[^"]+-name"/);
    expect(markup.match(/<button/g)).toHaveLength(1);
  });

  it('offers close for an in-progress ticket and reopen for a closed one, with no name field', () => {
    const inProgress = actions('in_progress');
    expect(inProgress).toContain(en.management.tickets.actions.close);
    expect(inProgress).not.toContain('<input');
    const closed = actions('closed');
    expect(closed).toContain(en.management.tickets.actions.reopen);
    expect(closed).not.toContain('<input');
  });

  it('offers no action for a cancelled ticket (R15.11)', () => {
    const markup = actions('cancelled');
    expect(markup).not.toContain('<button');
    expect(markup).toContain(en.management.tickets.actions.none);
  });

  it('disables the controls while a change is in flight', () => {
    expect(actions('in_progress', { pending: 'closed' })).toMatch(/<button[^>]*disabled/);
  });

  it.each(LANGUAGES)('names the failed action and the reason after a failure, in %s', (language) => {
    const failure = statusFailureOf('in_progress', 'closed', new StaffApiError('ticket_status_changed'));
    const markup = render(
      language,
      createElement(StatusActions, {
        ticketNumber: 12345,
        status: 'closed',
        pending: null,
        technicianName: '',
        onTechnicianNameChange: noop,
        onChange: noop,
        failure,
      }),
    );
    expectNoMissingKeys(markup);
    expect(markup).toContain('role="alert"');
    expect(markup).toContain(language === 'en' ? en.management.tickets.actions.close : he.management.tickets.actions.close);
    expect(markup).toContain(language === 'en' ? en.errors.ticket_status_changed : he.errors.ticket_status_changed);
  });
});

describe('AssignedTechnicianEditor markup', () => {
  it.each(LANGUAGES)('labels the field, keeps the typed value and ties the error to it, in %s', (language) => {
    const markup = render(
      language,
      createElement(AssignedTechnicianEditor, {
        value: 'x'.repeat(101),
        saving: false,
        errorCode: 'invalid_assigned_technician_name',
        saved: false,
        onChange: noop,
        onSave: noop,
      }),
    );
    expectNoMissingKeys(markup);
    expect(markup).toMatch(/<label[^>]*for="([^"]+)-input"/);
    expect(markup).toContain(`value="${'x'.repeat(101)}"`);
    expect(markup).toContain('aria-invalid="true"');
    const describedBy = /aria-describedby="([^"]+)"/.exec(markup)?.[1] ?? '';
    for (const id of describedBy.split(' ')) expect(markup).toContain(`id="${id}"`);
  });

  it('shows the saved confirmation only when there is no error', () => {
    const base = { value: 'Dana', saving: false, onChange: noop, onSave: noop };
    expect(render('en', createElement(AssignedTechnicianEditor, { ...base, errorCode: null, saved: true }))).toContain(
      en.management.ticket.assigned.saved,
    );
    expect(render('en', createElement(AssignedTechnicianEditor, { ...base, errorCode: null, saved: false }))).not.toContain(
      en.management.ticket.assigned.saved,
    );
  });
});

describe('HistoryEntryFormView markup (R16.4, R16.6)', () => {
  const base = { idPrefix: 'f', titleKey: 'management.ticket.history.add', pending: false, onChange: noop, onSubmit: noop, onCancel: noop };

  it.each(LANGUAGES)('keeps the typed values and puts one message beside each failing field, in %s', (language) => {
    const markup = render(
      language,
      createElement(HistoryEntryFormView, {
        ...base,
        values: { entry_text: 'x'.repeat(2001), technician_name: '' },
        failure: { fields: { entry_text: 'invalid_history_text', technician_name: 'invalid_technician_name' }, code: null },
      }),
    );
    expectNoMissingKeys(markup);
    expect(markup).toContain('x'.repeat(2001));
    expect(markup).toContain('aria-describedby="f-entry_text-error"');
    expect(markup).toContain('aria-describedby="f-technician_name-error"');
    expect(markup).toContain('id="f-entry_text-error"');
    expect(markup).toContain('id="f-technician_name-error"');
    expect(markup).toContain(language === 'en' ? en.errors.invalid_history_text : he.errors.invalid_history_text);
    expect(markup).toContain(language === 'en' ? en.errors.invalid_technician_name : he.errors.invalid_technician_name);
  });

  it('labels both inputs and marks them required', () => {
    const markup = render('en', createElement(HistoryEntryFormView, { ...base, values: addFormValues('avi'), failure: null }));
    expect(markup).toMatch(/<label[^>]*for="f-entry_text"/);
    expect(markup).toMatch(/<label[^>]*for="f-technician_name"/);
    expect(markup).toContain('value="avi"');
    expect(markup.match(/aria-required="true"/g)).toHaveLength(2);
    expect(markup).not.toContain('aria-describedby');
    expect(markup).not.toContain('role="alert"');
  });

  it('shows a form-level message when no field failed', () => {
    const markup = render(
      'en',
      createElement(HistoryEntryFormView, {
        ...base,
        values: addFormValues('avi'),
        failure: { fields: {}, code: 'ticket_not_found' },
      }),
    );
    expect(markup).toContain('role="alert"');
    expect(markup).toContain(en.errors.ticket_not_found);
  });

  it('disables the form while a save is in flight', () => {
    const markup = render('en', createElement(HistoryEntryFormView, { ...base, values: addFormValues('avi'), failure: null, pending: true }));
    expect(markup.match(/disabled/g)?.length).toBeGreaterThanOrEqual(4);
  });
});

describe('HistorySection markup (R16.4, R16.9)', () => {
  const props = { username: 'avi', onOpenAdd: noop, onOpenEdit: noop, onClose: noop, onSubmit: () => Promise.resolve(null) };

  it.each(LANGUAGES)('lists the entries oldest first and shows the edited time only for an edited entry, in %s', (language) => {
    const entries = [
      entryOf({ id: 'a', entry_text: 'FIRST', created_at: '2025-07-01T09:05:00Z', edited_at: null }),
      entryOf({ id: 'b', entry_text: 'SECOND', created_at: '2025-07-02T09:05:00Z', edited_at: '2025-07-03T10:15:00Z' }),
    ];
    const markup = render(language, createElement(HistorySection, { ...props, entries, mode: { kind: 'closed' } }));
    expectNoMissingKeys(markup);
    expect(markup.indexOf('FIRST')).toBeGreaterThan(-1);
    expect(markup.indexOf('FIRST')).toBeLessThan(markup.indexOf('SECOND'));
    expect(markup).toContain('01/07/2025 12:05');
    expect(markup).toContain('03/07/2025 13:15');
    const editedLabel = (language === 'en' ? en : he).management.ticket.history.edited.replace('{{when}}', '');
    expect(markup.split(editedLabel)).toHaveLength(2);
  });

  it('shows the empty message and the internal-log note for a ticket with no history', () => {
    const markup = render('en', createElement(HistorySection, { ...props, entries: [], mode: { kind: 'closed' } }));
    expect(markup).toContain(en.management.ticket.history.empty);
    expect(markup).toContain(en.management.ticket.history.internalNote);
    expect(markup).not.toContain('<form');
  });

  it('opens the add form with the technician name prefilled from the acting username', () => {
    const markup = render('en', createElement(HistorySection, { ...props, entries: [], mode: { kind: 'add' } }));
    expect(markup).toContain('<form');
    expect(markup).toContain('value="avi"');
  });

  it('opens the edit form of one entry with its stored values, and shows the others as text', () => {
    const entries = [
      entryOf({ id: 'a', entry_text: 'FIRST', technician_name: 'Dana' }),
      entryOf({ id: 'b', entry_text: 'SECOND', technician_name: 'Moshe', created_at: '2025-07-02T09:05:00Z' }),
    ];
    const markup = render('en', createElement(HistorySection, { ...props, entries, mode: { kind: 'edit', id: 'b' } }));
    expect(markup.match(/<form/g)).toHaveLength(1);
    expect(markup).toContain('value="Moshe"');
    expect(markup).toContain('>SECOND</textarea>');
    expect(markup).not.toContain('value="Dana"');
    expect(markup).toContain('FIRST');
  });

  it('shows entry text literally (R9.13)', () => {
    const entries = [entryOf({ entry_text: '<script>x</script>', technician_name: '<b>y</b>' })];
    const markup = render('en', createElement(HistorySection, { ...props, entries, mode: { kind: 'closed' } }));
    expect(markup).not.toContain('<script>');
    expect(markup).not.toContain('<b>y</b>');
    expect(markup).toContain('&lt;script&gt;');
  });
});
