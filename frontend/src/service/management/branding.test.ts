// Tests of the Branding screen (task 30.7; R28.1, R28.2, R23.12): the pure model (what one Save sends, the file
// hints, the error mapping), the object-URL lifecycle (revoked on change and on unmount), and server-rendered markup
// of the form and the page in both languages. No DOM, no testing library: the container that holds the queries and
// the mutation is rendered only up to its first paint.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { StaticRouter } from 'react-router-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ERROR_CODES } from '../api/errorCodes';
import { staffListKeys } from '../api/staffLists';
import type { StaffBranding } from '../api/staffListsTypes';
import { createPortalI18n, type PortalLanguage } from '../portal/portalI18n';
import {
  BRANDING_FIELD_ORDER,
  LOGO_ACCEPT,
  LOGO_MAX_BYTES,
  LOGO_TYPES,
  NO_BRANDING_ERRORS,
  brandingChange,
  brandingFormErrors,
  canRemoveName,
  draftFromStored,
  draftWithLogo,
  draftWithLogoRemovalToggled,
  draftWithName,
  draftWithNameRemoved,
  firstFailingBrandingField,
  formatFileSize,
  hasBrandingChange,
  isPreviewable,
  logoWarnings,
  logoWillBeRemoved,
  nameWillBeRemoved,
  withoutBrandingError,
  type BrandingDraft,
  type BrandingFormErrors,
} from './brandingModel';
import { BrandingFormView, brandingInputId } from './brandingView';
import { createManagedUrl, type ObjectUrlApi } from './objectUrl';
import { BrandingPage } from './pages/BrandingPage';
import en from '@/i18n/locales/en/service.json';
import he from '@/i18n/locales/he/service.json';

const locales = { en, he };

const noop = (): void => undefined;
const LANGUAGES: readonly PortalLanguage[] = ['en', 'he'];

const NAMED_AND_LOGO: StaffBranding = { company_name: 'Acme', has_logo: true };
const EMPTY: StaffBranding = { company_name: null, has_logo: false };

function file(name: string, type: string, size = 10): File {
  return new File([new Uint8Array(size)], name, { type });
}

// --- render helpers ----------------------------------------------------------------------------------

