/**
 * The settlement part of `GET /staff/form-config` (R30.2, R30.6). The staff form-config answers for every
 * ticket staff role with the active settlements and `settlements_required`; `FormConfig` in `formTypes.ts`
 * does not declare them, so this file extends it locally and reads them with one react-query hook.
 *
 * The hook uses `staffKeys.formConfig`, the key the new-ticket page already uses, so both share one cached
 * answer.
 */
import { useQuery } from '@tanstack/react-query';
import type { FormConfig } from './formTypes';
import { getStaffFormConfig, staffKeys } from './staff';

/** An active settlement as the form-config lists it. */
export interface FormLocality {
  readonly id: string;
  readonly name_he: string;
  readonly name_en: string;
}

/** `GET /staff/form-config` with the settlement fields. Both are optional so an older answer still types. */
export interface StaffFormConfigExt extends FormConfig {
  readonly localities?: readonly FormLocality[];
  readonly settlements_required?: boolean;
}

const NO_SETTLEMENTS: readonly FormLocality[] = [];

/** The active settlements of a form-config answer, oldest first as the server sends them; empty when none. */
export function activeSettlements(config: StaffFormConfigExt | undefined): readonly FormLocality[] {
  const list = config?.localities;
  return Array.isArray(list) ? list : NO_SETTLEMENTS;
}

/** How long the cached list is fresh: settlements change rarely and a stale list is corrected on save. */
export const SETTLEMENTS_STALE_MS = 5 * 60 * 1000;

export interface SettlementsState {
  /** The active settlements; empty while loading, after an error and when none exists. */
  readonly settlements: readonly FormLocality[];
  /** True until the first answer or error arrives. */
  readonly loading: boolean;
}

/** The active settlements for a staff form or filter, from the cached `GET /staff/form-config`. */
export function useActiveSettlements(): SettlementsState {
  const query = useQuery({
    queryKey: staffKeys.formConfig,
    queryFn: ({ signal }) => getStaffFormConfig(signal),
    staleTime: SETTLEMENTS_STALE_MS,
    retry: false,
  });
  return { settlements: activeSettlements(query.data as StaffFormConfigExt | undefined), loading: query.isPending };
}
