// The settlement choice and the branding header without React (task 30.8; R28.3, R28.4, R28.7, R30.2,
// R30.3, R30.5, R30.6, R30.8). Everything the form and the dialog decide is a pure function here, so it is
// testable without a DOM: the name in the current language, the search filter, the live-region text, the
// prefill rule, and what the branding header shows.
import type { FormConfig, FormLocality } from '../../api/formTypes';

export type SettlementLanguage = 'he' | 'en';

/** A settlement with its two names; the shape of `GET /form-config` `localities` and of `locality` on responses. */
export type SettlementNames = Pick<FormLocality, 'name_he' | 'name_en'>;

// --- names -----------------------------------------------------------------------------------------

/** The name in `language`; the other language when that name is blank, so a label is never empty (R30.5). */
export function settlementName(settlement: SettlementNames, language: SettlementLanguage): string {
  const own = (language === 'he' ? settlement.name_he : settlement.name_en).trim();
  if (own !== '') return own;
  return (language === 'he' ? settlement.name_en : settlement.name_he).trim();
}

/**
 * The place of a ticket as the portal shows it: the settlement's name in the current language when the
 * response carries a settlement, else the stored city text (R30.5).
 */
export function placeText(
  locality: SettlementNames | null | undefined,
  city: string | null | undefined,
  language: SettlementLanguage,
): string {
  if (locality !== null && locality !== undefined) {
    const name = settlementName(locality, language);
    if (name !== '') return name;
  }
  return city ?? '';
}

// --- the configuration -----------------------------------------------------------------------------

export interface SettlementChoice {
  /** True while the City field is a choice from the list (R30.2); false while it is free text (R30.3). */
  readonly required: boolean;
  readonly localities: readonly FormLocality[];
}

/** The settlement part of a form configuration; a missing member is no list and free text. */
export function settlementChoice(config: Pick<FormConfig, 'localities' | 'settlements_required'>): SettlementChoice {
  return { required: config.settlements_required === true, localities: config.localities ?? [] };
}

/** The settlement with this id from the list, or null (also for an empty id). */
export function findSettlement(localities: readonly FormLocality[], id: string | null | undefined): FormLocality | null {
  if (id === null || id === undefined || id === '') return null;
  return localities.find((locality) => locality.id === id) ?? null;
}

/**
 * The settlement preselected when the form opens (R30.6): the customer's own settlement when it is in the
 * active list, else nothing (the empty id). Nothing is preselected from the free-text city.
 */
export function prefillLocalityId(
  profileLocality: { readonly id: string } | null | undefined,
  localities: readonly FormLocality[],
): string {
  return findSettlement(localities, profileLocality?.id)?.id ?? '';
}

// --- the search ------------------------------------------------------------------------------------

/** A search text or a name compared as typed, ignoring case, surrounding blanks and composed/decomposed forms. */
export function normalizeSearch(text: string): string {
  return text.normalize('NFC').trim().toLowerCase();
}

/**
 * The settlements whose name in either language contains the search text, ignoring case (R30.2). The order
 * the server sent is kept. A blank search matches all. The other language matches too, so a resident with
 * the Hebrew keyboard still finds a settlement while the page is in English.
 */
export function filterSettlements(localities: readonly FormLocality[], query: string): FormLocality[] {
  const needle = normalizeSearch(query);
  if (needle === '') return [...localities];
  return localities.filter(
    (locality) => normalizeSearch(locality.name_he).includes(needle) || normalizeSearch(locality.name_en).includes(needle),
  );
}

/** The locale key (and plural count) of the live-region text for a number of matches (R30.8). */
export function matchesText(count: number): { readonly key: string; readonly count?: number } {
  return count === 0 ? { key: 'portal.settlementDialog.noMatches' } : { key: 'portal.settlementDialog.matches', count };
}

// --- the branding header ---------------------------------------------------------------------------

export interface BrandingView {
  /** The company name as plain text, or null. */
  readonly name: string | null;
  /** The object URL of the logo, or null while it is not there (not asked for, loading, or failed). */
  readonly logoUrl: string | null;
  /** The text alternative of the logo: the company name, or empty so assistive technology skips it (R28.3). */
  readonly logoAlt: string;
}

/**
 * What the branding header shows, or null when it shows nothing at all: no name and no logo means no
 * element, no placeholder and no space (R28.4). A logo that failed to load simply is not there.
 */
export function brandingView(
  branding: { readonly company_name: string | null; readonly has_logo: boolean } | null | undefined,
  logoUrl: string | null,
): BrandingView | null {
  const trimmed = branding?.company_name?.trim() ?? '';
  const name = trimmed === '' ? null : trimmed;
  const url = branding?.has_logo === true ? logoUrl : null;
  if (name === null && url === null) return null;
  return { name, logoUrl: url, logoAlt: name ?? '' };
}