function render(language: PortalLanguage, element: ReactElement, prime?: (client: QueryClient) => void): string {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  prime?.(queryClient);
  const i18n = createPortalI18n(language);
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(
        I18nextProvider,
        { i18n },
        createElement(StaticRouter, { location: '/tickets/settings/branding' }, element),
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

function count(html: string, pattern: RegExp): number {
  return html.match(pattern)?.length ?? 0;
}

// --- the draft and what a Save sends ---------------------------------------------------------------------

describe('the draft (R28.1)', () => {
  it('starts from the stored branding, with nothing chosen or removed', () => {
    expect(draftFromStored(NAMED_AND_LOGO)).toEqual({ name: 'Acme', nameRemoved: false, logo: null, logoRemoved: false });
    expect(draftFromStored(EMPTY)).toEqual({ name: '', nameRemoved: false, logo: null, logoRemoved: false });
  });

  it('typing a name cancels a pending name removal, and Remove empties the field and asks for the removal', () => {
    const removed = draftWithNameRemoved(draftFromStored(NAMED_AND_LOGO));
    expect(removed).toMatchObject({ name: '', nameRemoved: true });
    expect(draftWithName(removed, 'Beta')).toMatchObject({ name: 'Beta', nameRemoved: false });
  });

  it('choosing a file cancels a pending logo removal, and asking for a removal drops the chosen file', () => {
    const png = file('a.png', 'image/png');
    const removed = draftWithLogoRemovalToggled(draftFromStored(NAMED_AND_LOGO));
    expect(removed.logoRemoved).toBe(true);
    const chosen = draftWithLogo(removed, png);
    expect(chosen).toMatchObject({ logo: png, logoRemoved: false });
    expect(draftWithLogoRemovalToggled(chosen)).toMatchObject({ logo: null, logoRemoved: true });
    // Pressing it again keeps the logo.
    expect(draftWithLogoRemovalToggled(removed).logoRemoved).toBe(false);
    expect(draftWithLogo(chosen, null).logo).toBeNull();
  });

  it('offers "Remove the name" only when there is a name to remove', () => {
    expect(canRemoveName(draftFromStored(NAMED_AND_LOGO), NAMED_AND_LOGO)).toBe(true);
    expect(canRemoveName(draftFromStored(EMPTY), EMPTY)).toBe(false);
    expect(canRemoveName(draftWithName(draftFromStored(EMPTY), 'typed'), EMPTY)).toBe(true);
    expect(canRemoveName(draftWithNameRemoved(draftFromStored(NAMED_AND_LOGO)), NAMED_AND_LOGO)).toBe(false);
  });

  it('reports a pending removal only when something stored would be removed', () => {
    expect(nameWillBeRemoved(draftWithNameRemoved(draftFromStored(NAMED_AND_LOGO)), NAMED_AND_LOGO)).toBe(true);
    expect(nameWillBeRemoved(draftWithNameRemoved(draftFromStored(EMPTY)), EMPTY)).toBe(false);
    expect(logoWillBeRemoved(draftWithLogoRemovalToggled(draftFromStored(NAMED_AND_LOGO)), NAMED_AND_LOGO)).toBe(true);
    expect(logoWillBeRemoved(draftWithLogoRemovalToggled(draftFromStored(EMPTY)), EMPTY)).toBe(false);
  });
});

describe('brandingChange: one Save sends only what changed (R28.1)', () => {
  const png = file('a.png', 'image/png');

  it('sends nothing for an untouched draft', () => {
    const change = brandingChange(draftFromStored(NAMED_AND_LOGO), NAMED_AND_LOGO);
    expect(change).toEqual({});
    expect(hasBrandingChange(change)).toBe(false);
    expect(hasBrandingChange(brandingChange(draftFromStored(EMPTY), EMPTY))).toBe(false);
  });

  it('sends a changed name as typed, so the server does the trimming and the length check', () => {
    expect(brandingChange(draftWithName(draftFromStored(NAMED_AND_LOGO), '  Beta '), NAMED_AND_LOGO)).toEqual({
      companyName: '  Beta ',
    });
    expect(brandingChange(draftWithName(draftFromStored(EMPTY), 'Acme'), EMPTY)).toEqual({ companyName: 'Acme' });
  });

  it('lets the server refuse a name that was emptied by hand, instead of treating it as a removal', () => {
    expect(brandingChange(draftWithName(draftFromStored(NAMED_AND_LOGO), ''), NAMED_AND_LOGO)).toEqual({ companyName: '' });
  });

  it('sends the removal of the name only when a name is stored, and never with a new name', () => {
    expect(brandingChange(draftWithNameRemoved(draftFromStored(NAMED_AND_LOGO)), NAMED_AND_LOGO)).toEqual({
      removeName: true,
    });
    expect(brandingChange(draftWithNameRemoved(draftFromStored(EMPTY)), EMPTY)).toEqual({});
    const retyped = draftWithName(draftWithNameRemoved(draftFromStored(NAMED_AND_LOGO)), 'Beta');
    expect(brandingChange(retyped, NAMED_AND_LOGO)).toEqual({ companyName: 'Beta' });
  });

  it('sends a chosen file as the logo and leaves the name alone', () => {
    const change = brandingChange(draftWithLogo(draftFromStored(NAMED_AND_LOGO), png), NAMED_AND_LOGO);
    expect(change).toEqual({ logo: png });
    expect(hasBrandingChange(change)).toBe(true);
  });

  it('sends the removal of the logo only when a logo is stored', () => {
    expect(brandingChange(draftWithLogoRemovalToggled(draftFromStored(NAMED_AND_LOGO)), NAMED_AND_LOGO)).toEqual({
      removeLogo: true,
    });
    expect(brandingChange(draftWithLogoRemovalToggled(draftFromStored(EMPTY)), EMPTY)).toEqual({});
  });

  it('sends the name and the logo together, and never a value with its own removal', () => {
    const draft: BrandingDraft = draftWithLogo(draftWithName(draftFromStored(EMPTY), 'Acme'), png);
    const change = brandingChange(draft, EMPTY);
    expect(change).toEqual({ companyName: 'Acme', logo: png });
    expect(change.removeName).toBeUndefined();
    expect(change.removeLogo).toBeUndefined();
    const both = brandingChange(
      draftWithLogoRemovalToggled(draftWithNameRemoved(draftFromStored(NAMED_AND_LOGO))),
      NAMED_AND_LOGO,
    );
    expect(both).toEqual({ removeName: true, removeLogo: true });
  });
});

// --- the hints --------------------------------------------------------------------------------------------

describe('the file hints (R28.2)', () => {
  it('accepts the three types and states the 1 MB size', () => {
    expect(LOGO_ACCEPT).toBe('image/jpeg,image/png,image/webp');
    expect(LOGO_TYPES).toEqual(['image/jpeg', 'image/png', 'image/webp']);
    expect(LOGO_MAX_BYTES).toBe(1_048_576);
  });

  it('warns about a file over 1 MB and about a type that is not one of the three, and about nothing else', () => {
    expect(logoWarnings(null)).toEqual([]);
    expect(logoWarnings(file('a.png', 'image/png', LOGO_MAX_BYTES))).toEqual([]);
    expect(logoWarnings({ size: LOGO_MAX_BYTES + 1, type: 'image/webp' })).toEqual(['too_large']);
    expect(logoWarnings(file('a.gif', 'image/gif'))).toEqual(['wrong_type']);
    expect(logoWarnings({ size: LOGO_MAX_BYTES + 1, type: '' })).toEqual(['wrong_type', 'too_large']);
  });

  it('previews only the types the browser shows as a logo', () => {
    expect(isPreviewable(file('a.jpg', 'image/jpeg'))).toBe(true);
    expect(isPreviewable(file('a.webp', 'image/webp'))).toBe(true);
    expect(isPreviewable(file('a.svg', 'image/svg+xml'))).toBe(false);
    expect(isPreviewable(file('a.pdf', 'application/pdf'))).toBe(false);
    expect(isPreviewable(null)).toBe(false);
  });

  it.each([
    [0, '0 B'],
    [812, '812 B'],
    [1024, '1.0 KB'],
    [123_456, '120.6 KB'],
    [1_048_576, '1.0 MB'],
    [2_621_440, '2.5 MB'],
  ])('formats %i bytes as %s', (bytes, text) => {
    expect(formatFileSize(bytes)).toBe(text);
  });
});

// --- the errors --------------------------------------------------------------------------------------------

describe('brandingFormErrors (R28.2)', () => {
  it('shows each failing field of a 422 on its own field, and nothing at form level', () => {
    const errors = brandingFormErrors('invalid_company_name', {
      company_name: 'invalid_company_name',
      logo: 'invalid_logo_type',
    });
    expect(errors).toEqual({
      fields: { company_name: 'errors.invalid_company_name', logo: 'errors.invalid_logo_type' },
      form: null,
    });
    expect(firstFailingBrandingField(errors)).toBe('company_name');
  });

  it('puts the logo codes on the logo field', () => {
    expect(brandingFormErrors('logo_dimensions_too_large', { logo: 'logo_dimensions_too_large' })).toEqual({
      fields: { logo: 'errors.logo_dimensions_too_large' },
      form: null,
    });
    expect(firstFailingBrandingField(brandingFormErrors('invalid_logo_type', { logo: 'invalid_logo_type' }))).toBe('logo');
  });

  it('shows the 413 on the logo field, whether the logo or the whole body was too large', () => {
    expect(brandingFormErrors('logo_too_large')).toEqual({ fields: { logo: 'errors.logo_too_large' }, form: null });
    expect(brandingFormErrors('request_too_large')).toEqual({ fields: { logo: 'errors.logo_too_large' }, form: null });
  });

  it('gives a code with no field to the name or the logo when it is theirs, and to the whole form otherwise', () => {
    expect(brandingFormErrors('invalid_company_name')).toEqual({
      fields: { company_name: 'errors.invalid_company_name' },
      form: null,
    });
    expect(brandingFormErrors('photo_storage_unavailable')).toEqual({
      fields: {},
      form: 'errors.photo_storage_unavailable',
    });
    expect(brandingFormErrors('network')).toEqual({ fields: {}, form: 'errors.generic' });
    expect(brandingFormErrors('generic', { other: 'invalid_logo_type' })).toEqual({ fields: {}, form: 'errors.generic' });
  });

  it('gives `body` to the whole form, next to any field it came with', () => {
    expect(brandingFormErrors('invalid_request', { body: 'invalid_request' })).toEqual({ fields: {}, form: 'errors.generic' });
    expect(brandingFormErrors('unknown_field', { logo: 'unknown_field', body: 'invalid_request' })).toEqual({
      fields: { logo: 'errors.unknown_field' },
      form: 'errors.generic',
    });
  });

  it('puts the name before the logo for focus, and clears a field when it is edited', () => {
    expect(BRANDING_FIELD_ORDER).toEqual(['company_name', 'logo']);
    expect(firstFailingBrandingField(NO_BRANDING_ERRORS)).toBeNull();
    const errors: BrandingFormErrors = brandingFormErrors('x', { logo: 'invalid_logo_type', company_name: 'invalid_company_name' });
    expect(firstFailingBrandingField(errors)).toBe('company_name');
    const after = withoutBrandingError(errors, 'company_name');
    expect(after.fields).toEqual({ logo: 'errors.invalid_logo_type' });
    expect(withoutBrandingError(after, 'company_name')).toBe(after);
  });
});

// --- the object URL --------------------------------------------------------------------------------------------

describe('createManagedUrl: the preview URL is revoked (R28.1)', () => {
  function fakeApi(): ObjectUrlApi & { created: Blob[]; revoked: string[] } {
    const created: Blob[] = [];
    const revoked: string[] = [];
    return {
      created,
      revoked,
      createObjectURL: (blob) => {
        created.push(blob);
        return `blob:test/${created.length}`;
      },
      revokeObjectURL: (url) => {
        revoked.push(url);
      },
    };
  }

  it('makes a URL for the blob and revokes exactly that URL, once', () => {
    const api = fakeApi();
    const blob = new Blob(['x']);
    const managed = createManagedUrl(blob, api);
    expect(managed?.url).toBe('blob:test/1');
    expect(api.created).toEqual([blob]);
    expect(api.revoked).toEqual([]);
    managed?.revoke();
    managed?.revoke();
    expect(api.revoked).toEqual(['blob:test/1']);
  });

  it('keeps the URLs of two blobs apart: replacing a blob revokes the old URL only', () => {
    const api = fakeApi();
    const first = createManagedUrl(new Blob(['a']), api);
    const second = createManagedUrl(new Blob(['b']), api);
    first?.revoke();
    expect(api.revoked).toEqual(['blob:test/1']);
    second?.revoke();
    expect(api.revoked).toEqual(['blob:test/1', 'blob:test/2']);
  });

  it('makes nothing for no blob or no URL API', () => {
    const api = fakeApi();
    const create = vi.spyOn(api, 'createObjectURL');
    expect(createManagedUrl(null, api)).toBeNull();
    expect(createManagedUrl(new Blob(['a']), null)).toBeNull();
    expect(create).not.toHaveBeenCalled();
  });

  it('wires the hook to it: the hook revokes in its cleanup, which runs on a change and on unmount', async () => {
    const { readFileSync } = await import('node:fs');
    const { fileURLToPath } = await import('node:url');
    const source = readFileSync(fileURLToPath(new URL('./objectUrl.ts', import.meta.url)), 'utf8');
    expect(source).toMatch(/useEffect\(\(\) => \{\s*const managed = createManagedUrl\(blob\);/);
    expect(source).toMatch(/return \(\) => \{\s*managed\?\.revoke\(\);\s*\};\s*\}, \[blob\]\);/);
  });
});

// --- markup: the form ------------------------------------------------------------------------------------------

describe('BrandingFormView', () => {
  const view = (
    stored: StaffBranding,
    draft: BrandingDraft,
    options: Partial<Parameters<typeof BrandingFormView>[0]> = {},
  ): ReactElement =>
    createElement(BrandingFormView, {
      stored,
      draft,
      errors: NO_BRANDING_ERRORS,
      pending: false,
      storedLogoUrl: null,
      storedLogoFailed: false,
      chosenLogoUrl: null,
      inputKey: 0,
      onNameChange: noop,
      onRemoveName: noop,
      onFileChange: noop,
      onClearFile: noop,
      onToggleLogoRemoval: noop,
      onSubmit: noop,
      ...options,
    });

  it.each(LANGUAGES)('renders a labelled name field and a labelled file input (%s)', (language) => {
    const html = render(language, view(EMPTY, draftFromStored(EMPTY)));
    expectNoRawKeys(html);
    for (const field of BRANDING_FIELD_ORDER) {
      expect(html).toContain(`id="${brandingInputId(field)}"`);
      expect(html).toContain(`for="${brandingInputId(field)}"`);
      expect(html).toContain(`id="${brandingInputId(field)}-hint"`);
    }
    expect(html).toContain('type="file"');
    expect(html).toContain('accept="image/jpeg,image/png,image/webp"');
    expect(html).not.toContain('aria-invalid="true"');
    expect(html).not.toContain('aria-required');
    expect(count(html, /<button/g)).toBe(2);
    expect(html).toContain('type="submit"');
  });

  it('states the 1 MB limit and the three types, and names the fields in English', () => {
    const html = render('en', view(EMPTY, draftFromStored(EMPTY)));
    expect(html).toContain('Company name');
    expect(html).toContain('A JPEG, PNG or WebP image of up to 1 MB.');
    expect(html).toContain('No logo is set.');
    expect(html).toContain('>Save<');
  });

  it('names the fields in Hebrew', () => {
    const html = render('he', view(EMPTY, draftFromStored(EMPTY)));
    expect(html).toContain('שם החברה');
    expect(html).toContain('לא הוגדר לוגו.');
    expect(html).toContain('>שמירה<');
  });

  it('shows the stored name in the field and enables Remove the name only while there is a name', () => {
    const named = render('en', view(NAMED_AND_LOGO, draftFromStored(NAMED_AND_LOGO)));
    expect(named).toContain('value="Acme"');
    const removeNamed = named.match(/<button[^>]*>Remove the name<\/button>/)?.[0] ?? '';
    expect(removeNamed).not.toContain('disabled');
    const empty = render('en', view(EMPTY, draftFromStored(EMPTY)));
    const removeEmpty = empty.match(/<button[^>]*>Remove the name<\/button>/)?.[0] ?? '';
    expect(removeEmpty).toContain('disabled=""');
  });

  it('says the name will be removed on save and ties the note to the field', () => {
    const draft = draftWithNameRemoved(draftFromStored(NAMED_AND_LOGO));
    const html = render('en', view(NAMED_AND_LOGO, draft));
    expect(html).toContain('The name will be removed when you save.');
    expect(html).toMatch(/aria-describedby="config-branding-name-hint config-branding-name-note"/);
    expect(html).toContain('value=""');
  });

  it('shows the stored logo as an image with a text alternative, and offers to remove it', () => {
    const html = render('en', view(NAMED_AND_LOGO, draftFromStored(NAMED_AND_LOGO), { storedLogoUrl: 'blob:stored' }));
    expect(html).toContain('src="blob:stored"');
    expect(html).toContain('alt="The current logo"');
    expect(html).toContain('>Remove the logo<');
    expect(html).not.toContain('Loading the logo');
  });

  it('says it is loading the stored logo, and says so when it could not', () => {
    const loading = render('en', view(NAMED_AND_LOGO, draftFromStored(NAMED_AND_LOGO)));
    expect(loading).toContain('Loading the logo…');
    expect(loading).not.toContain('<img');
    const failed = render('en', view(NAMED_AND_LOGO, draftFromStored(NAMED_AND_LOGO), { storedLogoFailed: true }));
    expect(failed).toContain('Could not load the current logo.');
    expect(failed).toContain('role="alert"');
  });

  it('previews the chosen file next to the stored logo, with its name and size, and offers to clear it', () => {
    const chosen = file('brand.png', 'image/png', 2048);
    const draft = draftWithLogo(draftFromStored(NAMED_AND_LOGO), chosen);
    const html = render(
      'en',
      view(NAMED_AND_LOGO, draft, { storedLogoUrl: 'blob:stored', chosenLogoUrl: 'blob:chosen' }),
    );
    expect(html).toContain('src="blob:chosen"');
    expect(html).toContain('alt="Preview of the chosen logo"');
    expect(html).toContain('src="blob:stored"');
    expect(html).toContain('brand.png (2.0 KB)');
    expect(html).toContain('>Clear the chosen file<');
    expect(count(html, /<img/g)).toBe(2);
  });

  it('warns, without an error, about a file that is too large or of the wrong type', () => {
    const big = draftWithLogo(draftFromStored(EMPTY), file('big.png', 'image/png', LOGO_MAX_BYTES + 1));
    const bigHtml = render('en', view(EMPTY, big, { chosenLogoUrl: 'blob:big' }));
    expect(bigHtml).toContain('This file is larger than 1 MB.');
    expect(bigHtml).toContain('id="config-branding-logo-warning"');
    expect(bigHtml).toMatch(/aria-describedby="config-branding-logo-hint config-branding-logo-warning"/);
    expect(bigHtml).not.toContain('aria-invalid="true"');
    const wrong = draftWithLogo(draftFromStored(EMPTY), file('doc.pdf', 'application/pdf'));
    const wrongHtml = render('he', view(EMPTY, wrong));
    expect(wrongHtml).toContain('הקובץ אינו תמונת JPEG, PNG או WebP.');
    expect(wrongHtml).not.toContain('<img');
    expectNoRawKeys(wrongHtml);
  });

  it('hides the stored logo and says it will be removed, and the button turns into "Keep"', () => {
    const draft = draftWithLogoRemovalToggled(draftFromStored(NAMED_AND_LOGO));
    const html = render('en', view(NAMED_AND_LOGO, draft, { storedLogoUrl: 'blob:stored' }));
    expect(html).toContain('The logo will be removed when you save.');
    expect(html).toContain('>Keep the current logo<');
    expect(html).not.toContain('>Remove the logo<');
    expect(html).not.toContain('src="blob:stored"');
    expect(html).toMatch(/aria-describedby="config-branding-logo-hint config-branding-logo-note"/);
  });

  it('offers no logo removal when none is stored', () => {
    const html = render('en', view(EMPTY, draftFromStored(EMPTY)));
    expect(html).not.toContain('Remove the logo');
    expect(html).not.toContain('Keep the current logo');
  });

  it('marks the name invalid and ties the message to it (invalid_company_name)', () => {
    const errors = brandingFormErrors('invalid_company_name', { company_name: 'invalid_company_name' });
    const html = render('en', view(NAMED_AND_LOGO, draftFromStored(NAMED_AND_LOGO), { errors }));
    expect(count(html, /aria-invalid="true"/g)).toBe(1);
    expect(html).toContain('The company name is not valid. Enter 1 to 100 characters.');
    expect(html).toContain('id="config-branding-name-error"');
    expect(html).toMatch(/aria-describedby="config-branding-name-hint config-branding-name-error"/);
  });

  it.each([
    ['invalid_logo_type', 'The file is not a valid logo image. Choose a JPEG, PNG or WebP image.'],
    ['logo_dimensions_too_large', 'The logo dimensions are too large. Choose a smaller image.'],
    ['logo_too_large', 'The logo is too large. Choose a file of up to 1 MB.'],
  ])('shows %s on the logo field', (code, text) => {
    const errors = brandingFormErrors(code, code === 'logo_too_large' ? {} : { logo: code });
    const html = render('en', view(EMPTY, draftFromStored(EMPTY), { errors }));
    expect(count(html, /aria-invalid="true"/g)).toBe(1);
    expect(html).toContain(text);
    expect(html).toContain('id="config-branding-logo-error"');
    expect(html).toMatch(/aria-describedby="config-branding-logo-hint config-branding-logo-error"/);
    expect(html.indexOf('id="config-branding-logo-error"')).toBeGreaterThan(html.indexOf('type="file"'));
  });

  it('shows a refusal of the whole form as an alert, and disables every control while saving', () => {
    const alert = render('en', view(EMPTY, draftFromStored(EMPTY), { errors: brandingFormErrors('photo_storage_unavailable') }));
    expect(alert).toContain('role="alert"');
    expect(alert).toContain('The photo could not be saved right now. Try again later.');
    const pending = render('he', view(NAMED_AND_LOGO, draftFromStored(NAMED_AND_LOGO), { pending: true }));
    // The name, the file, remove name, remove logo and save.
    expect(count(pending, /disabled=""/g)).toBe(5);
    expect(pending).toContain('>שומר…<');
  });
});

// --- markup: the page ------------------------------------------------------------------------------------------------

describe('BrandingPage', () => {
  const page = (): ReactElement => createElement(BrandingPage);

  it.each(LANGUAGES)('shows the heading and the form with the stored branding from the cache (%s)', (language) => {
    const html = render(language, page(), (client) => {
      client.setQueryData<StaffBranding>(staffListKeys.branding, NAMED_AND_LOGO);
    });
    expectNoRawKeys(html);
    expect(html.match(/<h1/g)).toHaveLength(1);
    expect(html).toContain(language === 'en' ? 'Branding' : 'לוגו ושם החברה');
    expect(html).toContain('<form');
    expect(html).toContain('value="Acme"');
    expect(html).toContain('role="status"');
  });

  it('says it is loading before the branding arrives, with no form', () => {
    const html = render('en', page());
    expect(html).toContain('Loading…');
    expect(html).not.toContain('<form');
  });
});

describe('texts', () => {
  it('has a text for every error code the screen can show, in both languages', () => {
    const codes = [
      'invalid_company_name',
      'invalid_logo_type',
      'logo_dimensions_too_large',
      'logo_too_large',
      'photo_storage_unavailable',
      'unknown_field',
      'insufficient_role',
    ];
    for (const code of codes) expect(ERROR_CODES).toContain(code);
    for (const language of LANGUAGES) {
      const i18n = createPortalI18n(language);
      for (const code of codes) expect(i18n.t(`errors.${code}`, { ns: 'service' }), `${language} ${code}`).not.toBe(`errors.${code}`);
    }
  });

  it('has every key the screen uses, in both languages', () => {
    for (const language of LANGUAGES) {
      const i18n = createPortalI18n(language);
      for (const name of Object.keys(locales[language].management.settings.branding)) {
        const key = `management.settings.branding.${name}`;
        expect(i18n.t(key, { ns: 'service' }), `${language} ${key}`).not.toBe(key);
      }
      for (const name of ['localities', 'parts', 'branding']) {
        const key = `management.nav.${name}`;
        expect(i18n.t(key, { ns: 'service' }), `${language} ${key}`).not.toBe(key);
      }
    }
    expect(Object.keys(locales.he.management.settings.branding).sort()).toEqual(
      Object.keys(locales.en.management.settings.branding).sort(),
    );
  });
});
