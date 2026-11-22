// Tests of the ticket form and the confirmation (task 23.3): the pure model, the API module over a
// stubbed fetch, and server-rendered markup of `TicketForm`, the screen with a filled cache and the
// confirmation. No DOM and no testing library, so behavior that needs a browser (focus, object URLs)
// is covered by the model functions it calls and by the Playwright suite of task 28.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import enService from '../../../i18n/locales/en/service.json';
import heService from '../../../i18n/locales/he/service.json';
import type { FormCategory, FormConfig, FormSettings } from '../../api/formTypes';
import { PortalApiError } from '../../api/portalFetch';
import { createTicket, formConfigKey, getFormConfig, getMe } from '../../api/ticketCreate';
import { createPortalI18n, type PortalLanguage } from '../portalI18n';
import { portalKeys, type CustomerProfile } from '../session';
import { ConfirmationView } from './ConfirmationScreen';
import { TicketForm, TicketFormFrame, TicketFormScreen, type TicketFormProps } from './TicketFormScreen';
import keys233 from './keys-23-3.json';
import {
  FORM_FIELD_ORDER,
  MAX_PHOTO_BYTES,
  buildFormData,
  categoryOptions,
  clientPhotoError,
  confirmationState,
  describedBy,
  errorFieldOf,
  failureFromError,
  fieldOrder,
  focusTarget,
  initialValues,
  noticeTexts,
  photoTooLarge,
  resolveLabels,
  shownErrors,
  splitAroundNumber,
  ticketNumberFromState,
  withoutFieldError,
  type FormValues,
} from './ticketFormModel';

// --- helpers ---------------------------------------------------------------------------------------

const SETTINGS: FormSettings = { description_required: false, photo_upload_enabled: true };
const CATEGORIES: FormCategory[] = [
  { id: 'cat-1', name_he: 'אינסטלציה', name_en: 'Plumbing' },
  { id: 'cat-2', name_he: 'חשמל', name_en: 'Electrical' },
  { id: 'cat-3', name_he: '', name_en: 'Other' },
];
const ME: CustomerProfile = { full_name: 'Dana', phone: '0501234567', city: 'Haifa', apartment: null, must_change_password: false };
const EMPTY: FormValues = initialValues({ city: null, apartment: null, phone: '0501234567' });

function i18nFor(language: PortalLanguage) {
  const instance = createPortalI18n(language);
  for (const lng of ['he', 'en'] as const) instance.addResourceBundle(lng, 'service', keys233[lng], true, true);
  return instance;
}

function fallbacksOf(language: PortalLanguage) {
  const fields = (language === 'he' ? heService : enService).fields;
  return { ...fields };
}

function formProps(language: PortalLanguage, overrides: Partial<TicketFormProps> = {}): TicketFormProps {
  return {
    values: EMPTY,
    fieldErrors: {},
    notice: null,
    pending: false,
    categories: categoryOptions(CATEGORIES, language),
    labels: resolveLabels(undefined, language, fallbacksOf(language)),
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

function renderForm(language: PortalLanguage, overrides: Partial<TicketFormProps> = {}): string {
  return renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n: i18nFor(language) },
      createElement(
        StaticRouter,
        { location: '/new' },
        createElement(TicketFormFrame, {
          headingRef: null,
          children: createElement(TicketForm, formProps(language, overrides)),
        }),
      ),
    ),
  );
}

