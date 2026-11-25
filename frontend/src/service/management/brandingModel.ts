// View model of the Branding screen (task 30.7; R28.1, R28.2, R23.12): the company name and the logo. Pure: no
// React, no DOM, no i18n instance, no network.
//
// The server stays the authority for every rule: the name range (1 to 100 characters after trimming), the file
// type, the pixel limits, the 1 MiB limit. What lives here is what one Save sends (only what changed), which field
// a refusal belongs to, the keys of the messages, and the hints the screen gives before a file is sent: the 1 MB
// size and the three accepted types. The hints never block a Save, because the server decides.
import { errorTextKey, type ErrorTextKey } from './configModel';
import type { BrandingChange, StaffBranding } from '../api/staffListsTypes';

export type { ErrorTextKey } from './configModel';

/** The `accept` attribute of the logo input (R28.2). */
export const LOGO_ACCEPT = 'image/jpeg,image/png,image/webp';

/** The three types the server accepts. */
export const LOGO_TYPES: readonly string[] = ['image/jpeg', 'image/png', 'image/webp'];

/** The size hint: 1 MiB, the limit of the server (R28.2). */
export const LOGO_MAX_BYTES = 1_048_576;

// --- the draft -----------------------------------------------------------------------------------------

/** What the form holds: the name as typed, the chosen file, and the two removals the user asked for. */
export interface BrandingDraft {
  /** The text of the name field. Empty after "Remove the name". */
  readonly name: string;
  /** "Remove the name" was pressed and the name is still stored. */
  readonly nameRemoved: boolean;
  /** The file chosen in the logo input. Null when none. */
  readonly logo: File | null;
  /** "Remove the logo" was pressed. */
  readonly logoRemoved: boolean;
}

/** The draft of the branding the server holds. */
export function draftFromStored(stored: StaffBranding): BrandingDraft {
  return { name: stored.company_name ?? '', nameRemoved: false, logo: null, logoRemoved: false };
}

/** The user typed in the name field: that is a new name, not a removal. */
export function draftWithName(draft: BrandingDraft, name: string): BrandingDraft {
  return { ...draft, name, nameRemoved: false };
}

/** "Remove the name": the field is emptied and the removal is sent on Save. */
export function draftWithNameRemoved(draft: BrandingDraft): BrandingDraft {
  return { ...draft, name: '', nameRemoved: true };
}

/** A file was chosen (or the choice was cleared with null). Choosing a file cancels a pending logo removal. */
export function draftWithLogo(draft: BrandingDraft, logo: File | null): BrandingDraft {
  return logo === null ? { ...draft, logo: null } : { ...draft, logo, logoRemoved: false };
}

/** "Remove the logo" and "Keep the current logo": a toggle. Asking for a removal drops a chosen file. */
export function draftWithLogoRemovalToggled(draft: BrandingDraft): BrandingDraft {
  return draft.logoRemoved
    ? { ...draft, logoRemoved: false }
    : { ...draft, logoRemoved: true, logo: null };
}

/** True when "Remove the name" does something: there is a stored name that stays, or text in the field. */
export function canRemoveName(draft: BrandingDraft, stored: StaffBranding): boolean {
  return draft.name !== '' || (stored.company_name !== null && !draft.nameRemoved);
}

/** True while the stored name is going to be removed by the next Save. */
export function nameWillBeRemoved(draft: BrandingDraft, stored: StaffBranding): boolean {
  return draft.nameRemoved && stored.company_name !== null;
}

/** True while the stored logo is going to be removed by the next Save. */
export function logoWillBeRemoved(draft: BrandingDraft, stored: StaffBranding): boolean {
  return draft.logoRemoved && stored.has_logo;
}

/**
 * What one Save sends, and only that (R28.1). A name that equals the stored one is not sent; a name that
 * differs is sent as typed (the server trims it and says if it is empty or too long); a removal is sent only
 * when there is something to remove; a chosen file is sent as the logo.
 */
export function brandingChange(draft: BrandingDraft, stored: StaffBranding): BrandingChange {
  const change: { -readonly [K in keyof BrandingChange]: BrandingChange[K] } = {};
  if (nameWillBeRemoved(draft, stored)) change.removeName = true;
  else if (!draft.nameRemoved && draft.name !== (stored.company_name ?? '')) change.companyName = draft.name;
  if (draft.logo !== null) change.logo = draft.logo;
  else if (logoWillBeRemoved(draft, stored)) change.removeLogo = true;
  return change;
}

