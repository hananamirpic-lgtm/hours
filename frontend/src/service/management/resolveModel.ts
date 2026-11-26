// The pure model of the Resolve flow of the management interface (task 30.9; R29.4, R29.5, R29.7, R23.12).
// No React, no DOM, no i18n instance: components translate the keys and codes this module returns, so the
// decisions are testable without a renderer.
import type { ActivePart } from '../api/staffParts';
import type { ResolutionBody, StaffReplacedPart, StatusChangeBody, TicketStatus } from '../api/staffTypes';
import { isStaffApiError } from '../api/staff';
import { codeOf, type DetailLanguage } from './ticketDetailModel';

// --- the limits (R29.1, R29.3) ---------------------------------------------------------------------

export const RESOLVE_TEXT_MAX = 2000;
export const RESOLVE_TECHNICIAN_MAX = 100;
export const RESOLVE_PARTS_MAX = 20;

// --- the form --------------------------------------------------------------------------------------

/** The four fields of the resolution, which are also the keys of a 422 `fields` map. In reading order. */
export type ResolveField = 'entry_text' | 'technician_name' | 'parts_replaced' | 'part_ids';
export const RESOLVE_FIELDS: readonly ResolveField[] = ['entry_text', 'technician_name', 'parts_replaced', 'part_ids'];

export interface ResolveValues {
  readonly entry_text: string;
  readonly technician_name: string;
  readonly parts_replaced: boolean;
  readonly part_ids: readonly string[];
}

/**
 * The values of a new Resolve form (R29.4): empty history, the technician name prefilled with the ticket's
 * assigned technician, else the acting username, no part. Blank names do not count as a name.
 */
export function resolveInitialValues(
  assignedTechnicianName: string | null | undefined,
  username: string | null | undefined,
): ResolveValues {
  const assigned = (assignedTechnicianName ?? '').trim();
  const technician = assigned !== '' ? assigned : (username ?? '').trim();
  return { entry_text: '', technician_name: technician, parts_replaced: false, part_ids: [] };
}

/** The code per failing field. Empty when the values can be sent. Same codes and limits as the server (R29.1, R29.3). */
export type ResolveFieldCodes = Readonly<Partial<Record<ResolveField, string>>>;

export function validateResolve(values: ResolveValues): ResolveFieldCodes {
  const codes: Partial<Record<ResolveField, string>> = {};
  const text = values.entry_text.trim();
  if (text === '' || text.length > RESOLVE_TEXT_MAX) codes.entry_text = 'invalid_history_text';
  const name = values.technician_name.trim();
  if (name === '' || name.length > RESOLVE_TECHNICIAN_MAX) codes.technician_name = 'invalid_technician_name';
  if (values.parts_replaced) {
    const unique = new Set(values.part_ids);
    if (unique.size === 0) codes.part_ids = 'part_required';
    else if (unique.size > RESOLVE_PARTS_MAX) codes.part_ids = 'invalid_part';
  }
  return codes;
}

/**
 * The `resolution` of the status change: trimmed texts, and the part list only when the flag is set, so an
 * unticked box never sends a part (R29.3). Repeated ids are sent once.
 */
export function resolutionBody(values: ResolveValues): ResolutionBody {
  return {
    entry_text: values.entry_text.trim(),
    technician_name: values.technician_name.trim(),
    parts_replaced: values.parts_replaced,
    part_ids: values.parts_replaced ? Array.from(new Set(values.part_ids)) : [],
  };
}

/** The body of the status change to Resolved. `expected_status` is the status the page displays (R14.8). */
export function resolveStatusBody(from: TicketStatus, values: ResolveValues): StatusChangeBody {
  return { expected_status: from, target_status: 'closed', resolution: resolutionBody(values) };
}

/** Ticking or unticking "Part replaced". Unticking clears the chosen parts (R29.4). */
export function withPartsReplaced(values: ResolveValues, checked: boolean): ResolveValues {
  return { ...values, parts_replaced: checked, part_ids: checked ? values.part_ids : [] };
}

// --- failures --------------------------------------------------------------------------------------

export interface ResolveFailure {
  /** The error code per failing field of the form. */
  readonly fields: ResolveFieldCodes;
  /**
   * The code to show above the form when no field failed: a failure of the status change itself
   * (`ticket_status_changed`, `invalid_transition`, `resolution_required`, ...), a missing ticket, a server or
   * network error. Null when `fields` has entries.
   */
  readonly code: string | null;
}

/** A failure found by `validateResolve` before any request. */
export function localFailure(fields: ResolveFieldCodes): ResolveFailure {
  return { fields, code: null };
}

/**
 * Splits a failed resolve into per-field codes and a form-level code. Only the four form fields are read from
 * the `fields` map of a 422; any other failure is a form-level code.
 */
