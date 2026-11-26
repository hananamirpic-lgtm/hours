/**
 * The markup of the management ticket list (task 24.3). Props in, markup out: no query, no timers, so a
 * static render can test it. `TicketListPage` owns the data and the handlers.
 *
 * R15.1 total and empty message, R15.2 columns, R15.7 SOS indicator and urgent row, R15.8 order toggle,
 * R15.9 and R15.11 status actions, R15.10 failed change, R15.12 list error, R23.12 labels and text.
 */
import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import type { StaffCategory, StaffTicketPage, StaffTicketRow, TicketStatus } from '../api/staffTypes';
import { SERVICE_NAMESPACE } from './namespace';
import { NEW_TICKET_PATH } from './newTicketModel';
import { resolveLinkPath } from './resolveModel';
import {
  STATUS_ORDER,
  categoryName,
  formatJerusalemMinute,
  isResolveTarget,
  listErrorField,
  listErrorKey,
  pageInfo,
  rowClassName,
  statusActionKey,
  statusActions,
  statusBadgeClass,
  statusFailureReasonKey,
  statusTextKey,
  ticketDetailPath,
  toggleStatus,
  type ListLanguage,
  type StatusFailure,
  type TicketListFilters,
} from './ticketListModel';
import './ticketList.css';

export interface TicketListViewProps {
  readonly language: ListLanguage;
  /** The values in the search box and the filter controls (not yet applied until submit). */
  readonly draft: TicketListFilters;
  readonly onDraftChange: (next: TicketListFilters) => void;
  readonly onSubmit: () => void;
  readonly onClear: () => void;
  readonly urgentFirst: boolean;
  readonly onUrgentFirstChange: (next: boolean) => void;
  /** Every category, active and inactive (R15.5). */
  readonly categories: readonly StaffCategory[];
  /** The rows to show: the loaded page, or the last page that loaded when a request failed. */
  readonly page: StaffTicketPage | null;
  /** True while a request is running. */
  readonly busy: boolean;
  /** The code of the failed list request, or null. */
  readonly listErrorCode: string | null;
  readonly onRetry: () => void;
  readonly statusFailure: StatusFailure | null;
  readonly onDismissFailure: () => void;
  /** Ticket numbers with a status change in flight. */
  readonly pendingTickets: ReadonlySet<number>;
  readonly onChangeStatus: (row: StaffTicketRow, target: TicketStatus) => void;
  readonly onPageChange: (page: number) => void;
}

/** The SOS_Indicator: an icon that assistive technology skips, and the text (R15.7). */
function SosIndicator({ text }: { text: string }) {
  return (
    <span className="ticket-list__sos">
      <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
        <path d="M12 2 1 21h22L12 2Zm1 14h-2v-5h2v5Zm0 3h-2v-2h2v2Z" fill="currentColor" />
      </svg>
      <span>{text}</span>
    </span>
  );
}

