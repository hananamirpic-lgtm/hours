/**
 * Resolve a site id to its name for the payroll allocation table.
 *
 * The payroll payload names a site by id only — a payroll record is money, not a directory — so the
 * allocation drill-down needs the site list to label each row (Requirement 16.8). The list is cached
 * under the shared site query key, so the several allocation rows on a card share one fetch, and the
 * screen never re-requests a list another screen already loaded. Until it resolves, or when the id is
 * absent from the loaded page, the row falls back to the id so it is never blank.
 */

import { useQuery } from '@tanstack/react-query';

import { listSites, siteKeys } from '@/api/sites';

const OPTION_PAGE = 200;

export function SiteName({ siteId }: { siteId: string }) {
  const params = { status: null, limit: OPTION_PAGE, offset: 0 };
  const sites = useQuery({
    queryKey: siteKeys.list(params),
    queryFn: () => listSites(params),
    staleTime: 5 * 60 * 1000,
  });
  const site = (sites.data?.items ?? []).find((candidate) => candidate.id === siteId);
  return <>{site ? site.name : siteId}</>;
}