export function resolveFailureOf(error: unknown): ResolveFailure {
  const fields: Partial<Record<ResolveField, string>> = {};
  if (isStaffApiError(error)) {
    for (const name of RESOLVE_FIELDS) {
      const code = Object.prototype.hasOwnProperty.call(error.fields, name) ? error.fields[name] : undefined;
      if (code !== undefined) fields[name] = code;
    }
  }
  return Object.keys(fields).length > 0 ? { fields, code: null } : { fields, code: codeOf(error) };
}

/** True when the page should reload the ticket after this failure: the status change itself failed (R15.10). */
export function reloadsTicket(failure: ResolveFailure): boolean {
  return failure.code !== null;
}

/** The first failing field in reading order, or null. It takes the focus after a failure (R23.12). */
export function firstFailingField(fields: ResolveFieldCodes): ResolveField | null {
  return RESOLVE_FIELDS.find((field) => fields[field] !== undefined) ?? null;
}

/** The id of the control that takes focus for a field. The part list has no input of its own: its button does. */
export function resolveControlId(idPrefix: string, field: ResolveField): string {
  return `${idPrefix}-${field}`;
}

// --- the parts -------------------------------------------------------------------------------------

/** A part name in the current language; the other language when that name is blank. */
export function partName(part: Pick<ActivePart, 'name_he' | 'name_en'>, language: DetailLanguage): string {
  const own = (language === 'he' ? part.name_he : part.name_en).trim();
  if (own !== '') return own;
  return (language === 'he' ? part.name_en : part.name_he).trim();
}

/** Adds `id` when absent and removes it when present. A new list; the input is not changed. */
export function togglePart(ids: readonly string[], id: string): string[] {
  return ids.includes(id) ? ids.filter((item) => item !== id) : [...ids, id];
}

/** True when `id` can still be added: it is chosen already, or fewer than the maximum are chosen. */
export function canChoosePart(ids: readonly string[], id: string): boolean {
  return ids.includes(id) || ids.length < RESOLVE_PARTS_MAX;
}

/** The ids of `chosen` that are in `parts`, in the order of `parts` (the order the dialog lists them in). */
export function orderedChosenIds(parts: readonly ActivePart[], chosen: readonly string[]): string[] {
  return parts.filter((part) => chosen.includes(part.id)).map((part) => part.id);
}

/**
 * The chosen parts as text, comma separated, in the order of the list (R29.4). An id the list does not hold
 * (a part deactivated meanwhile) has no name to show and is left out; the server still validates it.
 */
export function chosenPartsText(parts: readonly ActivePart[], chosen: readonly string[], language: DetailLanguage): string {
  return parts
    .filter((part) => chosen.includes(part.id))
    .map((part) => partName(part, language))
    .join(', ');
}

/** The replaced parts of a ticket as names in the current language, in the order given; empty when there are none (R29.7). */
export function replacedPartNames(parts: readonly StaffReplacedPart[] | null | undefined, language: DetailLanguage): string[] {
  return (parts ?? []).map((part) => partName(part, language)).filter((name) => name !== '');
}

export type PartsView =
  | { readonly kind: 'loading' }
  | { readonly kind: 'error'; readonly code: string }
  | { readonly kind: 'ready'; readonly parts: readonly ActivePart[] };

/** What the parts dialog shows, from the state of the query that loads the active parts. */
export function partsView(input: {
  readonly status: 'pending' | 'error' | 'success';
  readonly data: readonly ActivePart[] | undefined;
  readonly error: unknown;
}): PartsView {
  if (input.data !== undefined) return { kind: 'ready', parts: input.data };
  if (input.status === 'error') return { kind: 'error', code: codeOf(input.error) };
  return { kind: 'loading' };
}

// --- the address (R29.5) ---------------------------------------------------------------------------

/** The query parameter the ticket list sets to ask the detail page for the Resolve form. */
export const RESOLVE_PARAM = 'resolve';

/** The address the list's "Resolve…" action goes to. It changes nothing on the server (R29.5). */
export function resolveLinkPath(ticketNumber: number): string {
  return `/tickets/list/${ticketNumber}?${RESOLVE_PARAM}=1`;
}

export interface ResolveUrlDecision {
  /** True when the Resolve form opens on load: the parameter is `resolve=1` and the ticket is in progress. */
  readonly open: boolean;
  /** The query string (with `?`, or empty) to replace the address with, or null when the address has no `resolve` parameter. */
  readonly nextSearch: string | null;
}

/**
 * What the detail page does with `?resolve=1` once the ticket is loaded (R29.5). The form opens only for a
 * ticket that is in progress; for any other status the parameter is ignored. The parameter is removed from the
 * address either way, so a reload or a later visit does not reopen the form. Other parameters stay.
 */
export function resolveUrlDecision(search: string, status: TicketStatus): ResolveUrlDecision {
  const params = new URLSearchParams(search);
  if (!params.has(RESOLVE_PARAM)) return { open: false, nextSearch: null };
  const asked = params.get(RESOLVE_PARAM) === '1';
  params.delete(RESOLVE_PARAM);
  const rest = params.toString();
  return { open: asked && status === 'in_progress', nextSearch: rest === '' ? '' : `?${rest}` };
}
