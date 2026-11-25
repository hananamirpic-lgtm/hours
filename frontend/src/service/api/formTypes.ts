// Wire types of the ticket form (R9.1, R9.3, R9.5, R11.1; backend `FormConfig` in
// backend/app/service_tickets/api/schemas.py). Pure types and constants: no React, no DOM.

/** The seven field labels the Management_Interface can edit (R19). Keys of `labels` in `GET /form-config`. */
export const FORM_LABEL_KEYS = ['city', 'apartment', 'phone', 'category', 'description', 'photo', 'urgent'] as const;
export type FormLabelKey = (typeof FORM_LABEL_KEYS)[number];

/** One active Issue_Category as the form shows it. The server sends them oldest first. */
export interface FormCategory {
  readonly id: string;
  readonly name_he: string;
  readonly name_en: string;
}

/** The Hebrew and English text of one Field_Label. */
export interface LabelTexts {
  readonly he: string;
  readonly en: string;
}

/** The two form options of Ticket_Settings (R19.10). */
export interface FormSettings {
  readonly description_required: boolean;
  readonly photo_upload_enabled: boolean;
}

/** One active Settlement as the form shows it (R30.2). The server sends them oldest first. */
export interface FormLocality {
  readonly id: string;
  readonly name_he: string;
  readonly name_en: string;
}

/** The company name and whether a logo exists (R28.5). The logo itself comes from `GET /branding/logo`. */
export interface FormBranding {
  readonly company_name: string | null;
  readonly has_logo: boolean;
}

/**
 * `GET /service/form-config`. The settlement and branding fields are optional in the type so that an older
 * answer (and the staff form configuration of other screens) still fits; `normalizeFormConfig` fills them.
 */
export interface FormConfig {
  readonly categories: readonly FormCategory[];
  readonly labels: Readonly<Partial<Record<FormLabelKey, LabelTexts>>>;
  readonly settings: FormSettings;
  /** Active settlements, oldest first (R30.2). */
  readonly localities?: readonly FormLocality[];
  /** True while at least one active settlement exists: the City field is then a choice, not text (R30.2, R30.3). */
  readonly settlements_required?: boolean;
  readonly branding?: FormBranding;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** The settlements of an answer; an entry that is not `{id, name_he, name_en}` strings is left out. */
export function parseLocalities(raw: unknown): FormLocality[] {
  if (!Array.isArray(raw)) return [];
  const result: FormLocality[] = [];
  for (const entry of raw as unknown[]) {
    if (isRecord(entry) && typeof entry.id === 'string' && typeof entry.name_he === 'string' && typeof entry.name_en === 'string') {
      result.push({ id: entry.id, name_he: entry.name_he, name_en: entry.name_en });
    }
  }
  return result;
}

/** The branding of an answer: no name and no logo when the member is missing or unusable. */
export function parseBranding(raw: unknown): FormBranding {
  if (!isRecord(raw)) return { company_name: null, has_logo: false };
  const name = typeof raw.company_name === 'string' && raw.company_name.trim() !== '' ? raw.company_name : null;
  return { company_name: name, has_logo: raw.has_logo === true };
}

/**
 * The form configuration with the new members made certain: a missing `localities` is an empty list, a
 * missing `settlements_required` is false and a missing `branding` is no name and no logo. Other members
 * are passed through as they came. A value that is not an object is returned as it is.
 */
export function normalizeFormConfig(raw: FormConfig): FormConfig {
  if (!isRecord(raw)) return raw;
  const source = raw as unknown as Record<string, unknown>;
  return {
    ...raw,
    localities: parseLocalities(source.localities),
    settlements_required: source.settlements_required === true,
    branding: parseBranding(source.branding),
  };
}

/** `POST /service/tickets` answers 201 with only the number (R9.8). */
export interface TicketCreated {
  readonly ticket_number: number;
}
