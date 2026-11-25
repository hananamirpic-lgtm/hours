// Tests of the Resolve flow of the management ticket detail page (task 30.9; R29.4, R29.5, R29.7, R23.12): the
// pure model, and the markup of the form and the parts dialog in both languages. No DOM and no testing library:
// `renderToStaticMarkup`.
import i18next, { type i18n as I18nInstance } from 'i18next';
import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { describe, expect, it } from 'vitest';
import en from '@/i18n/locales/en/service.json';
import he from '@/i18n/locales/he/service.json';
import { StaffApiError } from '../api/staff';
import type { ActivePart } from '../api/staffParts';
import {
  RESOLVE_PARTS_MAX,
  canChoosePart,
  chosenPartsText,
  firstFailingField,
  localFailure,
  orderedChosenIds,
  partName,
  partsView,
  reloadsTicket,
  replacedPartNames,
  resolutionBody,
  resolveFailureOf,
  resolveInitialValues,
  resolveLinkPath,
  resolveStatusBody,
  resolveUrlDecision,
  togglePart,
  validateResolve,
  withPartsReplaced,
  type PartsView,
  type ResolveValues,
} from './resolveModel';
import { ResolvePanelView, ResolvePartsDialog, type ResolvePanelViewProps } from './resolveSections';

const locales = { en, he };

// --- fixtures --------------------------------------------------------------------------------------

function part(id: string, name_he: string, name_en: string): ActivePart {
  return { id, name_he, name_en, is_active: true };
}

const PARTS: readonly ActivePart[] = [
  part('p1', 'אטם', 'Washer'),
  part('p2', 'ברז', 'Tap'),
  part('p3', 'צינור', 'Pipe'),
];

const READY: PartsView = { kind: 'ready', parts: PARTS };

