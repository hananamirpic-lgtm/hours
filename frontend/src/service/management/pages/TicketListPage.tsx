/**
 * The management ticket list (task 24.3; R15.1 to R15.12, R23.12). The container: it owns the draft filters,
 * the applied request, the queries and the status-change flow. `TicketListView` draws it.
 *
 *  - Search and filters apply on submit; the order toggle applies at once. Either resets the page to 1.
 *  - A failed list request (including 422 `invalid_ticket_number`, `invalid_date_range`, `invalid_filter`)
 *    shows its message and keeps the rows already displayed (R15.4, R15.6, R15.12).
 *  - A status change sends the status the row displayed. On failure it shows an error that names the change,
 *    then reloads that ticket and shows its current status (R15.10).
 */
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  changeTicketStatus,
  getTicket,
  isStaffApiError,
  listCategories,
  listTickets,
  staffKeys,
  type StaffTicketPage,
  type StaffTicketRow,
  type TicketStatus,
} from '../../api/staff';
import { SERVICE_NAMESPACE } from '../namespace';
import {
  EMPTY_FILTERS,
  INITIAL_LIST,
  clampPage,
  displayedPage,
  listLanguage,
  replaceRow,
  sameRequest,
  toListQuery,
  type AppliedList,
  type StatusFailure,
  type TicketListFilters,
} from '../ticketListModel';
import { TicketListView } from '../ticketListView';

/** Every cached list page, whatever its filters: `staffKeys.ticketList(query)` starts with this. */
const LIST_KEY_PREFIX = [...staffKeys.tickets, 'list'] as const;

export function TicketListPage() {
  const { i18n } = useTranslation(SERVICE_NAMESPACE);
  const language = listLanguage(i18n.language);
  const queryClient = useQueryClient();

  const [draft, setDraft] = useState<TicketListFilters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<AppliedList>(INITIAL_LIST);
  const request = useMemo(() => toListQuery(applied), [applied]);

  const list = useQuery({
    queryKey: staffKeys.ticketList(request),
    queryFn: ({ signal }) => listTickets(request, signal),
    // The rows of the previous request stay on screen while the next one loads (no flash, no layout jump).
    placeholderData: keepPreviousData,
    // A 422 is final; the retry button repeats a request that failed for another reason.
    retry: false,
  });
  const categories = useQuery({
    queryKey: staffKeys.categories,
    queryFn: ({ signal }) => listCategories(signal),
  });

  // A failed request has no data of its own, so the last page that loaded stays displayed (R15.4, R15.6, R15.12).
  const [lastLoaded, setLastLoaded] = useState<StaffTicketPage | null>(null);
  useEffect(() => {
    if (list.data !== undefined && !list.isPlaceholderData) setLastLoaded(list.data);
  }, [list.data, list.isPlaceholderData]);
  const page = displayedPage(list.data, lastLoaded);

  // A page past the end (rows were removed since it was shown): go to the last page.
  useEffect(() => {
    const data = list.data;
    if (data === undefined || list.isPlaceholderData || data.items.length > 0 || data.total === 0) return;
    const target = clampPage(applied.page, data.total);
    if (target !== applied.page) setApplied((current) => ({ ...current, page: target }));
  }, [list.data, list.isPlaceholderData, applied.page]);

  const listErrorCode = list.isError ? (isStaffApiError(list.error) ? list.error.code : 'generic') : null;

  // --- search, filters, order, page ---------------------------------------------------------------------

  const submit = useCallback(() => {
    const next: AppliedList = { ...applied, filters: draft, page: 1 };
    if (sameRequest(next, applied)) void list.refetch();
    else setApplied(next);
  }, [applied, draft, list]);

  const clear = useCallback(() => {
    setDraft(EMPTY_FILTERS);
    const next: AppliedList = { ...applied, filters: EMPTY_FILTERS, page: 1 };
    if (sameRequest(next, applied)) void list.refetch();
    else setApplied(next);
  }, [applied, list]);

  const setUrgentFirst = useCallback((urgentFirst: boolean) => {
    setApplied((current) => ({ ...current, urgentFirst, page: 1 }));
  }, []);

  const setPage = useCallback((next: number) => {
    setApplied((current) => ({ ...current, page: next }));
  }, []);

  // --- status changes -----------------------------------------------------------------------------------

  const [pendingTickets, setPendingTickets] = useState<ReadonlySet<number>>(() => new Set());
  const [statusFailure, setStatusFailure] = useState<StatusFailure | null>(null);

  const showRow = useCallback(
    (row: StaffTicketRow) => {
      queryClient.setQueriesData<StaffTicketPage>({ queryKey: LIST_KEY_PREFIX }, (old) =>
        old === undefined ? old : replaceRow(old, row),
      );
      setLastLoaded((old) => (old === null ? old : replaceRow(old, row)));
      void queryClient.invalidateQueries({ queryKey: staffKeys.ticket(row.ticket_number) });
    },
    [queryClient],
  );

  const changeStatus = useCallback(
    async (row: StaffTicketRow, target: TicketStatus) => {
      const number = row.ticket_number;
      setStatusFailure(null);
      setPendingTickets((current) => new Set(current).add(number));
      try {
        // `expected_status` is the status the row displayed (R14.8, R15.9).
        showRow(await changeTicketStatus(number, { expected_status: row.status, target_status: target }));
      } catch (caught) {
        setStatusFailure({
          ticketNumber: number,
          from: row.status,
          target,
          code: isStaffApiError(caught) ? caught.code : 'generic',
        });
        try {
          showRow(await getTicket(number));
        } catch {
          // The ticket could not be read (gone, offline): reload the whole list instead.
          void list.refetch();
        }
      } finally {
        setPendingTickets((current) => {
          const next = new Set(current);
          next.delete(number);
          return next;
        });
      }
    },
    [list, showRow],
  );

  return (
    <TicketListView
      language={language}
      draft={draft}
      onDraftChange={setDraft}
      onSubmit={submit}
      onClear={clear}
      urgentFirst={applied.urgentFirst}
      onUrgentFirstChange={setUrgentFirst}
      categories={categories.data ?? []}
      page={page}
      busy={list.isFetching}
      listErrorCode={listErrorCode}
      onRetry={() => void list.refetch()}
      statusFailure={statusFailure}
      onDismissFailure={() => setStatusFailure(null)}
      pendingTickets={pendingTickets}
      onChangeStatus={(row, target) => void changeStatus(row, target)}
      onPageChange={setPage}
    />
  );
}
