// View model of the three configuration screens (task 24.7; R19.1 to R19.13, R23.12): issue categories, field
// labels and the two form options. Pure: no React, no DOM, no i18n instance, no network. The screens turn the
// keys and values below into text.
//
// The server stays the authority for every rule: trimming, the 1 to 60 character range, the duplicate check
// (case-insensitive, active or inactive) and the "last active category" refusal (R19.5, R19.7, R19.13). Nothing
// here repeats them. What lives here is which field a refusal belongs to, which texts go in the request,
// and the keys of the messages.
import { isErrorCode, type ErrorCode } from '../api/errorCodes';
import { FORM_LABEL_KEYS } from '../api/formTypes';
import type {
  CategoryBody,
  CategoryDeleteResult,
  FormLabelKey,
  FormSettings,
  LabelTexts,
  StaffCategory,
} from '../api/staffTypes';

// --- shared ------------------------------------------------------------------------------------------

/** The two languages of every name and label. */
export type ConfigLanguage = 'he' | 'en';

export const CONFIG_LANGUAGES: readonly ConfigLanguage[] = ['he', 'en'];

/** The language of a screen from an i18next language tag (`he`, `he-IL`, `en`, ...). Anything else is English. */
export function configLanguage(tag: string | null | undefined): ConfigLanguage {
  return typeof tag === 'string' && tag.toLowerCase().startsWith('he') ? 'he' : 'en';
}

/** The name of a category in the language of the screen. */
export function categoryDisplayName(
  category: Pick<StaffCategory, 'name_he' | 'name_en'>,
  language: ConfigLanguage,
): string {
  return language === 'he' ? category.name_he : category.name_en;
}

/**
 * A name placed inside a sentence of the other direction: wrapped in first-strong isolates so a Latin name in
 * a Hebrew sentence (or the reverse) keeps its own order and does not reorder the words around it.
 */
export function isolateName(name: string): string {
  return `\u2068${name}\u2069`;
}

/** `errors.<code>` for a code the API defines, `errors.generic` for anything else. */
export type ErrorTextKey = `errors.${ErrorCode}` | 'errors.generic';

export function errorTextKey(code: string | null | undefined): ErrorTextKey {
  return isErrorCode(code) ? `errors.${code}` : 'errors.generic';
}

/** True when the refusal means the category no longer exists, so the list on screen is out of date. */
export function isCategoryGone(code: string): boolean {
  return code === 'category_not_found';
}

// --- categories (R19.5 to R19.13) ---------------------------------------------------------------------

/** The two name fields of the category form, in the order the server reports failures in. */
export type CategoryFieldName = 'name_he' | 'name_en';

export const CATEGORY_FIELD_ORDER: readonly CategoryFieldName[] = ['name_he', 'name_en'];

export interface CategoryFormValues {
  readonly nameHe: string;
  readonly nameEn: string;
}

export const EMPTY_CATEGORY_VALUES: CategoryFormValues = { nameHe: '', nameEn: '' };

/** The values of the rename form. */
export function categoryFormValues(category: Pick<StaffCategory, 'name_he' | 'name_en'>): CategoryFormValues {
  return { nameHe: category.name_he, nameEn: category.name_en };
}

/** The body of a create or a rename. The names go as typed: the server trims them and checks the length. */
export function toCategoryBody(values: CategoryFormValues): CategoryBody {
  return { name_he: values.nameHe, name_en: values.nameEn };
}

export interface CategoryFormErrors {
  /** The text key of each failing name. */
  readonly fields: Readonly<Partial<Record<CategoryFieldName, ErrorTextKey>>>;
  /** A refusal that belongs to no single field (server error, unknown category). Null when there is none. */
  readonly form: ErrorTextKey | null;
}

export const NO_CATEGORY_ERRORS: CategoryFormErrors = { fields: {}, form: null };

function isCategoryField(name: string): name is CategoryFieldName {
  return (CATEGORY_FIELD_ORDER as readonly string[]).includes(name);
}