function valuesOf(overrides: Partial<ResolveValues> = {}): ResolveValues {
  return { entry_text: 'Replaced the washer', technician_name: 'Dana', parts_replaced: false, part_ids: [], ...overrides };
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
const text = { en, he };

function panel(language: 'he' | 'en', overrides: Partial<ResolvePanelViewProps> = {}): string {
  return render(
    language,
    createElement(ResolvePanelView, {
      idPrefix: 'r',
      language,
      values: valuesOf({ entry_text: '' }),
      failure: null,
      pending: false,
      partsView: READY,
      dialogOpen: false,
      onText: noop,
      onPartsReplaced: noop,
      onOpenParts: noop,
      onPartsDone: noop,
      onPartsCancel: noop,
      onRetryParts: noop,
      onSubmit: noop,
      onCancel: noop,
      ...overrides,
    }),
  );
}

// --- the form values and the request ---------------------------------------------------------------

describe('resolveInitialValues (R29.4)', () => {
  it('prefills the technician with the assigned technician, else the acting username', () => {
    expect(resolveInitialValues('Moshe', 'avi')).toEqual({
      entry_text: '',
      technician_name: 'Moshe',
      parts_replaced: false,
      part_ids: [],
    });
    expect(resolveInitialValues('', 'avi').technician_name).toBe('avi');
    expect(resolveInitialValues('   ', 'avi').technician_name).toBe('avi');
    expect(resolveInitialValues(null, 'avi').technician_name).toBe('avi');
    expect(resolveInitialValues(undefined, undefined).technician_name).toBe('');
  });
});

describe('validateResolve (R29.1, R29.3)', () => {
  it('accepts a history text, a technician name and no part', () => {
    expect(validateResolve(valuesOf())).toEqual({});
  });

  it('refuses a blank or too long history text', () => {
    expect(validateResolve(valuesOf({ entry_text: '' }))).toEqual({ entry_text: 'invalid_history_text' });
    expect(validateResolve(valuesOf({ entry_text: ' \n\t ' }))).toEqual({ entry_text: 'invalid_history_text' });
    expect(validateResolve(valuesOf({ entry_text: 'x'.repeat(2000) }))).toEqual({});
    expect(validateResolve(valuesOf({ entry_text: 'x'.repeat(2001) }))).toEqual({ entry_text: 'invalid_history_text' });
    expect(validateResolve(valuesOf({ entry_text: ` ${'x'.repeat(2000)} ` }))).toEqual({});
  });

  it('refuses a blank or too long technician name', () => {
    expect(validateResolve(valuesOf({ technician_name: '  ' }))).toEqual({ technician_name: 'invalid_technician_name' });
    expect(validateResolve(valuesOf({ technician_name: 'x'.repeat(100) }))).toEqual({});
    expect(validateResolve(valuesOf({ technician_name: 'x'.repeat(101) }))).toEqual({ technician_name: 'invalid_technician_name' });
  });

  it('requires a part only when the box is ticked, and at most 20', () => {
    expect(validateResolve(valuesOf({ parts_replaced: true }))).toEqual({ part_ids: 'part_required' });
    expect(validateResolve(valuesOf({ parts_replaced: true, part_ids: ['p1'] }))).toEqual({});
    expect(validateResolve(valuesOf({ parts_replaced: false, part_ids: [] }))).toEqual({});
    const many = Array.from({ length: RESOLVE_PARTS_MAX + 1 }, (_, index) => `p${index}`);
    expect(validateResolve(valuesOf({ parts_replaced: true, part_ids: many }))).toEqual({ part_ids: 'invalid_part' });
    expect(validateResolve(valuesOf({ parts_replaced: true, part_ids: many.slice(0, RESOLVE_PARTS_MAX) }))).toEqual({});
  });

  it('reports every failing field at once', () => {
    expect(validateResolve({ entry_text: '', technician_name: '', parts_replaced: true, part_ids: [] })).toEqual({
      entry_text: 'invalid_history_text',
      technician_name: 'invalid_technician_name',
      part_ids: 'part_required',
    });
  });
});

describe('the resolution body', () => {
  it('trims both texts and sends the chosen parts once each', () => {
    expect(
      resolutionBody(valuesOf({ entry_text: '  Done \n', technician_name: ' Dana ', parts_replaced: true, part_ids: ['p1', 'p2', 'p1'] })),
    ).toEqual({ entry_text: 'Done', technician_name: 'Dana', parts_replaced: true, part_ids: ['p1', 'p2'] });
  });

  it('sends no part when the box is not ticked, whatever the list holds', () => {
    expect(resolutionBody(valuesOf({ parts_replaced: false, part_ids: ['p1'] })).part_ids).toEqual([]);
  });

  it('is the status change to closed, with the status the page shows as expected status', () => {
    expect(resolveStatusBody('in_progress', valuesOf())).toEqual({
      expected_status: 'in_progress',
      target_status: 'closed',
      resolution: { entry_text: 'Replaced the washer', technician_name: 'Dana', parts_replaced: false, part_ids: [] },
    });
  });

  it('clears the chosen parts when the box is unticked and keeps them when it is ticked', () => {
    const chosen = valuesOf({ parts_replaced: true, part_ids: ['p1', 'p2'] });
    expect(withPartsReplaced(chosen, false)).toEqual({ ...chosen, parts_replaced: false, part_ids: [] });
    expect(withPartsReplaced(chosen, true).part_ids).toEqual(['p1', 'p2']);
    expect(withPartsReplaced(valuesOf(), true)).toEqual({ ...valuesOf(), parts_replaced: true, part_ids: [] });
  });
});

describe('resolve failures', () => {
  it('maps a 422 to one code per failing field of the form', () => {
    const failure = resolveFailureOf(
      new StaffApiError('invalid_history_text', {
        entry_text: 'invalid_history_text',
        technician_name: 'invalid_technician_name',
        parts_replaced: 'invalid_request',
        part_ids: 'part_required',
      }),
    );
    expect(failure).toEqual({
      fields: {
        entry_text: 'invalid_history_text',
        technician_name: 'invalid_technician_name',
        parts_replaced: 'invalid_request',
        part_ids: 'part_required',
      },
      code: null,
    });
    expect(reloadsTicket(failure)).toBe(false);
    expect(resolveFailureOf(new StaffApiError('invalid_part', { part_ids: 'invalid_part' })).fields).toEqual({ part_ids: 'invalid_part' });
  });

  it('gives a form-level code to a failure of the status change itself and reloads the ticket', () => {
    for (const code of ['ticket_status_changed', 'invalid_transition', 'resolution_required', 'ticket_not_found']) {
      const failure = resolveFailureOf(new StaffApiError(code));
      expect(failure).toEqual({ fields: {}, code });
      expect(reloadsTicket(failure)).toBe(true);
    }
    expect(resolveFailureOf(new StaffApiError('invalid_part', { somethingElse: 'x' })).code).toBe('invalid_part');
    expect(resolveFailureOf(new TypeError('offline'))).toEqual({ fields: {}, code: 'generic' });
  });

  it('finds the first failing field in reading order, and none when nothing failed', () => {
    expect(firstFailingField({ part_ids: 'part_required', technician_name: 'invalid_technician_name' })).toBe('technician_name');
    expect(firstFailingField({ part_ids: 'part_required', entry_text: 'invalid_history_text' })).toBe('entry_text');
    expect(firstFailingField({ part_ids: 'part_required' })).toBe('part_ids');
    expect(firstFailingField({})).toBeNull();
    expect(localFailure({ entry_text: 'invalid_history_text' })).toEqual({ fields: { entry_text: 'invalid_history_text' }, code: null });
  });
});

// --- the parts -------------------------------------------------------------------------------------

describe('part selection', () => {
  it('names a part in the current language, falling back to the other when it is blank', () => {
    expect(partName(PARTS[0] as ActivePart, 'he')).toBe('אטם');
    expect(partName(PARTS[0] as ActivePart, 'en')).toBe('Washer');
    expect(partName({ name_he: 'אטם', name_en: ' ' }, 'en')).toBe('אטם');
  });

  it('toggles a part on and off without changing the input', () => {
    const start = ['p1'];
    expect(togglePart(start, 'p2')).toEqual(['p1', 'p2']);
    expect(togglePart(start, 'p1')).toEqual([]);
    expect(togglePart(togglePart(start, 'p3'), 'p3')).toEqual(start);
    expect(start).toEqual(['p1']);
  });

  it('stops adding at the maximum but still lets a chosen part be removed', () => {
    const full = Array.from({ length: RESOLVE_PARTS_MAX }, (_, index) => `p${index}`);
    expect(canChoosePart(full, 'new')).toBe(false);
    expect(canChoosePart(full, 'p3')).toBe(true);
    expect(canChoosePart(full.slice(1), 'new')).toBe(true);
  });

  it('orders the chosen parts as the list gives them and shows them as comma separated text', () => {
    expect(orderedChosenIds(PARTS, ['p3', 'p1', 'gone'])).toEqual(['p1', 'p3']);
    expect(chosenPartsText(PARTS, ['p3', 'p1'], 'en')).toBe('Washer, Pipe');
    expect(chosenPartsText(PARTS, ['p3', 'p1'], 'he')).toBe('אטם, צינור');
    expect(chosenPartsText(PARTS, [], 'en')).toBe('');
    expect(chosenPartsText([], ['p1'], 'en')).toBe('');
  });

  it('lists the replaced parts of a ticket in the current language, and none when absent (R29.7)', () => {
    const replaced = [
      { id: 'p1', name_he: 'אטם', name_en: 'Washer' },
      { id: 'p2', name_he: 'ברז', name_en: 'Tap' },
    ];
    expect(replacedPartNames(replaced, 'en')).toEqual(['Washer', 'Tap']);
    expect(replacedPartNames(replaced, 'he')).toEqual(['אטם', 'ברז']);
    expect(replacedPartNames([], 'en')).toEqual([]);
    expect(replacedPartNames(undefined, 'en')).toEqual([]);
    expect(replacedPartNames(null, 'en')).toEqual([]);
  });

  it('shows loading, the failure code, or the parts', () => {
    expect(partsView({ status: 'pending', data: undefined, error: null })).toEqual({ kind: 'loading' });
    expect(partsView({ status: 'error', data: undefined, error: new StaffApiError('network') })).toEqual({ kind: 'error', code: 'network' });
    expect(partsView({ status: 'success', data: PARTS, error: null })).toEqual({ kind: 'ready', parts: PARTS });
    // Parts already loaded stay on screen when a reload fails.
    expect(partsView({ status: 'error', data: PARTS, error: new StaffApiError('network') })).toEqual({ kind: 'ready', parts: PARTS });
  });
});

// --- the address (R29.5) ---------------------------------------------------------------------------

describe('the resolve address', () => {
  it('links the list action to the detail page with resolve=1', () => {
    expect(resolveLinkPath(10001)).toBe('/tickets/list/10001?resolve=1');
  });

  it('opens the form only for a ticket in progress, and removes the parameter either way', () => {
    expect(resolveUrlDecision('?resolve=1', 'in_progress')).toEqual({ open: true, nextSearch: '' });
    for (const status of ['received', 'closed', 'cancelled'] as const) {
      expect(resolveUrlDecision('?resolve=1', status)).toEqual({ open: false, nextSearch: '' });
    }
  });

  it('ignores any other value but still removes the parameter, and keeps the other parameters', () => {
    expect(resolveUrlDecision('?resolve=0', 'in_progress')).toEqual({ open: false, nextSearch: '' });
    expect(resolveUrlDecision('?resolve=yes', 'in_progress').open).toBe(false);
    expect(resolveUrlDecision('?a=b&resolve=1&c=d', 'in_progress')).toEqual({ open: true, nextSearch: '?a=b&c=d' });
  });

  it('does nothing when the address has no resolve parameter', () => {
    expect(resolveUrlDecision('', 'in_progress')).toEqual({ open: false, nextSearch: null });
    expect(resolveUrlDecision('?a=b', 'in_progress')).toEqual({ open: false, nextSearch: null });
  });
});

// --- the form markup -------------------------------------------------------------------------------

describe('ResolvePanelView markup', () => {
  it.each(LANGUAGES)('shows the labelled, required fields with the technician prefilled, in %s', (language) => {
    const markup = panel(language, { values: valuesOf({ entry_text: '', technician_name: 'avi' }) });
    expectNoMissingKeys(markup);
    expect(markup).toContain(locales[language].management.ticket.resolve.title);
    expect(markup).toMatch(/<label[^>]*for="r-entry_text"/);
    expect(markup).toMatch(/<label[^>]*for="r-technician_name"/);
    expect(markup).toMatch(/<label[^>]*for="r-parts_replaced"/);
    expect(markup).toContain(locales[language].management.ticket.resolve.historyText);
    expect(markup).toContain(text[language].a11y.required);
    expect(markup.match(/aria-required="true"/g)).toHaveLength(2);
    expect(markup).toContain('value="avi"');
    expect(markup).toContain(locales[language].management.ticket.resolve.partsReplaced);
    expect(markup).toContain(locales[language].management.ticket.resolve.submit);
    expect(markup).toContain(text[language].management.common.cancel);
    expect(markup).toMatch(/<textarea[^>]*id="r-entry_text"/);
    expect(markup).not.toContain('role="alert"');
    expect(markup).not.toContain('role="dialog"');
  });

  it('shows no parts button while the box is not ticked', () => {
    const markup = panel('en', { values: valuesOf({ parts_replaced: false }) });
    expect(markup).not.toContain(locales.en.management.ticket.resolve.chooseParts);
    expect(markup).not.toContain('id="r-part_ids"');
    expect(markup).toMatch(/<input[^>]*type="checkbox"(?![^>]*checked)/);
  });

  it.each(LANGUAGES)('shows "Choose parts…" when the box is ticked and no part is chosen, in %s', (language) => {
    const markup = panel(language, { values: valuesOf({ parts_replaced: true }) });
    expectNoMissingKeys(markup);
    expect(markup).toMatch(/<input[^>]*type="checkbox"[^>]*checked/);
    expect(markup).toContain(locales[language].management.ticket.resolve.chooseParts);
    expect(markup).toContain(locales[language].management.ticket.resolve.noPartsChosen);
    expect(markup).toMatch(/<button[^>]*id="r-part_ids"[^>]*aria-haspopup="dialog"/);
  });

  it.each(LANGUAGES)('shows the chosen parts as text beside a Change button, in %s', (language) => {
    const markup = panel(language, { values: valuesOf({ parts_replaced: true, part_ids: ['p3', 'p1'] }) });
    expectNoMissingKeys(markup);
    expect(markup).toContain(language === 'en' ? 'Washer, Pipe' : 'אטם, צינור');
    expect(markup).toContain(`>${locales[language].management.ticket.resolve.changeParts}</button>`);
    expect(markup).not.toContain(locales[language].management.ticket.resolve.chooseParts);
    expect(markup).toContain(locales[language].management.ticket.resolve.chosenParts);
  });

  it('shows a part name typed by staff literally', () => {
    const parts: PartsView = { kind: 'ready', parts: [part('p9', '<b>x</b>', '<b>x</b>')] };
    const markup = panel('en', { values: valuesOf({ parts_replaced: true, part_ids: ['p9'] }), partsView: parts });
    expect(markup).not.toContain('<b>x</b>');
    expect(markup).toContain('&lt;b&gt;x&lt;/b&gt;');
  });

  it.each(LANGUAGES)('keeps the typed values and ties one message to each failing field, in %s', (language) => {
    const longText = 'x'.repeat(2001);
    const markup = panel(language, {
      values: valuesOf({ entry_text: longText, technician_name: '', parts_replaced: true, part_ids: [] }),
      failure: {
        fields: { entry_text: 'invalid_history_text', technician_name: 'invalid_technician_name', part_ids: 'part_required' },
        code: null,
      },
    });
    expectNoMissingKeys(markup);
    expect(markup).toContain(longText);
    expect(markup.match(/aria-invalid="true"/g)).toHaveLength(3);
    for (const id of ['r-entry_text-error', 'r-technician_name-error', 'r-part_ids-error']) {
      expect(markup).toContain(`id="${id}"`);
      expect(markup).toMatch(new RegExp(`aria-describedby="[^"]*${id}`));
    }
    expect(markup).toContain(text[language].errors.invalid_history_text);
    expect(markup).toContain(text[language].errors.invalid_technician_name);
    expect(markup).toContain(text[language].errors.part_required);
    expect(markup).not.toContain('role="alert"');
  });

  it('shows the message of an invalid part beside the part button', () => {
    const markup = panel('en', {
      values: valuesOf({ parts_replaced: true, part_ids: ['p1'] }),
      failure: { fields: { part_ids: 'invalid_part' }, code: null },
    });
    expect(markup).toContain(text.en.errors.invalid_part);
    expect(markup).toMatch(/<button[^>]*id="r-part_ids"[^>]*aria-invalid="true"/);
  });

  it('does not show a part message once the box is unticked', () => {
    const markup = panel('en', { values: valuesOf(), failure: { fields: { part_ids: 'part_required' }, code: null } });
    expect(markup).not.toContain(text.en.errors.part_required);
  });

  it.each(LANGUAGES)('shows a failure of the status change itself above the form and keeps the values, in %s', (language) => {
    const markup = panel(language, {
      values: valuesOf({ entry_text: 'kept text' }),
      failure: { fields: {}, code: 'ticket_status_changed' },
    });
    expectNoMissingKeys(markup);
    expect(markup).toMatch(/<div id="r-alert"[^>]*role="alert"/);
    expect(markup).toContain(locales[language].management.ticket.resolve.failed);
    expect(markup).toContain(text[language].errors.ticket_status_changed);
    expect(markup).toContain('kept text');
    expect(markup).not.toContain('aria-invalid="true"');
  });

  it('disables every control while the request is pending', () => {
    const markup = panel('en', { values: valuesOf({ parts_replaced: true, part_ids: ['p1'] }), pending: true });
    const tags = markup.match(/<(button|input|textarea)\b[^>]*>/g) ?? [];
    expect(tags.length).toBeGreaterThanOrEqual(6);
    for (const tag of tags) expect(tag).toContain('disabled');
    expect(markup).toContain(text.en.management.common.saving);
  });

  it('opens the parts dialog beside the form, not inside it', () => {
    const markup = panel('en', { values: valuesOf({ parts_replaced: true }), dialogOpen: true });
    expect(markup).toContain('role="dialog"');
    expect(markup.indexOf('</form>')).toBeGreaterThan(-1);
    expect(markup.indexOf('</form>')).toBeLessThan(markup.indexOf('role="dialog"'));
  });
});

// --- the parts dialog markup -----------------------------------------------------------------------

function dialog(language: 'he' | 'en', overrides: Partial<Parameters<typeof ResolvePartsDialog>[0]> = {}): string {
  return render(
    language,
    createElement(ResolvePartsDialog, {
      idPrefix: 'r',
      language,
      view: READY,
      initialIds: [],
      onDone: noop,
      onCancel: noop,
      onRetry: noop,
      ...overrides,
    }),
  );
}

describe('ResolvePartsDialog markup (R29.4, R23.12)', () => {
  it.each(LANGUAGES)('is a labelled modal dialog listing the active parts in order, in %s', (language) => {
    const markup = dialog(language, { initialIds: ['p2'] });
    expectNoMissingKeys(markup);
    expect(markup).toContain('role="dialog"');
    expect(markup).toContain('aria-modal="true"');
    const labelledBy = /aria-labelledby="([^"]+)"/.exec(markup)?.[1] ?? '';
    expect(markup).toContain(`id="${labelledBy}"`);
    expect(markup).toContain(locales[language].management.ticket.resolve.parts.title);
    const names = language === 'en' ? ['Washer', 'Tap', 'Pipe'] : ['אטם', 'ברז', 'צינור'];
    const positions = names.map((name) => markup.indexOf(`<bdi>${name}</bdi>`));
    expect(positions.every((position) => position > -1)).toBe(true);
    expect([...positions].sort((a, b) => a - b)).toEqual(positions);
    expect(markup.match(/type="checkbox"/g)).toHaveLength(3);
    // Only the part that was chosen when the dialog opened is checked.
    expect(markup.match(/type="checkbox"[^>]*checked/g)).toHaveLength(1);
    expect(markup).toMatch(/id="r-part-p2"[^>]*checked/);
    expect(markup).toContain(text[language].management.common.cancel);
    expect(markup).toContain(locales[language].management.ticket.resolve.parts.done);
  });

  it('announces the count of chosen parts in a live region', () => {
    const markup = dialog('en', { initialIds: ['p1', 'p3'] });
    expect(markup).toMatch(/<p[^>]*role="status"[^>]*aria-live="polite"[^>]*>Selected: 2 \(up to 20\)<\/p>/);
    expect(dialog('he', { initialIds: [] })).toContain(`נבחרו: 0 (עד ${RESOLVE_PARTS_MAX})`);
  });

  it('labels each checkbox with the part name', () => {
    const markup = dialog('en');
    expect(markup).toMatch(/<label class="checkbox-field"><input[^>]*id="r-part-p1"[^>]*\/><span><bdi>Washer<\/bdi><\/span><\/label>/);
  });

  it('disables the parts that are not chosen once 20 are', () => {
    const many = Array.from({ length: RESOLVE_PARTS_MAX + 1 }, (_, index) => part(`q${index}`, `חלק ${index}`, `Part ${index}`));
    const chosen = many.slice(0, RESOLVE_PARTS_MAX).map((item) => item.id);
    const markup = dialog('en', { view: { kind: 'ready', parts: many }, initialIds: chosen });
    expect(markup.match(/<input[^>]*type="checkbox"[^>]*disabled/g)).toHaveLength(1);
    expect(markup).toMatch(/<input[^>]*id="r-part-q20"[^>]*disabled/);
  });

  it('shows a loading text, a failure with a retry control, and an empty message', () => {
    expect(dialog('en', { view: { kind: 'loading' } })).toContain(locales.en.management.ticket.resolve.parts.loading);
    const failed = dialog('en', { view: { kind: 'error', code: 'insufficient_role' } });
    expect(failed).toContain('role="alert"');
    expect(failed).toContain(locales.en.management.ticket.resolve.parts.loadError);
    expect(failed).toContain(text.en.errors.insufficient_role);
    expect(failed).toContain(text.en.management.common.retry);
    expect(failed).not.toContain('type="checkbox"');
    const empty = dialog('he', { view: { kind: 'ready', parts: [] } });
    expect(empty).toContain(locales.he.management.ticket.resolve.parts.empty);
    expect(empty).not.toContain('type="checkbox"');
  });
});