/** Text as it appears in server-rendered markup. */
function esc(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/** The opening tag of the element with this id. */
function tagWithId(html: string, id: string): string {
  const match = new RegExp(`<[a-z]+ [^>]*\\bid="${id}"[^>]*>`).exec(html);
  if (match === null) throw new Error(`no element with id ${id}`);
  return match[0];
}

function retryAfter(value: string | null): PortalApiError {
  return new PortalApiError(429, 'rate_limited', {}, value);
}

// --- model: labels, categories, prefill ------------------------------------------------------------

describe('resolveLabels', () => {
  const fallbacks = fallbacksOf('en');
  it('uses the configured label of the current language', () => {
    const labels = resolveLabels({ city: { he: 'יישוב', en: 'Town' } }, 'en', fallbacks);
    expect(labels.city).toBe('Town');
    expect(resolveLabels({ city: { he: 'יישוב', en: 'Town' } }, 'he', fallbacksOf('he')).city).toBe('יישוב');
  });
  it('falls back to the shipped text for a missing or blank label, in the same language', () => {
    const labels = resolveLabels({ city: { he: 'יישוב', en: '   ' } }, 'en', fallbacks);
    expect(labels.city).toBe(enService.fields.city);
    expect(labels.photo).toBe(enService.fields.photo);
    expect(resolveLabels(null, 'he', fallbacksOf('he')).urgent).toBe(heService.fields.urgent);
  });
  it('resolves all seven labels', () => {
    expect(Object.keys(resolveLabels(undefined, 'en', fallbacks)).sort()).toEqual(
      ['apartment', 'category', 'city', 'description', 'phone', 'photo', 'urgent'],
    );
  });
});

describe('categoryOptions', () => {
  it('keeps the order the server sent and names each in the current language', () => {
    expect(categoryOptions(CATEGORIES, 'en').map((c) => [c.id, c.name])).toEqual([
      ['cat-1', 'Plumbing'],
      ['cat-2', 'Electrical'],
      ['cat-3', 'Other'],
    ]);
    expect(categoryOptions(CATEGORIES, 'he').map((c) => c.name)).toEqual(['אינסטלציה', 'חשמל', 'Other']);
  });
  it('is empty for no categories', () => {
    expect(categoryOptions([], 'en')).toEqual([]);
  });
});

describe('initialValues', () => {
  it('prefills city, apartment and phone from the record and nothing else', () => {
    expect(initialValues({ city: 'Haifa', apartment: '12', phone: '0501234567' })).toEqual({
      city: 'Haifa',
      apartment: '12',
      phone: '0501234567',
      categoryId: '',
      description: '',
      urgent: false,
    });
  });
  it('leaves city and apartment empty when the record holds none', () => {
    const values = initialValues({ city: null, apartment: null, phone: '0501234567' });
    expect(values.city).toBe('');
    expect(values.apartment).toBe('');
    expect(values.phone).toBe('0501234567');
  });
});

// --- model: request body ---------------------------------------------------------------------------

describe('buildFormData', () => {
  const values: FormValues = {
    city: '  Haifa ',
    apartment: '12',
    phone: '050-123 4567',
    categoryId: 'cat-1',
    description: ' leak ',
    urgent: true,
  };
  it('sends every value as typed, urgent as the text true or false', () => {
    const body = buildFormData(values, null, SETTINGS);
    expect(body.get('city')).toBe('  Haifa ');
    expect(body.get('apartment')).toBe('12');
    expect(body.get('phone')).toBe('050-123 4567');
    expect(body.get('category_id')).toBe('cat-1');
    expect(body.get('description')).toBe(' leak ');
    expect(body.get('urgent')).toBe('true');
    expect(buildFormData({ ...values, urgent: false }, null, SETTINGS).get('urgent')).toBe('false');
  });
  it('omits the photo part entirely when no photo is chosen', () => {
    const body = buildFormData(values, null, SETTINGS);
    expect([...body.keys()].sort()).toEqual(['apartment', 'category_id', 'city', 'description', 'phone', 'urgent']);
    expect(body.has('photo')).toBe(false);
  });
  it('adds one photo part when a photo is chosen and uploads are on', () => {
    const file = new File([new Uint8Array([1, 2, 3])], 'leak.png', { type: 'image/png' });
    const body = buildFormData(values, file, SETTINGS);
    expect(body.getAll('photo')).toHaveLength(1);
    const part = body.get('photo');
    expect(part).toBeInstanceOf(File);
    expect((part as File).name).toBe('leak.png');
  });
  it('sends no photo while uploads are off, even if one was chosen earlier', () => {
    const file = new File(['x'], 'leak.png', { type: 'image/png' });
    expect(buildFormData(values, file, { ...SETTINGS, photo_upload_enabled: false }).has('photo')).toBe(false);
  });
  it('sends an empty category as an empty value, so the server answers invalid_category', () => {
    expect(buildFormData({ ...values, categoryId: '' }, null, SETTINGS).get('category_id')).toBe('');
  });
});

// --- model: photo size -----------------------------------------------------------------------------

describe('photo size check', () => {
  it('accepts exactly 10,485,760 bytes and rejects one more', () => {
    expect(MAX_PHOTO_BYTES).toBe(10_485_760);
    expect(photoTooLarge(10_485_760)).toBe(false);
    expect(photoTooLarge(10_485_761)).toBe(true);
    expect(photoTooLarge(0)).toBe(false);
  });
  it('shows photo_too_large at once but only for a file over the limit', () => {
    expect(clientPhotoError({ size: 10_485_761 })).toBe('photo_too_large');
    expect(clientPhotoError({ size: 10_485_760 })).toBeNull();
    expect(clientPhotoError(null)).toBeNull();
  });
  it('adds the hint to the shown errors without overriding the server, and only while uploads are on', () => {
    const big = { size: 10_485_761 };
    expect(shownErrors({}, big, true)).toEqual({ photo: 'photo_too_large' });
    expect(shownErrors({ photo: 'invalid_photo_type' }, big, true)).toEqual({ photo: 'invalid_photo_type' });
    expect(shownErrors({}, big, false)).toEqual({});
    expect(shownErrors({ city: 'invalid_city' }, null, true)).toEqual({ city: 'invalid_city' });
  });
});

// --- model: failed submissions ---------------------------------------------------------------------

describe('failureFromError', () => {
  it('maps a 422 to the fields the server named, with no notice', () => {
    const failure = failureFromError(new PortalApiError(422, 'invalid_city', { city: 'invalid_city', phone: 'invalid_phone' }));
    expect(failure).toEqual({ fieldErrors: { city: 'invalid_city', phone: 'invalid_phone' }, notice: null });
  });
  it('maps a 422 with no fields to a notice with its code', () => {
    expect(failureFromError(new PortalApiError(422, 'generic'))).toEqual({
      fieldErrors: {},
      notice: { kind: 'error', code: 'generic' },
    });
  });
  it('maps every 413 to the photo-too-large message under the photo control', () => {
    for (const code of ['photo_too_large', 'request_too_large', 'generic', 'something_new']) {
      expect(failureFromError(new PortalApiError(413, code))).toEqual({ fieldErrors: { photo: 'photo_too_large' }, notice: null });
    }
  });
  it('maps a 429 to the rate-limit notice with the wait time of Retry-After', () => {
    expect(failureFromError(retryAfter('30')).notice).toEqual({ kind: 'rate_limited', wait: { unit: 'seconds', value: 30 } });
    expect(failureFromError(retryAfter('60')).notice).toEqual({ kind: 'rate_limited', wait: { unit: 'seconds', value: 60 } });
    expect(failureFromError(retryAfter('61')).notice).toEqual({ kind: 'rate_limited', wait: { unit: 'minutes', value: 2 } });
    expect(failureFromError(retryAfter(null)).notice).toEqual({ kind: 'rate_limited', wait: { unit: 'none', value: 0 } });
    expect(failureFromError(retryAfter('soon')).notice).toEqual({ kind: 'rate_limited', wait: { unit: 'none', value: 0 } });
    expect(failureFromError(retryAfter('30')).fieldErrors).toEqual({});
  });
  it('maps ticket_numbers_exhausted to its own notice', () => {
    expect(failureFromError(new PortalApiError(503, 'ticket_numbers_exhausted')).notice).toEqual({ kind: 'numbers_exhausted' });
  });
  it('maps 5xx, network failures and the timeout to "check the ticket list"', () => {
    const cases = [
      new PortalApiError(500, 'generic'),
      new PortalApiError(502, 'generic'),
      new PortalApiError(503, 'service_unavailable'),
      new PortalApiError(503, 'photo_storage_unavailable'),
      new PortalApiError(0, 'network'),
      new PortalApiError(0, 'timeout'),
      new TypeError('boom'),
    ];
    for (const error of cases) {
      expect(failureFromError(error)).toEqual({ fieldErrors: {}, notice: { kind: 'submit_failed' } });
    }
  });
  it('maps any other 4xx to a notice with its code', () => {
    expect(failureFromError(new PortalApiError(403, 'password_change_required')).notice).toEqual({
      kind: 'error',
      code: 'password_change_required',
    });
  });
});

describe('noticeTexts', () => {
  it('lists the message and, for a rate limit, the wait time after it', () => {
    expect(noticeTexts({ kind: 'rate_limited', wait: { unit: 'seconds', value: 30 } })).toEqual([
      { key: 'errors.rate_limited' },
      { key: 'portal.waitTime.seconds', count: 30 },
    ]);
    expect(noticeTexts({ kind: 'rate_limited', wait: { unit: 'minutes', value: 2 } })[1]).toEqual({
      key: 'portal.waitTime.minutes',
      count: 2,
    });
    expect(noticeTexts({ kind: 'rate_limited', wait: { unit: 'none', value: 0 } })[1]).toEqual({ key: 'portal.waitTime.none' });
  });
  it('gives the other notices one text each', () => {
    expect(noticeTexts({ kind: 'submit_failed' })).toEqual([{ key: 'portal.form.submitFailed' }]);
    expect(noticeTexts({ kind: 'numbers_exhausted' })).toEqual([{ key: 'errors.ticket_numbers_exhausted' }]);
    expect(noticeTexts({ kind: 'error', code: 'nonsense' })).toEqual([{ key: 'errors.generic' }]);
  });
  it('has a text in both languages for every key it can return', () => {
    const lookup = (language: PortalLanguage, key: string): unknown => i18nFor(language).t(key, { count: 5 });
    const refs = [
      ...noticeTexts({ kind: 'rate_limited', wait: { unit: 'seconds', value: 30 } }),
      ...noticeTexts({ kind: 'rate_limited', wait: { unit: 'minutes', value: 2 } }),
      ...noticeTexts({ kind: 'rate_limited', wait: { unit: 'none', value: 0 } }),
      ...noticeTexts({ kind: 'submit_failed' }),
      ...noticeTexts({ kind: 'numbers_exhausted' }),
    ];
    for (const language of ['he', 'en'] as const) {
      for (const ref of refs) expect(lookup(language, ref.key)).not.toBe(ref.key);
    }
  });
});

// --- model: focus and field errors -----------------------------------------------------------------

describe('focusTarget', () => {
  it('is the first failing field in reading order, not the order the server listed them', () => {
    expect(focusTarget({ urgent: 'invalid_urgent', phone: 'invalid_phone', city: 'invalid_city' }, true)).toBe('city');
    expect(focusTarget({ description: 'invalid_description', category_id: 'invalid_category' }, true)).toBe('category_id');
    expect(focusTarget({ photo: 'invalid_photo_type', urgent: 'invalid_urgent' }, true)).toBe('photo');
  });
  it('follows the field order of the form', () => {
    expect(FORM_FIELD_ORDER).toEqual(['city', 'apartment', 'phone', 'category_id', 'description', 'photo', 'urgent']);
  });
  it('skips the photo while uploads are off and ignores fields the form does not have', () => {
    expect(fieldOrder(false)).not.toContain('photo');
    expect(focusTarget({ photo: 'photo_upload_disabled', urgent: 'invalid_urgent' }, false)).toBe('urgent');
    expect(focusTarget({ photo: 'photo_upload_disabled' }, false)).toBeNull();
    expect(focusTarget({ body: 'invalid_request' }, true)).toBeNull();
    expect(focusTarget({}, true)).toBeNull();
  });
});

describe('field error helpers', () => {
  it('names the category field category_id in an error map', () => {
    expect(errorFieldOf('categoryId')).toBe('category_id');
    expect(errorFieldOf('city')).toBe('city');
  });
  it('removes one field from an error map without touching the rest', () => {
    const errors = { city: 'invalid_city', phone: 'invalid_phone' };
    expect(withoutFieldError(errors, 'city')).toEqual({ phone: 'invalid_phone' });
    expect(withoutFieldError(errors, 'urgent')).toBe(errors);
    expect(errors).toEqual({ city: 'invalid_city', phone: 'invalid_phone' });
  });
  it('lists the error before the hint in aria-describedby', () => {
    expect(describedBy('photo', true, true)).toBe('ticket-form-photo-error ticket-form-photo-hint');
    expect(describedBy('photo', false, true)).toBe('ticket-form-photo-hint');
    expect(describedBy('city', true, false)).toBe('ticket-form-city-error');
    expect(describedBy('city', false, false)).toBeUndefined();
  });
});

// --- model: confirmation ---------------------------------------------------------------------------

describe('confirmation state', () => {
  it('round-trips the ticket number through the router state', () => {
    expect(ticketNumberFromState(confirmationState(10001))).toBe(10001);
  });
  it('gives null for a missing or unusable state', () => {
    for (const state of [null, undefined, 'x', 7, {}, { ticketNumber: '10001' }, { ticketNumber: 0 }, { ticketNumber: 1.5 }, { ticketNumber: NaN }]) {
      expect(ticketNumberFromState(state)).toBeNull();
    }
  });
  it('splits a sentence around the number', () => {
    expect(splitAroundNumber((slot) => `Ticket number ${slot} has been received.`)).toEqual({
      before: 'Ticket number ',
      after: ' has been received.',
    });
    expect(splitAroundNumber((slot) => `${slot} נקלטה`)).toEqual({ before: '', after: ' נקלטה' });
    expect(splitAroundNumber(() => 'no slot')).toEqual({ before: 'no slot', after: '' });
  });
});

// --- locale keys -----------------------------------------------------------------------------------

describe('keys-23-3.json', () => {
  function paths(value: unknown, prefix = ''): string[] {
    if (typeof value === 'string') return [prefix];
    return Object.entries(value as Record<string, unknown>).flatMap(([key, child]) => paths(child, prefix === '' ? key : `${prefix}.${key}`));
  }
  it('has the same keys in Hebrew and English, all under portal.', () => {
    expect(paths(keys233.he).sort()).toEqual(paths(keys233.en).sort());
    expect(paths(keys233.he).every((path) => path.startsWith('portal.'))).toBe(true);
  });
  it('covers every text the form and the confirmation use that is not in the shipped locale', () => {
    const merged = i18nFor('en');
    const used = [
      'portal.form.title', 'portal.form.intro', 'portal.form.requiredNote', 'portal.form.categoryPlaceholder', 'portal.form.photoHint',
      'portal.form.photoSelected', 'portal.form.photoRemove', 'portal.form.photoPreviewAlt', 'portal.form.urgentHelp',
      'portal.form.errorSummary', 'portal.form.submit', 'portal.form.submitting', 'portal.form.cancel', 'portal.form.submitFailed',
      'portal.form.loadError', 'portal.form.retry', 'portal.confirmation.title', 'portal.confirmation.message',
      'portal.confirmation.toList', 'portal.confirmation.neutralTitle', 'portal.confirmation.neutralMessage',
      'a11y.required', 'a11y.optional', 'a11y.errorPrefix', 'a11y.live.ticketSubmitted', 'portal.loading',
    ];
    for (const language of ['he', 'en'] as const) {
      const instance = i18nFor(language);
      for (const key of used) expect(instance.exists(key), `${language} ${key}`).toBe(true);
    }
    expect(merged.t('portal.form.loadError')).toBe(keys233.en.portal.form.loadError);
  });
});

// --- api module ------------------------------------------------------------------------------------

describe('ticket create API', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubFetch(response: Response): ReturnType<typeof vi.fn> {
    // A fresh copy per call: a body can be read once.
    const fn = vi.fn().mockImplementation(() => Promise.resolve(response.clone()));
    vi.stubGlobal('fetch', fn);
    return fn;
  }

  it('posts the multipart body without a content type and returns the ticket number', async () => {
    const fn = stubFetch(new Response(JSON.stringify({ ticket_number: 10007 }), { status: 201 }));
    const body = buildFormData(EMPTY, null, SETTINGS);
    await expect(createTicket(body)).resolves.toBe(10007);
    const [url, init] = fn.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/service/tickets');
    expect(init.method).toBe('POST');
    expect(init.body).toBe(body);
    expect(Object.keys(init.headers as Record<string, string>).map((h) => h.toLowerCase())).not.toContain('content-type');
  });

  it('rejects with the status, code, fields and Retry-After of an error answer', async () => {
    stubFetch(
      new Response(JSON.stringify({ detail: { error: { code: 'invalid_city', fields: { city: 'invalid_city' } } } }), {
        status: 422,
        headers: { 'Retry-After': '5' },
      }),
    );
    const error = await createTicket(new FormData()).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(PortalApiError);
    expect(failureFromError(error)).toEqual({ fieldErrors: { city: 'invalid_city' }, notice: null });
  });

  it('treats a created answer with no usable number as a lost answer', async () => {
    stubFetch(new Response('', { status: 201 }));
    const error = await createTicket(new FormData()).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(PortalApiError);
    expect(failureFromError(error).notice).toEqual({ kind: 'submit_failed' });
  });

  it('reads the form configuration and the record from their paths', async () => {
    const fn = stubFetch(new Response(JSON.stringify({}), { status: 200 }));
    await getFormConfig();
    await getMe();
    expect(fn.mock.calls.map((call) => call[0])).toEqual(['/api/service/form-config', '/api/service/auth/me']);
  });
});

