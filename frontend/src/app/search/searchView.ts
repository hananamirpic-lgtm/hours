/**
 * The pure shaping behind the global search UI (Requirement 22.1, 22.5, 21.5).
 *
 * The API returns the three groups already scoped to the caller and ordered stably. The screen shows
 * them under headings — employees, sites, clients — and lets the keyboard move through the whole
 * result as one list, across the headings, the way a command palette does. Keeping that logic pure
 * means the flattening and the arrow-key arithmetic are tested against fixed inputs here rather than
 * through a rendered popover, and the component stays a thin presentation over the result.
 *
 * Three things live here.
 *
 * **A label in the reader's language, with the other form to disambiguate (Requirement 21.5).** An
 * employee carries both name forms; the hit reads the reader's-language form first and appends the
 * other in parentheses only when it differs, so a bilingual name is legible either way and a
 * one-language name is not doubled. A site and a client carry one name, shown as-is.
 *
 * **A flat, ordered list of navigable items across the groups (Requirement 22.5).** The three groups
 * are concatenated in a fixed order — employees, then sites, then clients — each item tagged with its
 * kind and a stable key, so the arrow keys step through every hit regardless of which heading it sits
 * under, and the order the API returned is preserved within each group.
 *
 * **Where a hit goes when it is chosen.** Each kind resolves to the console list route that shows it,
 * with the id in the query so the screen can open or highlight it. The routing is data, not branching
 * scattered through the component: one function maps a hit to its path.
 */

import type {
  ClientSearchHit,
  EmployeeSearchHit,
  SearchResults,
  SiteSearchHit,
} from '@/api/types';
import type { Language } from '@/i18n';

/** The kinds of hit a search returns, in the order the results render and the keyboard steps. */
export type HitKind = 'employee' | 'site' | 'client';

/**
 * One hit as the list renders and the keyboard moves over it: its kind, a key stable across renders,
 * the label in the reader's language, an optional second line (the site number, the company, the
 * status) and the console path it opens. `index` is its position in the flattened list, so a keyed
 * row and the active index agree without re-deriving the order.
 */
export interface SearchItem {
  kind: HitKind;
  id: string;
  key: string;
  index: number;
  label: string;
  detail: string | null;
  to: string;
}

/** A rendered group: its kind, the total that matched before paging, and its items in order. */
export interface RenderGroup {
  kind: HitKind;
  total: number;
  items: SearchItem[];
}

/** The flattened result: the groups in render order, and the single navigable list across them. */
export interface FlatResults {
  groups: RenderGroup[];
  items: SearchItem[];
  /** How many navigable items there are in total — the count the keyboard wraps around. */
  count: number;
}

/**
 * A label in the reader's language, with the other name form appended only when it differs, and the
 * employee's 4-digit number appended when present (Requirement 6). The number goes last so the
 * name reads first: "Name (OtherForm) #2047", or "Name #2047" when both forms match, or just the
 * name when there is no number.
 */
export const employeeLabel = (
  language: Language,
  name: string,
  nameEn: string,
  employeeNumber?: string | null,
): string => {
  const primary = language === 'he' ? name : nameEn;
  const secondary = language === 'he' ? nameEn : name;
  const base = primary === secondary ? primary : `${primary} (${secondary})`;
  return employeeNumber ? `${base} #${employeeNumber}` : base;
};

/** The console path a chosen hit opens, carrying the id so the screen can focus it. */
export const hitPath = (kind: HitKind, id: string): string => {
  switch (kind) {
    case 'employee':
      return `/employees?id=${encodeURIComponent(id)}`;
    case 'site':
      return `/sites?id=${encodeURIComponent(id)}`;
    case 'client':
      return `/clients?id=${encodeURIComponent(id)}`;
  }
};

const employeeItem = (language: Language, hit: EmployeeSearchHit, index: number): SearchItem => ({
  kind: 'employee',
  id: hit.id,
  key: `employee::${hit.id}`,
  index,
  label: employeeLabel(language, hit.full_name, hit.full_name_en),
  detail: null,
  to: hitPath('employee', hit.id),
});

const siteItem = (hit: SiteSearchHit, index: number): SearchItem => ({
  kind: 'site',
  id: hit.id,
  key: `site::${hit.id}`,
  index,
  label: hit.name,
  detail: hit.site_number,
  to: hitPath('site', hit.id),
});

const clientItem = (hit: ClientSearchHit, index: number): SearchItem => ({
  kind: 'client',
  id: hit.id,
  key: `client::${hit.id}`,
  index,
  label: hit.name,
  detail: hit.company,
  to: hitPath('client', hit.id),
});

/**
 * Flatten the grouped API result into the render groups and the single navigable list across them
 * (Requirement 22.1, 22.5). The groups keep a fixed order — employees, sites, clients — and each
 * item's `index` is its position in that concatenation, so the keyboard steps through every hit in
 * that order and a keyed row and the active index never disagree. An empty result flattens to no
 * groups and no items. `null` (no search run yet) is treated the same as an empty result.
 */
export const flattenResults = (
  results: SearchResults | null | undefined,
  language: Language,
): FlatResults => {
  if (!results) {
    return { groups: [], items: [], count: 0 };
  }

  const items: SearchItem[] = [];

  const employees = results.employees.items.map((hit) => {
    const item = employeeItem(language, hit, items.length);
    items.push(item);
    return item;
  });
  const sites = results.sites.items.map((hit) => {
    const item = siteItem(hit, items.length);
    items.push(item);
    return item;
  });
  const clients = results.clients.items.map((hit) => {
    const item = clientItem(hit, items.length);
    items.push(item);
    return item;
  });

  const groups: RenderGroup[] = [];
  if (employees.length > 0) {
    groups.push({ kind: 'employee', total: results.employees.total, items: employees });
  }
  if (sites.length > 0) {
    groups.push({ kind: 'site', total: results.sites.total, items: sites });
  }
  if (clients.length > 0) {
    groups.push({ kind: 'client', total: results.clients.total, items: clients });
  }

  return { groups, items, count: items.length };
};

/**
 * The active index after an arrow key, wrapping around the flattened list (Requirement 22.5).
 *
 * `step` is +1 for Down and -1 for Up. Over an empty list there is nothing to point at, so the active
 * index is cleared to `null`. From no selection, Down lands on the first item and Up on the last, so
 * the first arrow always selects. Otherwise the index moves by `step` modulo the count, so Down past
 * the end wraps to the top and Up past the start wraps to the bottom — the palette behaviour a user
 * expects, and one that can never point outside the list.
 */
export const moveActive = (
  active: number | null,
  step: 1 | -1,
  count: number,
): number | null => {
  if (count === 0) {
    return null;
  }
  if (active === null) {
    return step === 1 ? 0 : count - 1;
  }
  return (active + step + count) % count;
};