/**
 * The messages of a refused create or rename. A 422 names every failing text (`fields`: `name_he`, `name_en`),
 * and a 409 `duplicate_category` names the clashing languages the same way (R19.5, R19.7); each becomes the
 * message of that field. A refusal that names no known field (a duplicate found by a concurrent writer, a
 * server error, an unknown category) is a message for the whole form.
 */
export function categoryFormErrors(code: string, fields: Readonly<Record<string, string>> = {}): CategoryFormErrors {
  const byField: Partial<Record<CategoryFieldName, ErrorTextKey>> = {};
  for (const [name, fieldCode] of Object.entries(fields)) {
    if (isCategoryField(name)) byField[name] = errorTextKey(fieldCode);
  }
  if (Object.keys(byField).length > 0) return { fields: byField, form: null };
  return { fields: {}, form: errorTextKey(code) };
}

/** The first failing name in form order, to take keyboard focus after a refusal (R23.12). */
export function firstFailingCategoryField(errors: CategoryFormErrors): CategoryFieldName | null {
  return CATEGORY_FIELD_ORDER.find((name) => errors.fields[name] !== undefined) ?? null;
}

/** The actions a category row offers besides rename. An active category is deactivated, an inactive one reactivated. */
export type CategoryAction = 'deactivate' | 'reactivate' | 'delete';

export function availableCategoryActions(category: Pick<StaffCategory, 'is_active'>): readonly CategoryAction[] {
  return category.is_active ? ['deactivate', 'delete'] : ['reactivate', 'delete'];
}

export interface CategoryActionTexts {
  /** The label of the row button. */
  readonly labelKey: string;
  /** The confirmation shown on the page after the action succeeded. Delete has two, see `deleteResultKey`. */
  readonly doneKey: string | null;
}

const CATEGORY_ACTION_TEXTS: Readonly<Record<CategoryAction, CategoryActionTexts>> = {
  deactivate: {
    labelKey: 'management.settings.categories.deactivate',
    doneKey: 'management.settings.categories.deactivateDone',
  },
  reactivate: {
    labelKey: 'management.settings.categories.reactivate',
    doneKey: 'management.settings.categories.reactivateDone',
  },
  delete: { labelKey: 'management.settings.categories.delete', doneKey: null },
};

export function categoryActionTexts(action: CategoryAction): CategoryActionTexts {
  return CATEGORY_ACTION_TEXTS[action];
}

/** The message for the result of a delete: removed for good, or kept and deactivated because tickets use it (R19.11, R19.12). */
export function deleteResultKey(
  result: CategoryDeleteResult,
): 'management.settings.categories.removed' | 'management.settings.categories.deactivated' {
  return result === 'removed'
    ? 'management.settings.categories.removed'
    : 'management.settings.categories.deactivated';
}

/** The state of a row as a text key, so it is never shown by colour alone (R23.12). */
export function categoryStateKey(
  category: Pick<StaffCategory, 'is_active'>,
): 'management.settings.categories.active' | 'management.settings.categories.inactive' {
  return category.is_active
    ? 'management.settings.categories.active'
    : 'management.settings.categories.inactive';
}

// --- field labels (R19.4, R19.5) ---------------------------------------------------------------------

/** The seven label fields in the order of the ticket form. */
export const LABEL_FIELDS: readonly FormLabelKey[] = FORM_LABEL_KEYS;

/** The text key of a field's name (`fields.city`, ...), the legend of its group. */
export function labelFieldNameKey(key: FormLabelKey): `fields.${FormLabelKey}` {
  return `fields.${key}`;
}

/** The texts being edited, one pair per field. */
export type LabelDrafts = Readonly<Record<FormLabelKey, LabelTexts>>;

/** The drafts of the labels the service returned. A field the service holds no label for starts empty. */
export function labelDrafts(labels: Readonly<Partial<Record<FormLabelKey, LabelTexts>>>): LabelDrafts {
  const drafts = {} as Record<FormLabelKey, LabelTexts>;
  for (const key of LABEL_FIELDS) {
    const stored = labels[key];
    drafts[key] = { he: stored?.he ?? '', en: stored?.en ?? '' };
  }
  return drafts;
}

export function withLabelText(
  drafts: LabelDrafts,
  key: FormLabelKey,
  language: ConfigLanguage,
  value: string,
): LabelDrafts {
  return { ...drafts, [key]: { ...drafts[key], [language]: value } };
}