// --- markup: the form ------------------------------------------------------------------------------

describe('TicketForm markup', () => {
  for (const language of ['he', 'en'] as const) {
    const texts = language === 'he' ? heService : enService;
    const dir = language === 'he' ? 'rtl' : 'ltr';

    describe(language, () => {
      it('sets lang and dir and shows the one h1', () => {
        const html = renderForm(language);
        expect(html).toContain(`<section class="portal-ticket-form" lang="${language}" dir="${dir}">`);
        expect(html).toContain(`lang="${language}" dir="${dir}" aria-busy="false"`);
        expect(html.match(/<h1[ >]/g)).toHaveLength(1);
        expect(html).toContain(`>${texts.portal.form.title}</h1>`);
      });

      it('shows every field label, linked to its input', () => {
        const html = renderForm(language);
        for (const [field, label] of [
          ['city', texts.fields.city],
          ['apartment', texts.fields.apartment],
          ['phone', texts.fields.phone],
          ['category_id', texts.fields.category],
          ['description', texts.fields.description],
          ['photo', texts.fields.photo],
          ['urgent', texts.fields.urgent],
        ] as const) {
          expect(html).toContain(`<label for="ticket-form-${field}">${label}`);
          expect(html).toMatch(new RegExp(`<(input|select|textarea) [^>]*id="ticket-form-${field}"`));
        }
      });

      it('marks required fields with aria-required and the visible text', () => {
        const html = renderForm(language);
        for (const field of ['city', 'apartment', 'phone', 'category_id']) {
          expect(tagWithId(html, `ticket-form-${field}`)).toContain('aria-required="true"');
          expect(html).toContain(`<label for="ticket-form-${field}">`);
        }
        expect(html.split(`<span class="portal-field-mark"> ${texts.a11y.required}</span>`)).toHaveLength(5);
        // Description is optional while the setting is off.
        expect(tagWithId(html, 'ticket-form-description')).toContain('aria-required="false"');
        expect(html).toContain(texts.a11y.optional);
      });

      it('marks the description required while the setting is on', () => {
        const html = renderForm(language, { settings: { ...SETTINGS, description_required: true } });
        expect(tagWithId(html, 'ticket-form-description')).toContain('aria-required="true"');
        expect(html.split(`<span class="portal-field-mark"> ${texts.a11y.required}</span>`)).toHaveLength(6);
      });

      it('has no category preselected and lists the categories in order in this language', () => {
        const html = renderForm(language);
        expect(html.match(/ selected=""/g)).toHaveLength(1);
        expect(html).toContain(`<option value="" selected="">${texts.portal.form.categoryPlaceholder}</option>`);
        const names = categoryOptions(CATEGORIES, language).map((c) => c.name);
        const positions = names.map((name) => html.indexOf(`>${name}</option>`));
        expect(positions.every((position) => position > 0)).toBe(true);
        expect([...positions].sort((a, b) => a - b)).toEqual(positions);
      });

      it('renders the fields in reading order', () => {
        const html = renderForm(language);
        const positions = FORM_FIELD_ORDER.map((field) => html.search(new RegExp(`<(input|select|textarea) [^>]*id="ticket-form-${field}"`)));
        expect(positions.every((position) => position > 0)).toBe(true);
        expect([...positions].sort((a, b) => a - b)).toEqual(positions);
      });

      it('shows the photo control only while uploads are enabled', () => {
        const on = renderForm(language);
        const tag = tagWithId(on, 'ticket-form-photo');
        expect(tag).toContain('type="file"');
        expect(tag).toContain('accept="image/jpeg,image/png,image/webp"');
        expect(tag).not.toContain('capture');
        expect(tag).toContain('aria-describedby="ticket-form-photo-hint"');
        const off = renderForm(language, { settings: { ...SETTINGS, photo_upload_enabled: false } });
        expect(off).not.toContain('type="file"');
        expect(off).not.toContain('ticket-form-photo');
        expect(off).not.toContain(texts.portal.form.photoHint);
      });

      it('shows the preview with the attach text as alt, the file name and a remove control', () => {
        const html = renderForm(language, { photo: { name: 'leak.png', previewUrl: 'blob:http://x/1' } });
        expect(html).toContain(`src="blob:http://x/1" alt="${texts.portal.form.photoPreviewAlt}"`);
        expect(html).toContain('leak.png');
        expect(html).toContain(`>${texts.portal.form.photoRemove}</button>`);
        const none = renderForm(language);
        expect(none).not.toContain('<img');
        expect(none).not.toContain(texts.portal.form.photoRemove);
      });

      it('leaves Urgent unchecked and prefills only the three values', () => {
        const html = renderForm(language, { values: initialValues({ city: 'Haifa', apartment: null, phone: '0501234567' }) });
        expect(tagWithId(html, 'ticket-form-urgent')).toContain('type="checkbox"');
        expect(tagWithId(html, 'ticket-form-urgent')).not.toContain('checked');
        expect(tagWithId(html, 'ticket-form-city')).toContain('value="Haifa"');
        expect(tagWithId(html, 'ticket-form-apartment')).toContain('value=""');
        expect(tagWithId(html, 'ticket-form-phone')).toContain('value="0501234567"');
        expect(tagWithId(html, 'ticket-form-phone')).toContain('type="tel"');
        expect(tagWithId(html, 'ticket-form-phone')).toContain('dir="ltr"');
        expect(html).toMatch(/<textarea [^>]*>\s*<\/textarea>/);
      });

      it('checks Urgent when it is set and keeps typed values', () => {
        const values: FormValues = { city: 'X', apartment: '4', phone: '0529999999', categoryId: 'cat-2', description: 'drip', urgent: true };
        const html = renderForm(language, { values });
        expect(tagWithId(html, 'ticket-form-urgent')).toContain('checked');
        expect(html).toContain('<option value="cat-2" selected="">');
        expect(html).toContain('>drip</textarea>');
      });

      it('shows no error markup and no summary when there are no errors', () => {
        const html = renderForm(language);
        expect(html).not.toContain('aria-invalid');
        expect(html).not.toContain('portal-error');
        expect(html).not.toContain('ticket-form-error-summary');
        expect(html).not.toContain('ticket-form-notice');
      });

      it('links each failing field to its message and lists them in a summary', () => {
        const html = renderForm(language, {
          fieldErrors: { phone: 'invalid_phone', city: 'invalid_city', photo: 'photo_too_large' },
        });
        for (const [field, code] of [
          ['city', 'invalid_city'],
          ['phone', 'invalid_phone'],
          ['photo', 'photo_too_large'],
        ] as const) {
          const tag = tagWithId(html, `ticket-form-${field}`);
          expect(tag).toContain('aria-invalid="true"');
          expect(tag).toContain(`aria-describedby="ticket-form-${field}-error`);
          const message = texts.errors[code];
          expect(html).toContain(`<p id="ticket-form-${field}-error" class="portal-error">${texts.a11y.errorPrefix} ${message}</p>`);
        }
        expect(tagWithId(html, 'ticket-form-photo')).toContain('aria-describedby="ticket-form-photo-error ticket-form-photo-hint"');
        // Fields without an error are not marked.
        expect(tagWithId(html, 'ticket-form-apartment')).not.toContain('aria-invalid');
        expect(tagWithId(html, 'ticket-form-apartment')).not.toContain('aria-describedby');
        // The summary lists them in reading order, each linking to its field.
        expect(html).toContain(texts.portal.form.errorSummary);
        const summary = html.slice(html.indexOf('ticket-form-error-summary'));
        const order = ['#ticket-form-city', '#ticket-form-phone', '#ticket-form-photo'].map((href) => summary.indexOf(`href="${href}"`));
        expect(order.every((position) => position > 0)).toBe(true);
        expect([...order].sort((a, b) => a - b)).toEqual(order);
      });

      it('shows an error for the category and the urgent checkbox under the control', () => {
        const html = renderForm(language, { fieldErrors: { category_id: 'invalid_category', urgent: 'invalid_urgent' } });
        expect(tagWithId(html, 'ticket-form-category_id')).toContain('aria-describedby="ticket-form-category_id-error"');
        expect(tagWithId(html, 'ticket-form-urgent')).toContain('aria-describedby="ticket-form-urgent-error ticket-form-urgent-hint"');
        expect(html).toContain(texts.errors.invalid_category);
        expect(html).toContain(esc(texts.errors.invalid_urgent));
      });

      it('lists a failing field the form does not have in the summary without a link', () => {
        const html = renderForm(language, { fieldErrors: { unknown_thing: 'unknown_field' } });
        expect(html).toContain(`<li>${texts.errors.unknown_field}</li>`);
        expect(html).not.toContain('href="#ticket-form-unknown_thing"');
      });

      it('disables the submit control only while pending, and says it is sending', () => {
        const idle = renderForm(language);
        const idleTag = /<button type="submit"[^>]*>/.exec(idle)?.[0] ?? '';
        expect(idleTag).not.toContain('disabled');
        expect(idle).toContain(`>${texts.portal.form.submit}</button>`);
        const busy = renderForm(language, { pending: true });
        const busyTag = /<button type="submit"[^>]*>/.exec(busy)?.[0] ?? '';
        expect(busyTag).toContain('disabled');
        expect(busy).toContain(`>${texts.portal.form.submitting}</button>`);
        expect(busy).toContain('aria-busy="true"');
      });

      it('offers a cancel link to the list', () => {
        const html = renderForm(language);
        expect(html).toContain(`href="/"`);
        expect(html).toContain(texts.portal.form.cancel);
      });

      it('shows the check-the-list message after a failure that may have created the ticket', () => {
        const html = renderForm(language, { notice: { kind: 'submit_failed' } });
        expect(html).toContain('id="ticket-form-notice"');
        expect(html).toContain(texts.portal.form.submitFailed);
      });

      it('shows the rate-limit message with the wait time', () => {
        const instance = i18nFor(language);
        const html = renderForm(language, { notice: { kind: 'rate_limited', wait: { unit: 'seconds', value: 30 } } });
        expect(html).toContain(texts.errors.rate_limited);
        expect(html).toContain(String(instance.t('portal.waitTime.seconds', { count: 30 })));
        const minutes = renderForm(language, { notice: { kind: 'rate_limited', wait: { unit: 'minutes', value: 2 } } });
        expect(minutes).toContain(String(instance.t('portal.waitTime.minutes', { count: 2 })));
        const none = renderForm(language, { notice: { kind: 'rate_limited', wait: { unit: 'none', value: 0 } } });
        expect(none).toContain(texts.portal.waitTime.none);
      });

      it('shows the numbers-exhausted message and keeps the typed values', () => {
        const values: FormValues = { ...EMPTY, city: 'Haifa', description: 'drip' };
        const html = renderForm(language, { notice: { kind: 'numbers_exhausted' }, values });
        expect(html).toContain(texts.errors.ticket_numbers_exhausted);
        expect(html).toContain('value="Haifa"');
        expect(html).toContain('>drip</textarea>');
      });
    });
  }

  it('uses the configured labels, falling back to the shipped text', () => {
    const labels = resolveLabels({ city: { he: 'יישוב', en: 'Town' } }, 'en', fallbacksOf('en'));
    const html = renderForm('en', { labels });
    expect(html).toContain('<label for="ticket-form-city">Town');
    expect(html).toContain(`<label for="ticket-form-apartment">${enService.fields.apartment}`);
  });

  it('escapes markup typed into a value instead of rendering it', () => {
    const html = renderForm('en', { values: { ...EMPTY, city: '<script>x</script>', description: '<b>x</b>' } });
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('<b>x</b>');
    expect(html).toContain('&lt;b&gt;x&lt;/b&gt;');
  });
});

