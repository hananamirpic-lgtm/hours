// Wire types of the second-round configuration endpoints under `/api/service/staff` (task 30.7): the settlement
// list, the replaceable-parts list and the branding (company name and logo).
//
// Hand-written mirror of `LocalityRecord`, `PartRecord`, `LocalityDeleteResult`, `PartDeleteResult` and
// `BrandingInfo` in backend/app/service_tickets/schemas.py. Pure types: no React, no DOM.

/** `LocalityRecord` and `PartRecord`: an item of a named list for configuration staff, active or inactive. */
export interface StaffNamedItem {
  readonly id: string;
  readonly name_he: string;
  readonly name_en: string;
  readonly is_active: boolean;
}

/** A settlement of the list (R30.1). */
export type StaffLocality = StaffNamedItem;

/** A replaceable part of the list (R29.6). */
export type StaffPart = StaffNamedItem;

/** The body of a create or a rename. Both names go as typed; the server trims and checks them. */
export interface NamedItemBody {
  readonly name_he: string;
  readonly name_en: string;
}

/** `LocalityDeleteResult` and `PartDeleteResult`: removed, or kept and deactivated because it is used. */
export type NamedItemDeleteResult = 'removed' | 'deactivated';

/** The two lists that work as the issue categories do. */
export type NamedListKind = 'localities' | 'parts';

/** `BrandingInfo`: the company name (or null) and whether a logo is stored. The key is never sent. */
export interface StaffBranding {
  readonly company_name: string | null;
  readonly has_logo: boolean;
}

/**
 * What one save of the branding screen sends, and nothing else (R28.1). An absent member leaves that part
 * as it is. `companyName` is sent as typed; the server trims it and checks 1 to 100 characters.
 */
export interface BrandingChange {
  readonly companyName?: string;
  readonly logo?: Blob;
  readonly removeName?: boolean;
  readonly removeLogo?: boolean;
}