/**
 * The labels whose Hebrew or English text differs from the stored one, as the body of `PUT /staff/labels`.
 * A field left alone is not sent, so it keeps its label; the texts go as typed (the server trims them).
 */
export function changedLabels(
  drafts: LabelDrafts,
  stored: LabelDrafts,
): Partial<Record<FormLabelKey, LabelTexts>> {
  const changed: Partial<Record<FormLabelKey, LabelTexts>> = {};
  for (const key of LABEL_FIELDS) {
    if (drafts[key].he !== stored[key].he || drafts[key].en !== stored[key].en) {
      changed[key] = { he: drafts[key].he, en: drafts[key].en };
    }
  }
  return changed;
}

/** One text of one field: `city.he`, `phone.en`. */
export type LabelErrorKey = `${FormLabelKey}.${ConfigLanguage}`;

export function labelErrorKey(key: FormLabelKey, language: ConfigLanguage): LabelErrorKey {
  return `${key}.${language}`;
}

export interface LabelFormErrors {
  /** The text key of each failing label text. */
  readonly fields: Readonly<Partial<Record<LabelErrorKey, ErrorTextKey>>>;
  readonly form: ErrorTextKey | null;
}

export const NO_LABEL_ERRORS: LabelFormErrors = { fields: {}, form: null };

/**
 * The field of a `fields` key of a refused `PUT /staff/labels`. The service names a failing text
 * `<field>.label_he` or `<field>.label_en`; `<field>.he` and `<field>.en` are read the same way. A key
 * with no language part (`unknown_field` names the bare field) or an unknown field gives null.
 */
export function parseLabelErrorKey(name: string): LabelErrorKey | null {
  const dot = name.lastIndexOf('.');
  if (dot <= 0) return null;
  const field = name.slice(0, dot);
  const language = name.slice(dot + 1).replace(/^label_/, '');
  if (!(FORM_LABEL_KEYS as readonly string[]).includes(field)) return null;
  if (language !== 'he' && language !== 'en') return null;
  return `${field as FormLabelKey}.${language}`;
}

/**
 * The messages of a refused save (R19.5). A 422 names every failing text, and each becomes the message of
 * that input; the save changed nothing in either language. A refusal naming no known text is a message for the
 * whole form.
 */
export function labelFormErrors(code: string, fields: Readonly<Record<string, string>> = {}): LabelFormErrors {
  const byField: Partial<Record<LabelErrorKey, ErrorTextKey>> = {};
  for (const [name, fieldCode] of Object.entries(fields)) {
    const parsed = parseLabelErrorKey(name);
    if (parsed !== null) byField[parsed] = errorTextKey(fieldCode);
  }
  if (Object.keys(byField).length > 0) return { fields: byField, form: null };
  return { fields: {}, form: errorTextKey(code) };
}

/** The first failing text in form order (Hebrew before English within a field), to take focus after a refusal. */
export function firstFailingLabel(errors: LabelFormErrors): { key: FormLabelKey; language: ConfigLanguage } | null {
  for (const key of LABEL_FIELDS) {
    for (const language of CONFIG_LANGUAGES) {
      if (errors.fields[labelErrorKey(key, language)] !== undefined) return { key, language };
    }
  }
  return null;
}

/** An edited text is no longer the refused value, so its message goes away. */
export function withoutLabelError(errors: LabelFormErrors, key: FormLabelKey, language: ConfigLanguage): LabelFormErrors {
  const at = labelErrorKey(key, language);
  if (errors.fields[at] === undefined) return errors;
  const rest = { ...errors.fields };
  delete rest[at];
  return { fields: rest, form: errors.form };
}

// --- the two options (R19.2, R19.3) ------------------------------------------------------------------

export type OptionName = keyof FormSettings;

/** The two options in the order they are shown. */
export const OPTION_NAMES: readonly OptionName[] = ['description_required', 'photo_upload_enabled'];

export function withOption(settings: FormSettings, name: OptionName, value: boolean): FormSettings {
  return { ...settings, [name]: value };
}