export function TicketListView(props: TicketListViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const {
    language,
    draft,
    onDraftChange,
    onSubmit,
    onClear,
    urgentFirst,
    onUrgentFirstChange,
    categories,
    page,
    busy,
    listErrorCode,
    onRetry,
    statusFailure,
    onDismissFailure,
    pendingTickets,
    onChangeStatus,
    onPageChange,
  } = props;

  const uid = useId();
  const id = (name: string): string => `${uid}-${name}`;
  const errorField = listErrorCode === null ? null : listErrorField(listErrorCode);
  const errorId = id('error');
  const describedBy = (field: 'search' | 'dates' | 'filters', extra?: string): string | undefined => {
    const ids = [extra, errorField === field ? errorId : undefined].filter((value) => value !== undefined);
    return ids.length === 0 ? undefined : ids.join(' ');
  };

  const set = (patch: Partial<TicketListFilters>): void => onDraftChange({ ...draft, ...patch });
  const info = page === null ? null : pageInfo(page.page, page.total);

  const failureAction =
    statusFailure === null
      ? ''
      : t(statusActionKey(statusFailure.from, statusFailure.target) ?? statusTextKey(statusFailure.target));

  return (
    <section className="page-body ticket-list">
      <header className="page-header">
        <h1>{t('management.tickets.title')}</h1>
        <div className="page-header__actions">
          <Link to={NEW_TICKET_PATH} className="button button--primary">
            {t('management.tickets.newTicket')}
          </Link>
        </div>
      </header>

      <form
        className="ticket-list__filters"
        role="search"
        aria-label={t('management.tickets.filters.title')}
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <div className="field">
          <label className="field__label" htmlFor={id('search')}>
            {t('management.tickets.searchLabel')}
          </label>
          <input
            id={id('search')}
            className="input"
            type="search"
            autoComplete="off"
            value={draft.search}
            aria-invalid={errorField === 'search' ? true : undefined}
            aria-describedby={describedBy('search', id('search-hint'))}
            onChange={(event) => set({ search: event.target.value })}
          />
          <p className="ticket-list__hint" id={id('search-hint')}>
            {t('management.tickets.searchHint')}
          </p>
        </div>

        <div className="ticket-list__grid">
          <fieldset className="ticket-list__fieldset" aria-describedby={describedBy('filters')}>
            <legend>{t('management.tickets.filters.status')}</legend>
            <div className="ticket-list__options">
              {STATUS_ORDER.map((status) => (
                <label className="checkbox-field" key={status}>
                  <input
                    type="checkbox"
                    checked={draft.statuses.includes(status)}
                    onChange={() => set({ statuses: toggleStatus(draft.statuses, status) })}
                  />
                  <span>{t(statusTextKey(status))}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <div className="field">
            <label className="field__label" htmlFor={id('category')}>
              {t('management.tickets.filters.category')}
            </label>
            <select
              id={id('category')}
              className="select"
              value={draft.categoryId}
              aria-describedby={describedBy('filters')}
              onChange={(event) => set({ categoryId: event.target.value })}
            >
              <option value="">{t('management.tickets.filters.allCategories')}</option>
              {categories.map((category) => {
                const name = categoryName(category, language);
                return (
                  <option key={category.id} value={category.id}>
                    {category.is_active ? name : t('management.tickets.filters.inactiveCategory', { name })}
                  </option>
                );
              })}
            </select>
          </div>

          <div className="field">
            <label className="field__label" htmlFor={id('city')}>
              {t('management.tickets.filters.city')}
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

          <div className="field">
            <label className="field__label" htmlFor={id('from')}>
              {t('management.tickets.filters.dateFrom')}
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
              {t('management.tickets.filters.dateTo')}
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

          <label className="checkbox-field">
            <input
              type="checkbox"
              checked={draft.urgentOnly}
              onChange={(event) => set({ urgentOnly: event.target.checked })}
            />
            <span>{t('management.tickets.filters.urgentOnly')}</span>
          </label>
        </div>

        <div className="ticket-list__buttons">
          <button type="submit" className="button button--primary">
            {t('management.common.search')}
          </button>
          <button type="button" className="button" onClick={onClear}>
            {t('management.common.clearFilters')}
          </button>
        </div>
      </form>

      <div className="ticket-list__order">
        <label className="checkbox-field">
          <input type="checkbox" checked={urgentFirst} onChange={(event) => onUrgentFirstChange(event.target.checked)} />
          <span>{t('management.tickets.urgentFirst')}</span>
        </label>
      </div>

      {listErrorCode !== null && (
        <div className="feedback feedback--error ticket-list__alert" role="alert" id={errorId}>
          <p>{t(listErrorKey(listErrorCode))}</p>
          <button type="button" className="button" onClick={onRetry}>
            {t('management.common.retry')}
          </button>
        </div>
      )}

      {statusFailure !== null && (
        <div className="feedback feedback--error ticket-list__alert" role="alert">
          <p>
            {t('management.tickets.statusChangeFailed', {
              action: failureAction,
              number: statusFailure.ticketNumber,
            })}{' '}
            {t(statusFailureReasonKey(statusFailure.code))}
          </p>
          <button type="button" className="button" onClick={onDismissFailure}>
            {t('management.common.close')}
          </button>
        </div>
      )}

      {page === null ? (
        busy ? (
          <p role="status">{t('management.common.loading')}</p>
        ) : null
      ) : (
        <>
          <p className="ticket-list__total" role="status">
            {t('management.tickets.total', { count: page.total })}
          </p>

          {page.total === 0 ? (
            <p className="table__empty">{t('management.tickets.empty')}</p>
          ) : (
            <div className="table-wrap">
              <table className="table" aria-busy={busy}>
                <caption className="ticket-list__sr-only">{t('management.tickets.title')}</caption>
                <thead>
                  <tr>
                    <th scope="col">{t('management.tickets.columns.number')}</th>
                    <th scope="col">{t('management.tickets.columns.created')}</th>
                    <th scope="col">{t('management.tickets.columns.customer')}</th>
                    <th scope="col">{t('management.tickets.columns.city')}</th>
                    <th scope="col">{t('management.tickets.columns.apartment')}</th>
                    <th scope="col">{t('management.tickets.columns.category')}</th>
                    <th scope="col">{t('management.tickets.columns.status')}</th>
                    <th scope="col">{t('management.tickets.columns.technician')}</th>
                    <th scope="col">{t('management.tickets.columns.sos')}</th>
                    <th scope="col">{t('management.common.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {page.items.map((row) => {
                    const actions = statusActions(row.status);
                    const pending = pendingTickets.has(row.ticket_number);
                    return (
                      <tr key={row.ticket_number} className={rowClassName(row)}>
                        <th scope="row">
                          <Link
                            className="ticket-list__link"
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
                          <bdi>{row.city}</bdi>
                        </td>
                        <td>
                          <bdi>{row.apartment}</bdi>
                        </td>
                        <td>
                          <bdi>{categoryName(row.category, language)}</bdi>
                        </td>
                        <td>
                          <span className={statusBadgeClass(row.status)}>{t(statusTextKey(row.status))}</span>
                        </td>
                        <td>
                          <bdi>{row.assigned_technician_name}</bdi>
                        </td>
                        <td>{row.urgent ? <SosIndicator text={t('management.tickets.sos')} /> : null}</td>
                        <td>
                          {actions.length === 0 ? (
                            <span className="ticket-list__hint">{t('management.tickets.actions.none')}</span>
                          ) : (
                            <div className="ticket-list__actions">
                              {actions.map((action) =>
                                isResolveTarget(action.target) ? (
                                  // Resolving needs the service history, so the list only opens the Resolve form
                                  // on the detail page. It sends nothing (R29.5).
                                  <Link
                                    key={action.target}
                                    to={resolveLinkPath(row.ticket_number)}
                                    className="button"
                                    aria-label={t('management.tickets.actionLabel', {
                                      action: t('management.tickets.actions.resolveLink'),
                                      number: row.ticket_number,
                                    })}
                                  >
                                    {t('management.tickets.actions.resolveLink')}
                                  </Link>
                                ) : (
                                  <button
                                    key={action.target}
                                    type="button"
                                    className="button"
                                    disabled={pending}
                                    aria-label={t('management.tickets.actionLabel', {
                                      action: t(action.labelKey),
                                      number: row.ticket_number,
                                    })}
                                    onClick={() => onChangeStatus(row, action.target)}
                                  >
                                    {t(action.labelKey)}
                                  </button>
                                ),
                              )}
                            </div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {info !== null && page.total > 0 && (
            <nav className="pagination" aria-label={t('management.common.paginationLabel')}>
              <span className="pagination__info">
                {t('management.common.range', {
                  first: info.first,
                  last: Math.min(info.last, info.first + page.items.length - 1),
                  total: page.total,
                })}
              </span>
              <button
                type="button"
                className="button"
                disabled={!info.hasPrevious}
                onClick={() => onPageChange(page.page - 1)}
              >
                {t('management.common.previous')}
              </button>
              <button
                type="button"
                className="button"
                disabled={!info.hasNext}
                onClick={() => onPageChange(page.page + 1)}
              >
                {t('management.common.next')}
              </button>
            </nav>
          )}
        </>
      )}
    </section>
  );
}
