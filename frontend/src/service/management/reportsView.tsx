/**
 * The markup of the management report screen (task 24.6). Props in, markup out: no query, no timers, so a
 * static render can test it. `ReportsPage` owns the data and the handlers.
 *
 * R18.1 filters, R18.7 figures and table, R18.11 no-match message, R18.12 error that keeps the filters,
 * R23.12 labels, keyboard and text for status and SOS.
 */
import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import type { FormLocality } from '../api/staffFormConfig';
import type { PartOption } from '../api/staffReportsExtra';
import type { StaffCategory, StaffReport } from '../api/staffTypes';
import { SERVICE_NAMESPACE } from './namespace';
import {
  categoryCountEntries,
  exportErrorKey,
  isEmptyReport,
  partFilterOptions,
  partsCellText,
  reportErrorField,
  reportErrorKey,
  settlementCellText,
  statusCountEntries,
  type ExportState,
  type ReportFilters,
} from './reportsModel';
import {
  categoryName,
  formatJerusalemMinute,
  pageInfo,
  statusBadgeClass,
  statusTextKey,
  ticketDetailPath,
  type ListLanguage,
} from './ticketListModel';
import './reports.css';

export interface ReportsViewProps {
  readonly language: ListLanguage;
  /** The values in the filter controls (not applied until submit). Kept as entered when a request fails. */
  readonly draft: ReportFilters;
  readonly onDraftChange: (next: ReportFilters) => void;
  readonly onSubmit: () => void;
  readonly onClear: () => void;
  /** Every category, active and inactive (R18.6). */
  readonly categories: readonly StaffCategory[];
  /** The active settlements. While there are some, the settlement filter is a select; else a text input. */
  readonly settlements: readonly FormLocality[];
  /** The parts the "Part replaced" filter lists (all of them for Config_Staff, else the active ones). */
  readonly parts: readonly PartOption[];
  /** The state of the Excel export: its status and error messages, and whether the button is busy. */
  readonly exportState: ExportState;
  /** True when a report is on screen and no file is being prepared. */
  readonly canExport: boolean;
  readonly onExport: () => void;
  /** The report to show, or null while loading and after a failure. */
  readonly report: StaffReport | null;
  /** True while a request is running. */
  readonly busy: boolean;
  /** The code of the failed request, or null. */
  readonly errorCode: string | null;
  readonly onRetry: () => void;
  readonly onPageChange: (page: number) => void;
}

const PART_FILTER_TEXT_KEYS = {
  any: 'management.reports.partFilterAny',
  yes: 'management.reports.partFilterYes',
  no: 'management.reports.partFilterNo',
} as const;

/** The SOS_Indicator: an icon that assistive technology skips, and the text (R15.2, R23.12). */
function SosIndicator({ text }: { text: string }) {
  return (
    <span className="reports__sos">
      <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
        <path d="M12 2 1 21h22L12 2Zm1 14h-2v-5h2v5Zm0 3h-2v-2h2v2Z" fill="currentColor" />
      </svg>
      <span>{text}</span>
    </span>
  );
}

