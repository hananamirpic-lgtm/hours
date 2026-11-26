// The Customers screen of the Management_Interface (task 24.5; R3.1, R3.2, R3.3, R3.4, R3.5, R3.10, R3.12,
// R3.14, R23.12). Open to all Ticket_Staff and separate from the Hours Users page.
//
// The list is the server's: 50 per page, sorted by name, filtered by a name or phone search and by state.
// Page, search text and state filter live in the address (`?page=`, `?q=`, `?active=`), so a reload or a
// shared link shows the same list. Create and edit open the form in a dialog; deactivate, reactivate and
// reset password open a confirmation. Each action ends with a status message in a live region, the list is
// reloaded, and focus returns to the control that opened the dialog (or to the heading when that control no
// longer exists). No password, hash or initial password value is shown: the API returns none (R3.12).
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';
import { listCustomers, staffKeys, type StaffCustomer } from '../../api/staff';
import { CustomerConfirmDialog } from '../CustomerConfirmDialog';
import { CustomerForm } from '../CustomerForm';
import {
  actionTexts,
  hasActiveFilters,
  listStateFromParams,
  listStateToParams,
  listStateToQuery,
  paginationState,
  pastTheEndPage,
  withActive,
  withPage,
  withSearch,
  DEFAULT_LIST_STATE,
  type ActiveFilter,
  type CustomerAction,
  type CustomersListState,
} from '../customersModel';
import { CustomersTable } from '../CustomersTable';
import { CustomersToolbar } from '../CustomersToolbar';
import { SERVICE_NAMESPACE } from '../namespace';
import '../customers.css';

type DialogState =
  | { readonly kind: 'create' }
  | { readonly kind: 'edit'; readonly customer: StaffCustomer }
  | { readonly kind: 'action'; readonly customer: StaffCustomer; readonly action: CustomerAction };

export function CustomersPage() {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const [params, setParams] = useSearchParams();
  const state = useMemo(() => listStateFromParams(params), [params]);
  const [searchDraft, setSearchDraft] = useState(state.search);
  const [dialog, setDialog] = useState<DialogState | null>(null);
  // The text key of the last success, shown in the live region. Null when nothing happened yet.
  const [notice, setNotice] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const dialogWasOpen = useRef(false);

  const go = (next: CustomersListState, replace = false): void => {
    setParams(listStateToParams(next), { replace });
  };

  // The search box follows the address (clear filters, the back button).
  useEffect(() => {
    setSearchDraft(state.search);
  }, [state.search]);

  const request = useMemo(() => listStateToQuery(state), [state]);
  const list = useQuery({
    queryKey: staffKeys.customerList(request),
    queryFn: ({ signal }) => listCustomers(request, signal),
    placeholderData: keepPreviousData,
  });
  const data = list.data;

  // A page past the end (an address typed by hand, or the last row of the last page gone) shows the last page.
  const pastEnd = data !== undefined && !list.isPlaceholderData ? pastTheEndPage(state.page, data.total) : null;
  useEffect(() => {
    if (pastEnd !== null) setParams(listStateToParams(withPage(state, pastEnd)), { replace: true });
  }, [pastEnd, state, setParams]);

  // When a dialog closes, focus goes back to the control that opened it, or to the heading when it is gone.
  useEffect(() => {
    if (dialog !== null) {
      dialogWasOpen.current = true;
      return;
    }
    if (!dialogWasOpen.current) return;
    dialogWasOpen.current = false;
    const opener = openerRef.current;
    openerRef.current = null;
    if (opener !== null && opener.isConnected) opener.focus();
    else headingRef.current?.focus();
  }, [dialog]);

  const open = (next: DialogState): void => {
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setNotice(null);
    setDialog(next);
  };

  const rows = data?.items ?? [];
  const showRows = data !== undefined && rows.length > 0;
  const showEmpty = data !== undefined && rows.length === 0 && data.total === 0;
  const pagination = data === undefined ? null : paginationState(state.page, data.total, rows.length);
  const loadFailed = list.isError;

  return (
    <section className="page-body">
      <div className="page-header">
        <h1 ref={headingRef} tabIndex={-1}>
          {t('management.customers.title')}
        </h1>
        <div className="page-header__actions">
          <button type="button" className="button button--primary" onClick={() => open({ kind: 'create' })}>
            {t('management.customers.new')}
          </button>
        </div>
      </div>
      <p className="subtitle">{t('management.customers.subtitle')}</p>

      <div role="status" aria-live="polite" className="customers-notice">
        {notice === null ? null : <p className="feedback feedback--success">{t(notice)}</p>}
      </div>

      <CustomersToolbar
        searchDraft={searchDraft}
        active={state.active}
        canClear={hasActiveFilters(state) || searchDraft.trim() !== ''}
        onSearchDraftChange={setSearchDraft}
        onSearchSubmit={() => go(withSearch(state, searchDraft.trim()))}
        onActiveChange={(active: ActiveFilter) => go(withActive(state, active))}
        onClear={() => {
          setSearchDraft('');
          go(DEFAULT_LIST_STATE);
        }}
      />

      {loadFailed ? (
        <div className="feedback feedback--error customers-load-error" role="alert">
          <p>{t('management.customers.loadError')}</p>
          <button type="button" className="button button--small" onClick={() => void list.refetch()}>
            {t('management.common.retry')}
          </button>
        </div>
      ) : null}

      {data === undefined && !loadFailed ? (
        <p className="state" aria-busy="true">
          {t('management.common.loading')}
        </p>
      ) : null}

      {data !== undefined ? (
        <p className="customers-total" role="status">
          {t('management.customers.total', { count: data.total })}
        </p>
      ) : null}

      {showRows ? (
        <CustomersTable
          rows={rows}
          onEdit={(customer) => open({ kind: 'edit', customer })}
          onAction={(customer, action) => open({ kind: 'action', customer, action })}
        />
      ) : null}

      {showEmpty ? <p className="state">{t('management.customers.empty')}</p> : null}

      {pagination !== null && pagination.showControls ? (
        <nav className="pagination" aria-label={t('management.common.paginationLabel')}>
          <span className="pagination__info">
            {t('management.common.range', { first: pagination.first, last: pagination.last, total: pagination.total })}
          </span>
          <button
            type="button"
            className="button button--small"
            disabled={!pagination.hasPrevious}
            onClick={() => go(withPage(state, state.page - 1))}
          >
            {t('management.common.previous')}
          </button>
          <button
            type="button"
            className="button button--small"
            disabled={!pagination.hasNext}
            onClick={() => go(withPage(state, state.page + 1))}
          >
            {t('management.common.next')}
          </button>
        </nav>
      ) : null}

      {dialog?.kind === 'create' || dialog?.kind === 'edit' ? (
        <CustomerForm
          key={dialog.kind === 'edit' ? dialog.customer.id : 'new'}
          {...(dialog.kind === 'edit' ? { customer: dialog.customer } : {})}
          onSaved={(_saved, mode) => {
            setNotice(mode === 'created' ? 'management.customers.form.created' : 'management.customers.form.updated');
            setDialog(null);
          }}
          onCancel={() => setDialog(null)}
        />
      ) : null}

      {dialog?.kind === 'action' ? (
        <CustomerConfirmDialog
          key={`${dialog.customer.id}:${dialog.action}`}
          action={dialog.action}
          customer={dialog.customer}
          onDone={(action) => {
            setNotice(actionTexts(action).doneKey);
            setDialog(null);
          }}
          onDismiss={() => setDialog(null)}
        />
      ) : null}
    </section>
  );
}
