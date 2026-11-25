/**
 * The management report screen (task 24.6; R18.1, R18.7, R18.11, R18.12, R23.12). The container: it owns the
 * draft filters, the applied request and the queries. `ReportsView` draws it.
 *
 *  - The report loads when the screen opens, with every filter empty (all tickets, R18.1). Submitting the
 *    filters applies them and returns to page 1; the table pages 25 at a time with the applied filters.
 *  - A failed request (including 422 `invalid_date_range` and `invalid_filter`) shows an error message and
 *    keeps every value in the filter controls (R18.12). The figures of an earlier request are not shown next
 *    to filters they do not match.
 *  - The settlement filter is a select of the active settlements while that list is not empty, else the text
 *    input (R30.7). The "Part replaced" filter lists every part for Config_Staff and the active ones for the
 *    other ticket staff (R29.8).
 *  - "Export to Excel" downloads the filters APPLIED to the report, not the unsubmitted edits, in the language
 *    of the screen (R31.1), with a status while it prepares and an error message that keeps the filters (R31.6).
 */
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '@/auth/AuthProvider';
import { isStaffApiError, listCategories, staffKeys } from '../../api/staff';
import { useActiveSettlements } from '../../api/staffFormConfig';
import {
  REPORT_EXPORT_FILE_NAME,
  exportReportExt,
  getReportExt,
  listPartOptions,
  partKeys,
  saveBlob,
} from '../../api/staffReportsExtra';
import { isConfigStaffRole } from '../guards';
import { SERVICE_NAMESPACE } from '../namespace';
import {
  EMPTY_REPORT_FILTERS,
  EXPORT_IDLE,
  INITIAL_REPORT,
  canExport,
  effectiveReportFilters,
  sameReportRequest,
  shownReport,
  toExportQuery,
  toReportQuery,
  type AppliedReport,
  type ExportState,
  type ReportFilters,
} from '../reportsModel';
import { ReportsView } from '../reportsView';
import { clampPage, listLanguage } from '../ticketListModel';

export function ReportsPage() {
  const { i18n } = useTranslation(SERVICE_NAMESPACE);
  const language = listLanguage(i18n.language);
  const { user } = useAuth();
  const configStaff = isConfigStaffRole(user?.role);

  const [draft, setDraft] = useState<ReportFilters>(EMPTY_REPORT_FILTERS);
  const [applied, setApplied] = useState<AppliedReport>(INITIAL_REPORT);
  const request = useMemo(() => toReportQuery(applied), [applied]);

  const query = useQuery({
    queryKey: staffKeys.report(request),
    queryFn: ({ signal }) => getReportExt(request, signal),
    // The previous report stays on screen while the next page or filter set loads (no flash, no layout jump).
    placeholderData: keepPreviousData,
    // A 422 is final; the retry button repeats a request that failed for another reason.
    retry: false,
  });
  const categories = useQuery({
    queryKey: staffKeys.categories,
    queryFn: ({ signal }) => listCategories(signal),
  });

  const { settlements } = useActiveSettlements();
  const settlementList = settlements.length > 0;
  const parts = useQuery({
    queryKey: configStaff ? partKeys.all : partKeys.active,
    queryFn: ({ signal }) => listPartOptions(configStaff, signal),
    retry: false,
  });

  const [exportState, setExportState] = useState<ExportState>(EXPORT_IDLE);
  // The request of the running export, cancelled when the screen closes.
  const exportAbort = useRef<AbortController | null>(null);
  useEffect(() => () => exportAbort.current?.abort(), []);

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
    const next: AppliedReport = { filters: effectiveReportFilters(draft, settlementList), page: 1 };
    setExportState((current) => (current.kind === 'preparing' ? current : EXPORT_IDLE));
    if (sameReportRequest(next, applied)) void query.refetch();
    else setApplied(next);
  }, [applied, draft, query, settlementList]);

  const exportFile = useCallback(() => {
    if (!canExport(report, exportState)) return;
    const controller = new AbortController();
    exportAbort.current = controller;
    setExportState({ kind: 'preparing' });
    exportReportExt(toExportQuery(applied), language, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return;
        saveBlob(blob, REPORT_EXPORT_FILE_NAME);
        setExportState({ kind: 'done' });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setExportState({ kind: 'failed', code: isStaffApiError(error) ? error.code : 'generic' });
      })
      .finally(() => {
        if (exportAbort.current === controller) exportAbort.current = null;
      });
  }, [applied, exportState, language, report]);

  const clear = useCallback(() => {
    setDraft(EMPTY_REPORT_FILTERS);
    setExportState((current) => (current.kind === 'preparing' ? current : EXPORT_IDLE));
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
      settlements={settlements}
      parts={parts.data ?? []}
      exportState={exportState}
      canExport={canExport(report, exportState)}
      onExport={exportFile}
      report={report}
      busy={query.isFetching}
      errorCode={errorCode}
      onRetry={() => void query.refetch()}
      onPageChange={setPage}
    />
  );
}