/** True when a Save would send anything. */
export function hasBrandingChange(change: BrandingChange): boolean {
  return (
    change.companyName !== undefined ||
    change.logo !== undefined ||
    change.removeName === true ||
    change.removeLogo === true
  );
}

// --- the file hints ------------------------------------------------------------------------------------

/** What the browser can tell about a chosen file before it is sent. Advice only; the server decides. */
export type LogoWarning = 'too_large' | 'wrong_type';

export function logoWarnings(file: Pick<File, 'size' | 'type'> | null): readonly LogoWarning[] {
  if (file === null) return [];
  const warnings: LogoWarning[] = [];
  if (!LOGO_TYPES.includes(file.type)) warnings.push('wrong_type');
  if (file.size > LOGO_MAX_BYTES) warnings.push('too_large');
  return warnings;
}

/** True when the browser can show the file in an `<img>` as a preview of the logo. */
export function isPreviewable(file: Pick<File, 'type'> | null): boolean {
  return file !== null && LOGO_TYPES.includes(file.type);
}

/** A file size for the screen: `812 B`, `120.5 KB`, `1.2 MB`. The units are the same in both languages. */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

// --- errors --------------------------------------------------------------------------------------------

/** The two fields of the form, in the order the server reports failures in. */
export type BrandingFieldName = 'company_name' | 'logo';

export const BRANDING_FIELD_ORDER: readonly BrandingFieldName[] = ['company_name', 'logo'];

export interface BrandingFormErrors {
  /** The text key of each failing field. */
  readonly fields: Readonly<Partial<Record<BrandingFieldName, ErrorTextKey>>>;
  /** A refusal that belongs to no single field (the body, a server error, storage down). Null when none. */
  readonly form: ErrorTextKey | null;
}

export const NO_BRANDING_ERRORS: BrandingFormErrors = { fields: {}, form: null };

function isBrandingField(name: string): name is BrandingFieldName {
  return (BRANDING_FIELD_ORDER as readonly string[]).includes(name);
}

/** The codes of a refusal that belong to the logo field when the server names no field (a 413). */
const LOGO_CODES: readonly string[] = ['logo_too_large', 'request_too_large', 'invalid_logo_type', 'logo_dimensions_too_large'];

/** The text of a code on the logo field: a body over the whole-request limit is "the logo is too large" too. */
function logoTextKey(code: string): ErrorTextKey {
  return errorTextKey(code === 'request_too_large' ? 'logo_too_large' : code);
}

/**
 * The messages of a refused save. A 422 names every failing part (`fields`: `company_name`, `logo`, `body`) and
 * each becomes the message of that field; `body` ("nothing to change", a body that is not a form) is a message
 * for the whole form. A 413 (`logo_too_large`, or `request_too_large` for a body over the whole limit) names no
 * field and is shown on the logo field. Anything else (storage unavailable, a server error, the network) is a
 * message for the whole form.
 */
export function brandingFormErrors(code: string, fields: Readonly<Record<string, string>> = {}): BrandingFormErrors {
  const byField: Partial<Record<BrandingFieldName, ErrorTextKey>> = {};
  for (const [name, fieldCode] of Object.entries(fields)) {
    if (isBrandingField(name)) byField[name] = name === 'logo' ? logoTextKey(fieldCode) : errorTextKey(fieldCode);
  }
  const body = fields.body;
  const form = body === undefined ? null : errorTextKey(body);
  if (Object.keys(byField).length > 0 || form !== null) return { fields: byField, form };
  if (LOGO_CODES.includes(code)) return { fields: { logo: logoTextKey(code) }, form: null };
  if (code === 'invalid_company_name') return { fields: { company_name: errorTextKey(code) }, form: null };
  return { fields: {}, form: errorTextKey(code) };
}

/** The first failing field in form order, to take keyboard focus after a refusal (R23.12). */
export function firstFailingBrandingField(errors: BrandingFormErrors): BrandingFieldName | null {
  return BRANDING_FIELD_ORDER.find((name) => errors.fields[name] !== undefined) ?? null;
}

/** An edited field is no longer the refused value, so its message goes away. */
export function withoutBrandingError(errors: BrandingFormErrors, field: BrandingFieldName): BrandingFormErrors {
  if (errors.fields[field] === undefined) return errors;
  const rest = { ...errors.fields };
  delete rest[field];
  return { fields: rest, form: errors.form };
}
