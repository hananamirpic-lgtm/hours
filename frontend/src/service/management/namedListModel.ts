// View model of the settlement and replaceable-parts screens (task 30.7; R30.1, R29.6, R23.12). Pure: no React,
// no DOM, no i18n instance, no network.
//
// The two lists work as the issue categories do (`configModel.ts`), so this module follows that one and
// reuses its language helpers. What differs: the texts live under `management.settings.<list>.*`, the codes of a
// refusal are the list's own (`duplicate_settlement`, `invalid_part_name_he`, ...), and there is NO "last active"
// rule: any item, the last one included, can be deactivated or deleted (R30.1, R29.6).
//
// The server stays the authority for every rule: trimming, the length range (1 to 100 for a settlement, 1 to 60
// for a part), and the duplicate check (case-insensitive, active or inactive). Nothing here repeats them.
import type { NamedItemBody, NamedItemDeleteResult, NamedListKind, StaffNamedItem } from '../api/staffListsTypes';
import { errorTextKey, type ConfigLanguage, type ErrorTextKey } from './configModel';

export { configLanguage, errorTextKey, isolateName } from './configModel';
export type { ConfigLanguage, ErrorTextKey } from './configModel';

// --- the two lists -----------------------------------------------------------------------------------------

export interface NamedListSpec {
  readonly kind: NamedListKind;
  /** The prefix of every text key of the list: `management.settings.localities`. */
  readonly textPrefix: `management.settings.${NamedListKind}`;
  /** The prefix of the element ids of the list's screens. */
  readonly idPrefix: string;
  /** The code of a refusal that means the item no longer exists, so the list on screen is out of date. */
  readonly notFoundCode: string;
  /** The route of the screen. */
  readonly path: string;
}

export const NAMED_LISTS: Readonly<Record<NamedListKind, NamedListSpec>> = {
  localities: {
    kind: 'localities',
    textPrefix: 'management.settings.localities',
    idPrefix: 'config-localities',
    notFoundCode: 'settlement_not_found',
    path: '/tickets/settings/localities',
  },
  parts: {
    kind: 'parts',
    textPrefix: 'management.settings.parts',
    idPrefix: 'config-parts',
    notFoundCode: 'part_not_found',
    path: '/tickets/settings/parts',
  },
};

/** The text key `management.settings.<list>.<name>`. */
export function listText(kind: NamedListKind, name: string): string {
  return `${NAMED_LISTS[kind].textPrefix}.${name}`;
}

/** The name of an item in the language of the screen. */
export function namedItemDisplayName(
  item: Pick<StaffNamedItem, 'name_he' | 'name_en'>,
  language: ConfigLanguage,
): string {
  return language === 'he' ? item.name_he : item.name_en;
}

/** True when the refusal means the item no longer exists, so the list on screen is out of date. */
export function isNamedItemGone(kind: NamedListKind, code: string): boolean {
  return code === NAMED_LISTS[kind].notFoundCode;
}

// --- the form (R30.1, R29.6) -------------------------------------------------------------------------------

/** The two name fields of the form, in the order the server reports failures in. */
export type NameFieldName = 'name_he' | 'name_en';

export const NAME_FIELD_ORDER: readonly NameFieldName[] = ['name_he', 'name_en'];

export interface NamedFormValues {
  readonly nameHe: string;
  readonly nameEn: string;
}

export const EMPTY_NAMED_VALUES: NamedFormValues = { nameHe: '', nameEn: '' };

/** The values of the rename form. */
export function namedFormValues(item: Pick<StaffNamedItem, 'name_he' | 'name_en'>): NamedFormValues {
  return { nameHe: item.name_he, nameEn: item.name_en };
}

/** The body of a create or a rename. The names go as typed: the server trims them and checks the length. */
export function toNamedBody(values: NamedFormValues): NamedItemBody {
  return { name_he: values.nameHe, name_en: values.nameEn };
}

export interface NamedFormErrors {
  /** The text key of each failing name. */
  readonly fields: Readonly<Partial<Record<NameFieldName, ErrorTextKey>>>;
  /** A refusal that belongs to no single field (server error, unknown item). Null when there is none. */
  readonly form: ErrorTextKey | null;
}

export const NO_NAMED_ERRORS: NamedFormErrors = { fields: {}, form: null };

function isNameField(name: string): name is NameFieldName {
  return (NAME_FIELD_ORDER as readonly string[]).includes(name);
}

/**
 * The messages of a refused create or rename. A 422 names every failing text (`fields`: `name_he`, `name_en`),
 * and a 409 `duplicate_settlement` or `duplicate_part` names the clashing languages the same way; each becomes
 * the message of that field. A refusal that names no known field (a duplicate found by a concurrent writer, a
 * server error, an unknown item) is a message for the whole form.
 */
export function namedFormErrors(code: string, fields: Readonly<Record<string, string>> = {}): NamedFormErrors {
  const byField: Partial<Record<NameFieldName, ErrorTextKey>> = {};
  for (const [name, fieldCode] of Object.entries(fields)) {
    if (isNameField(name)) byField[name] = errorTextKey(fieldCode);
  }
  if (Object.keys(byField).length > 0) return { fields: byField, form: null };
  return { fields: {}, form: errorTextKey(code) };
}

/** The first failing name in form order, to take keyboard focus after a refusal (R23.12). */
export function firstFailingNameField(errors: NamedFormErrors): NameFieldName | null {
  return NAME_FIELD_ORDER.find((name) => errors.fields[name] !== undefined) ?? null;
}

/** An edited name is no longer the refused value, so its message goes away. */
export function withoutNamedError(errors: NamedFormErrors, field: NameFieldName): NamedFormErrors {
  if (errors.fields[field] === undefined) return errors;
  const rest = { ...errors.fields };
  delete rest[field];
  return { fields: rest, form: errors.form };
}

// --- the row actions (R30.1, R29.6) ------------------------------------------------------------------------

/** The actions a row offers besides rename. An active item is deactivated, an inactive one reactivated. */
export type NamedAction = 'deactivate' | 'reactivate' | 'delete';

export function availableNamedActions(item: Pick<StaffNamedItem, 'is_active'>): readonly NamedAction[] {
  return item.is_active ? ['deactivate', 'delete'] : ['reactivate', 'delete'];
}

export interface NamedActionTexts {
  /** The label of the row button. */
  readonly labelKey: string;
  /** The confirmation shown on the page after the action succeeded. Delete has two, see `namedDeleteResultKey`. */
  readonly doneKey: string | null;
}

export function namedActionTexts(kind: NamedListKind, action: NamedAction): NamedActionTexts {
  switch (action) {
    case 'deactivate':
      return { labelKey: listText(kind, 'deactivate'), doneKey: listText(kind, 'deactivateDone') };
    case 'reactivate':
      return { labelKey: listText(kind, 'reactivate'), doneKey: listText(kind, 'reactivateDone') };
    case 'delete':
      return { labelKey: listText(kind, 'delete'), doneKey: null };
  }
}

/** The message for the result of a delete: removed for good, or kept and deactivated because it is used. */
export function namedDeleteResultKey(kind: NamedListKind, result: NamedItemDeleteResult): string {
  return listText(kind, result === 'removed' ? 'removed' : 'deactivated');
}

/** The state of a row as a text key, so it is never shown by colour alone (R23.12). */
export function namedStateKey(kind: NamedListKind, item: Pick<StaffNamedItem, 'is_active'>): string {
  return listText(kind, item.is_active ? 'active' : 'inactive');
}