// --- markup: the screen with a filled cache --------------------------------------------------------

describe('TicketFormScreen markup', () => {
  const config: FormConfig = {
    categories: CATEGORIES,
    labels: { city: { he: 'יישוב', en: 'Town' } },
    settings: { description_required: true, photo_upload_enabled: false },
  };

  function renderScreen(language: PortalLanguage, data: { config?: FormConfig; me?: CustomerProfile }): string {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    if (data.config) client.setQueryData(formConfigKey, data.config);
    if (data.me) client.setQueryData(portalKeys.me, data.me);
    return renderToStaticMarkup(
      createElement(
        I18nextProvider,
        { i18n: i18nFor(language) },
        createElement(
          QueryClientProvider,
          { client },
          createElement(StaticRouter, { location: '/new' }, createElement(TicketFormScreen)),
        ),
      ),
    );
  }

  it('prefills from the record, applies the configuration and preselects nothing', () => {
    const html = renderScreen('en', { config, me: ME });
    expect(html).toContain('<label for="ticket-form-city">Town');
    expect(tagWithId(html, 'ticket-form-city')).toContain('value="Haifa"');
    expect(tagWithId(html, 'ticket-form-apartment')).toContain('value=""');
    expect(tagWithId(html, 'ticket-form-phone')).toContain('value="0501234567"');
    expect(tagWithId(html, 'ticket-form-description')).toContain('aria-required="true"');
    expect(html).not.toContain('type="file"');
    expect(html).toContain('<option value="" selected="">');
    expect(html.match(/ selected=""/g)).toHaveLength(1);
    expect(tagWithId(html, 'ticket-form-urgent')).not.toContain('checked');
  });

  it('shows the heading and a loading text while the data is not there', () => {
    const html = renderScreen('he', {});
    expect(html).toContain(`>${heService.portal.form.title}</h1>`);
    expect(html).toContain(heService.portal.loading);
    expect(html).not.toContain('<form');
  });

  it('shows only the heading and the loading text when the record is missing', () => {
    const html = renderScreen('en', { config });
    expect(html).toContain(enService.portal.loading);
    expect(html).not.toContain('<form');
  });
});

