/**
 * Resolve a client id to its name for the billing tables.
 *
 * The billing payload aggregates per client by id (Requirement 17.3), so the per-client rows and the
 * drill-down header need the client list to label each one. The list is cached under the shared
 * client query key, so every client row on the page shares one fetch. Until it resolves, or when the
 * id is absent from the loaded page, the row falls back to the id so it is never blank.
 */

import { useQuery } from '@tanstack/react-query';

import { clientKeys, listClients } from '@/api/clients';

const OPTION_PAGE = 200;

/** Resolve a client id to its name, or the id when the roster has not loaded it. Not a component, so
 * a header string or an `aria-label` can use the same lookup a row does. */
export function useClientName(): (clientId: string) => string {
  const params = { includeArchived: true, limit: OPTION_PAGE, offset: 0 };
  const clients = useQuery({
    queryKey: clientKeys.list(params),
    queryFn: () => listClients(params),
    staleTime: 5 * 60 * 1000,
  });
  return (clientId: string): string => {
    const client = (clients.data?.items ?? []).find((candidate) => candidate.id === clientId);
    return client ? client.name : clientId;
  };
}

export function ClientName({ clientId }: { clientId: string }) {
  const nameFor = useClientName();
  return <>{nameFor(clientId)}</>;
}
