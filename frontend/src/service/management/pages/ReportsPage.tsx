/**
 * The management report screen (task 24.6; R18.1, R18.7, R18.11, R18.12, R23.12). The container: it owns the
 * draft filters, the applied request and the queries. `ReportsView` draws it.
 *
 *  - The report loads when the screen opens, with every filter empty (all tickets, R18.1). Submitting the
 *    filters applies them and returns to page 1; the table pages 25 at a time with the applied filters.
 *  - A failed request (including 422 `invalid_date_range` and `invalid_filter`) shows an error message and
 *    keeps every value in the filter controls (R18.12). The figures of an earlier request are not shown next
 *    to filters they do not match.
 */
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { getReport, isStaffApiError, listCategories, staffKeys } from '../../api/staff';
import { SERVICE_NAMESPACE } from '../namespace';
import {
  EMPTY_REPORT_FILTERS,
  INITIAL_REPORT,
  sameReportRequest,
  shownReport,
  toReportQuery,
  type AppliedReport,
  type ReportFilters,
} from '../reportsModel';
import { ReportsView } from '../reportsView';
import { clampPage, listLanguage } from '../ticketListModel';

export function ReportsPage() {
  const { i18n } = useTranslation(SERVICE_NAMESPACE);
  const language = listLanguage(i18n.language);

  const [draft, setDraft] = useState<ReportFilters>(EMPTY_REPORT_FILTERS);
  const [applied, setApplied] = useState<AppliedReport>(INITIAL_REPORT);
  const request = useMemo(() => toReportQuery(applied), [applied]);

  const query = useQuery({
    queryKey: staffKeys.report(request),
    queryFn: ({ signal }) => getReport(request, signal),
    // The previous report stays on screen while the next page or filter set loads (no flash, no layout jump).
    placeholderData: keepPreviousData,
    // A 422 is final; the retry button repeats a request that failed for another reason.
    retry: false,
  });
  const categories = useQuery({
    queryKey: staffKeys.categories,
    queryFn: ({ signal }) => listCategories(signal),
  });

  const report = shownReport(query.data, query.isError);
  const errorCode = query.isError ? (isStaffApiError(query.error) ? query.error.code : 'generic') : null;

  // A page past the end (tickets were removed since it was shown): go to the last page.
  useEffect(() => {
    const data = query.data;
    if (data === undefined || query.isPlaceholderData || data.tickets.items.length > 0 || data.total === 0) return;
    const target = clampPage(applied.page, data.total);
    if (target !== applied.page) setApplied((current) => ({ ...current, page: target }));
  }, [query.data, query.isPlaceholderData, applied.page]);

  const submit = useCallback(() => {
    const next: AppliedReport = { filters: draft, page: 1 };
    if (sameReportRequest(next, applied)) void query.refetch();
    else setApplied(next);
  }, [applied, draft, query]);

  const clear = useCallback(() => {
    setDraft(EMPTY_REPORT_FILTERS);
    const next: AppliedReport = { filters: EMPTY_REPORT_FILTERS, page: 1 };
    if (sameReportRequest(next, applied)) void query.refetch();
    else setApplied(next);
  }, [applied, query]);

  const setPage = useCallback((page: number) => {
    setApplied((current) => ({ ...current, page }));
  }, []);

  return (
    <ReportsView
      language={language}
      draft={draft}
      onDraftChange={setDraft}
      onSubmit={submit}
      onClear={clear}
      categories={categories.data ?? []}
      report={report}
      busy={query.isFetching}
      errorCode={errorCode}
      onRetry={() => void query.refetch()}
      onPageChange={setPage}
    />
  );
}