// --- markup: the confirmation ----------------------------------------------------------------------

describe('ConfirmationView markup', () => {
  function renderConfirmation(language: PortalLanguage, ticketNumber: number | null): string {
    return renderToStaticMarkup(
      createElement(
        I18nextProvider,
        { i18n: i18nFor(language) },
        createElement(StaticRouter, { location: '/new/done' }, createElement(ConfirmationView, { ticketNumber, headingRef: null })),
      ),
    );
  }

  for (const language of ['he', 'en'] as const) {
    const texts = language === 'he' ? heService : enService;
    const dir = language === 'he' ? 'rtl' : 'ltr';

    it(`${language}: shows the number in a left-to-right bdi and a link to the list`, () => {
      const html = renderConfirmation(language, 10042);
      expect(html).toContain(`lang="${language}" dir="${dir}"`);
      expect(html).toContain(`>${texts.portal.confirmation.title}</h1>`);
      expect(html).toContain('<bdi dir="ltr">10042</bdi>');
      expect(html).toContain(`<a class="portal-button" href="/">${texts.portal.confirmation.toList}</a>`);
      const sentence = splitAroundNumber((slot) => texts.portal.confirmation.message.replace('{{number}}', slot));
      expect(html).toContain(`${sentence.before}<bdi dir="ltr">10042</bdi>${sentence.after}`);
      expect(html.match(/<h1[ >]/g)).toHaveLength(1);
    });

    it(`${language}: shows a neutral page with the list link when there is no ticket number`, () => {
      const html = renderConfirmation(language, null);
      expect(html).not.toContain('<bdi');
      expect(html).not.toContain(texts.portal.confirmation.title);
      expect(html).toContain(i18nFor(language).t('portal.confirmation.neutralTitle'));
      expect(html).toContain(i18nFor(language).t('portal.confirmation.neutralMessage'));
      expect(html).toContain(`href="/"`);
    });
  }
});