export function ReportsView(props: ReportsViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const {
    language,
    draft,
    onDraftChange,
    onSubmit,
    onClear,
    categories,
    settlements,
    parts,
    exportState,
    canExport,
    onExport,
    report,
    busy,
    errorCode,
    onRetry,
    onPageChange,
  } = props;

  const uid = useId();
  const id = (name: string): string => `${uid}-${name}`;
  const errorField = errorCode === null ? null : reportErrorField(errorCode);
  const errorId = id('error');
  const describedBy = (field: 'dates' | 'filters'): string | undefined =>
    errorField === field ? errorId : undefined;

  const set = (patch: Partial<ReportFilters>): void => onDraftChange({ ...draft, ...patch });

  const statuses = report === null ? [] : statusCountEntries(report.by_status);
  const categoryCounts = report === null ? [] : categoryCountEntries(report.by_category, language);
  const info = report === null ? null : pageInfo(report.tickets.page, report.tickets.total);
  const exporting = exportState.kind === 'preparing';
  const exportStatusId = id('export-status');

  return (
    <section className="page-body reports" aria-labelledby={id('title')}>
      <header className="page-header">
        <div>
          <h1 id={id('title')}>{t('management.reports.title')}</h1>
          <p className="subtitle">{t('management.reports.subtitle')}</p>
        </div>
      </header>

      <form
        className="reports__filters"
        role="search"
        aria-label={t('management.reports.filtersLabel')}
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <div className="reports__grid">
          <div className="field">
            <label className="field__label" htmlFor={id('from')}>
              {t('management.reports.dateFrom')}
            </label>
            <input
              id={id('from')}
              className="input"
              type="date"
              value={draft.dateFrom}
              aria-invalid={errorField === 'dates' ? true : undefined}
              aria-describedby={describedBy('dates')}
              onChange={(event) => set({ dateFrom: event.target.value })}
            />
          </div>

          <div className="field">
            <label className="field__label" htmlFor={id('to')}>
              {t('management.reports.dateTo')}
            </label>
            <input
              id={id('to')}
              className="input"
              type="date"
              value={draft.dateTo}
              aria-invalid={errorField === 'dates' ? true : undefined}
              aria-describedby={describedBy('dates')}
              onChange={(event) => set({ dateTo: event.target.value })}
            />
          </div>

          {settlements.length > 0 ? (
            <div className="field">
              <label className="field__label" htmlFor={id('settlement')}>
                {t('management.reports.settlementLabel')}
              </label>
              <select
                id={id('settlement')}
                className="select"
                value={draft.localityId}
                aria-invalid={errorField === 'filters' ? true : undefined}
                aria-describedby={describedBy('filters')}
                onChange={(event) => set({ localityId: event.target.value })}
              >
                <option value="">{t('management.reports.settlementAll')}</option>
                {settlements.map((settlement) => (
                  <option key={settlement.id} value={settlement.id}>
                    {categoryName(settlement, language)}
                  </option>
                ))}
              </select>
            </div>
          ) : (
            <div className="field">
              <label className="field__label" htmlFor={id('city')}>
                {t('management.reports.city')}
              </label>
              <input
                id={id('city')}
                className="input"
                type="text"
                autoComplete="off"
                value={draft.city}
                onChange={(event) => set({ city: event.target.value })}
              />
            </div>
          )}

          <div className="field">
            <label className="field__label" htmlFor={id('category')}>
              {t('management.reports.category')}
            </label>
            <select
              id={id('category')}
              className="select"
              value={draft.categoryId}
              aria-invalid={errorField === 'filters' ? true : undefined}
              aria-describedby={describedBy('filters')}
              onChange={(event) => set({ categoryId: event.target.value })}
            >
              <option value="">{t('management.reports.allCategories')}</option>
              {categories.map((category) => {
                const name = categoryName(category, language);
                return (
                  <option key={category.id} value={category.id}>
                    {category.is_active ? name : t('management.reports.inactiveCategory', { name })}
                  </option>
                );
              })}
            </select>
          </div>

          <div className="field">
            <label className="field__label" htmlFor={id('part')}>
              {t('management.reports.partFilterLabel')}
            </label>
            <select
              id={id('part')}
              className="select"
              value={draft.partFilter}
              aria-invalid={errorField === 'filters' ? true : undefined}
              aria-describedby={describedBy('filters')}
              onChange={(event) => set({ partFilter: event.target.value })}
            >
              {partFilterOptions(parts, language).map((option) => {
                if (option.kind === 'part') {
                  return (
                    <option key={option.value} value={option.value}>
                      {option.inactive ? t('management.reports.partFilterInactive', { name: option.name }) : option.name}
                    </option>
                  );
                }
                return (
                  <option key={option.value} value={option.value}>
                    {t(PART_FILTER_TEXT_KEYS[option.kind])}
                  </option>
                );
              })}
            </select>
          </div>
        </div>

        <div className="reports__buttons">
          <button type="submit" className="button button--primary">
            {t('management.reports.show')}
          </button>
          <button type="button" className="button" onClick={onClear}>
            {t('management.common.clearFilters')}
          </button>
          <button
            type="button"
            className="button"
            disabled={!canExport}
            aria-busy={exporting}
            aria-describedby={exportStatusId}
            onClick={onExport}
          >
            {t('management.reports.exportButton')}
          </button>
        </div>
      </form>

      <div id={exportStatusId} className="reports__export-status" role="status">
        {exportState.kind === 'preparing' ? t('management.reports.exportPreparing') : null}
        {exportState.kind === 'done' ? t('management.reports.exportDone') : null}
      </div>
      {exportState.kind === 'failed' && (
        <div className="feedback feedback--error reports__alert" role="alert">
          <p>{t(exportErrorKey(exportState.code))}</p>
        </div>
      )}

      {errorCode !== null && (
        <div className="feedback feedback--error reports__alert" role="alert" id={errorId}>
          <p>{t(reportErrorKey(errorCode))}</p>
          <button type="button" className="button" onClick={onRetry}>
            {t('management.common.retry')}
          </button>
        </div>
      )}

      {report === null ? (
        busy && errorCode === null ? <p role="status">{t('management.common.loading')}</p> : null
      ) : (
        <div className="reports__results" aria-busy={busy}>
          <h2 className="reports__heading">{t('management.reports.summary')}</h2>
          <dl className="reports__figures">
            <div className="stat-card reports__figure">
              <dt>{t('management.reports.total')}</dt>
              <dd className="reports__value">
                <bdi dir="ltr">{report.total}</bdi>
              </dd>
            </div>
            <div className="stat-card reports__figure">
              <dt>{t('management.reports.open')}</dt>
              <dd className="reports__value">
                <bdi dir="ltr">{report.open}</bdi>
              </dd>
            </div>
          </dl>

          <h2 className="reports__heading">{t('management.reports.byStatus')}</h2>
          <dl className="reports__figures">
            {statuses.map((entry) => (
              <div className="stat-card reports__figure" key={entry.status}>
                <dt>{t(statusTextKey(entry.status))}</dt>
                <dd className="reports__value">
                  <bdi dir="ltr">{entry.count}</bdi>
                </dd>
              </div>
            ))}
          </dl>

          <h2 className="reports__heading">{t('management.reports.byCategory')}</h2>
          {categoryCounts.length === 0 ? (
            <p className="reports__hint">{t('management.reports.noCategories')}</p>
          ) : (
            <dl className="reports__figures">
              {categoryCounts.map((entry) => (
                <div className="stat-card reports__figure" key={entry.id}>
                  <dt>
                    <bdi>{entry.name}</bdi>
                  </dt>
                  <dd className="reports__value">
                    <bdi dir="ltr">{entry.count}</bdi>
                  </dd>
                </div>
              ))}
            </dl>
          )}

          <h2 className="reports__heading">{t('management.reports.tickets')}</h2>
          {isEmptyReport(report) ? (
            <p className="table__empty">{t('management.reports.empty')}</p>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <caption className="reports__sr-only">{t('management.reports.tickets')}</caption>
                <thead>
                  <tr>
                    <th scope="col">{t('management.tickets.columns.number')}</th>
                    <th scope="col">{t('management.tickets.columns.created')}</th>
                    <th scope="col">{t('management.tickets.columns.customer')}</th>
                    <th scope="col">{t('management.tickets.columns.city')}</th>
                    <th scope="col">{t('management.tickets.columns.apartment')}</th>
                    <th scope="col">{t('management.tickets.columns.category')}</th>
                    <th scope="col">{t('management.reports.columns.parts')}</th>
                    <th scope="col">{t('management.tickets.columns.status')}</th>
                    <th scope="col">{t('management.tickets.columns.technician')}</th>
                    <th scope="col">{t('management.tickets.columns.sos')}</th>
                  </tr>
                </thead>
                <tbody>
                  {report.tickets.items.map((row) => (
                    <tr key={row.ticket_number}>
                      <th scope="row">
                        <Link
                          className="reports__link"
                          to={ticketDetailPath(row.ticket_number)}
                          aria-label={t('management.tickets.openTicket', { number: row.ticket_number })}
                        >
                          <bdi dir="ltr">{row.ticket_number}</bdi>
                        </Link>
                      </th>
                      <td>
                        <bdi dir="ltr">{formatJerusalemMinute(row.created_at)}</bdi>
                      </td>
                      <td>
                        <bdi>{row.customer_name}</bdi>
                      </td>
                      <td>
                        <bdi>{settlementCellText(row, language)}</bdi>
                      </td>
                      <td>
                        <bdi>{row.apartment}</bdi>
                      </td>
                      <td>
                        <bdi>{categoryName(row.category, language)}</bdi>
                      </td>
                      <td>
                        <bdi>{partsCellText(row, language)}</bdi>
                      </td>
                      <td>
                        <span className={statusBadgeClass(row.status)}>{t(statusTextKey(row.status))}</span>
                      </td>
                      <td>
                        <bdi>{row.assigned_technician_name}</bdi>
                      </td>
                      <td>{row.urgent ? <SosIndicator text={t('management.tickets.sos')} /> : null}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {info !== null && report.tickets.total > 0 && (
            <nav className="pagination" aria-label={t('management.common.paginationLabel')}>
              <span className="pagination__info">
                {t('management.common.range', {
                  first: info.first,
                  last: Math.min(info.last, info.first + report.tickets.items.length - 1),
                  total: report.tickets.total,
                })}
              </span>
              <button
                type="button"
                className="button"
                disabled={!info.hasPrevious}
                onClick={() => onPageChange(report.tickets.page - 1)}
              >
                {t('management.common.previous')}
              </button>
              <button
                type="button"
                className="button"
                disabled={!info.hasNext}
                onClick={() => onPageChange(report.tickets.page + 1)}
              >
                {t('management.common.next')}
              </button>
            </nav>
          )}
        </div>
      )}
    </section>
  );
}
