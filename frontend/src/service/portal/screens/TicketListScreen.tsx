// The customer's ticket list (task 23.2; R12.1, R12.5-R12.10, R23.5, R23.14, R23.15).
//
//   TicketListScreen  container: page from the URL, the query, polling, announcements, redirects.
//   TicketListView    presentational: props in, markup out. No query, no router state, no timers.
//
// The 30-second reload only updates query data. Row keys are ticket numbers, no element changes key
// between loads, the loading text shows only while nothing has been loaded, and nothing here calls
// `focus()` or `scrollTo()` after a reload, so keyboard focus, scroll and typed values stay put (R23.15).
// Focus moves only when the customer changes page: to the heading, so a screen reader starts at the top.
import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState, type Ref } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { listTickets, ticketKeys } from '../../api/tickets';
import { useAnnouncer } from '../Announcer';
import { PageHeading } from '../PageHeading';
import { directionOf, getLanguage, usePortalTranslation } from '../portalI18n';
import {
  APARTMENT_LABEL_KEY,
  TICKET_LIST_QUERY_OPTIONS,
  announcementTexts,
  apartmentText,
  categoryName,
  formatCreatedDate,
  listViewState,
  pageSearch,
  pastTheEndPage,
  paginationState,
  parsePageParam,
  pollAnnouncements,
  pollInterval,
  scheduleAnnouncements,
  shouldFallBackToFirstPage,
  statusTextKey,
  ticketPath,
  urgentMarkerKey,
  type ListViewState,
  type PaginationState,
  type StatusSnapshot,
} from './ticketListModel';
import './ticketList.css';

// --- presentational --------------------------------------------------------------------------------

export interface TicketListViewProps {
  readonly state: ListViewState;
  readonly pagination: PaginationState;
  /** The page now shown after the address named a page that is not available; null when there was no fallback. */
  readonly fallbackPage: number | null;
  readonly onRetry: () => void;
  readonly onRefresh: () => void;
  /** Receives the `<h1>`, which takes focus when the customer changes page. */
  readonly headingRef: Ref<HTMLHeadingElement> | null;
}

/** The urgent marker: an icon that assistive technology skips, and the text. */
function UrgentMarker({ text }: { text: string }) {
  return (
    <span className="portal-urgent">
      <svg className="portal-urgent-icon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
        <path d="M12 2 1 21h22L12 2Zm1 14h-2v-5h2v5Zm0 3h-2v-2h2v2Z" fill="currentColor" />
      </svg>
      <span>{text}</span>
    </span>
  );
}

export function TicketListView({ state, pagination, fallbackPage, onRetry, onRefresh, headingRef }: TicketListViewProps) {
  const { t, i18n } = usePortalTranslation();
  const language = getLanguage(i18n);

  return (
    <section className="portal-ticket-list" dir={directionOf(language)} lang={language}>
      <PageHeading pageKey="list" headingRef={headingRef}>
        {t('portal.list.title')}
      </PageHeading>

      <div className="portal-list-actions">
        <Link to="/new" className="portal-button">
          {t('portal.list.newTicket')}
        </Link>
        <button type="button" className="portal-button portal-button--quiet" onClick={onRefresh}>
          {t('portal.list.refresh')}
        </button>
      </div>

      {fallbackPage !== null && (
        <p className="portal-notice" role="status">
          {t('portal.list.pageFallback', { page: fallbackPage })}
        </p>
      )}

      {state.showError && (
        <div className="portal-notice portal-notice--error portal-list-error">
          <p>{t('portal.list.loadError')}</p>
          <button type="button" onClick={onRetry}>
            {t('portal.list.retry')}
          </button>
        </div>
      )}

      {state.content === 'loading' && <p>{t('portal.loading')}</p>}

      {state.content === 'empty' && (
        <div className="portal-list-empty">
          <p>{t('portal.list.empty')}</p>
          <Link to="/new" className="portal-button">
            {t('portal.list.emptyAction')}
          </Link>
        </div>
      )}

      {state.content === 'rows' && (
        <table className="portal-ticket-table" role="table">
          <caption className="portal-visually-hidden">{t('portal.list.tableLabel')}</caption>
          <thead role="rowgroup">
            <tr role="row">
              <th scope="col" role="columnheader">
                {t('portal.list.number')}
              </th>
              <th scope="col" role="columnheader">
                {t('portal.list.category')}
              </th>
              <th scope="col" role="columnheader">
                {t('portal.list.created')}
              </th>
              <th scope="col" role="columnheader">
                {t(APARTMENT_LABEL_KEY)}
              </th>
              <th scope="col" role="columnheader">
                {t('portal.list.status')}
              </th>
            </tr>
          </thead>
          <tbody role="rowgroup">
            {state.rows.map((row) => {
              const urgentKey = urgentMarkerKey(row);
              return (
                <tr key={row.ticket_number} role="row">
                  <th scope="row" role="rowheader" data-label={t('portal.list.number')}>
                    <Link
                      to={ticketPath(row.ticket_number)}
                      className="portal-ticket-link"
                      aria-label={t('portal.list.open', { number: row.ticket_number })}
                    >
                      <bdi dir="ltr">{row.ticket_number}</bdi>
                    </Link>
                  </th>
                  <td role="cell" data-label={t('portal.list.category')}>
                    {categoryName(row.category, language)}
                  </td>
                  <td role="cell" data-label={t('portal.list.created')}>
                    <bdi dir="ltr">{formatCreatedDate(row.created_date)}</bdi>
                  </td>
                  <td role="cell" data-label={t(APARTMENT_LABEL_KEY)}>
                    <bdi>{apartmentText(row)}</bdi>
                  </td>
                  <td role="cell" data-label={t('portal.list.status')}>
                    <span className={`portal-status portal-status--${row.status}`}>{t(statusTextKey(row.status))}</span>
                    {urgentKey !== null && <UrgentMarker text={t(urgentKey)} />}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {pagination.showControls && (
        <nav className="portal-pagination" aria-label={t('portal.list.pagination.label')}>
          {pagination.previousPage !== null && (
            <Link
              to={{ search: pageSearch(pagination.previousPage) }}
              className="portal-button portal-button--quiet"
              rel="prev"
            >
              {t('portal.list.pagination.previous')}
            </Link>
          )}
          <p className="portal-pagination-position">
            {t('portal.list.pagination.page', { page: pagination.page, pages: pagination.pages })}
          </p>
          {pagination.nextPage !== null && (
            <Link
              to={{ search: pageSearch(pagination.nextPage) }}
              className="portal-button portal-button--quiet"
              rel="next"
            >
              {t('portal.list.pagination.next')}
            </Link>
          )}
        </nav>
      )}
    </section>
  );
}

// --- container -------------------------------------------------------------------------------------

export function TicketListScreen() {
  const { t } = usePortalTranslation();
  const { announce, alert } = useAnnouncer();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const parsed = parsePageParam(searchParams.get('page'));
  const page = parsed.page;

  // A completed load (success or failure) restarts the 30-second timer, so the next reload comes 30
  // seconds after the previous load ended, also after a manual refresh (R12.5, R12.10).
  const [completedLoads, setCompletedLoads] = useState(0);

  const query = useQuery({
    queryKey: ticketKeys.list(page),
    queryFn: ({ signal }) => listTickets(page, signal),
    ...TICKET_LIST_QUERY_OPTIONS,
    refetchInterval: pollInterval(completedLoads),
  });
  const { data, isError, isPlaceholderData, error, dataUpdatedAt, errorUpdatedAt } = query;

  useEffect(() => {
    if (dataUpdatedAt > 0 || errorUpdatedAt > 0) setCompletedLoads((count) => count + 1);
  }, [dataUpdatedAt, errorUpdatedAt]);

  // --- a page that is not available: say so and show another page -----------------------------------
  const [fallback, setFallback] = useState<{ shownOn: number } | null>(null);

  useEffect(() => {
    if (!parsed.valid) {
      setFallback({ shownOn: 1 });
      navigate({ search: pageSearch(1) }, { replace: true });
    }
  }, [parsed.valid, navigate]);

  useEffect(() => {
    if (shouldFallBackToFirstPage(error, page)) {
      setFallback({ shownOn: 1 });
      navigate({ search: pageSearch(1) }, { replace: true });
    }
  }, [error, page, navigate]);

  const loadedTotal = !isPlaceholderData && data !== undefined ? data.total : null;
  const lastPage = loadedTotal === null ? null : pastTheEndPage(page, loadedTotal);
  useEffect(() => {
    if (lastPage !== null) {
      setFallback({ shownOn: lastPage });
      navigate({ search: pageSearch(lastPage) }, { replace: true });
    }
  }, [lastPage, navigate]);

  // The notice stays while the page it explains is shown and goes when the customer moves on.
  useEffect(() => {
    setFallback((current) => (current !== null && current.shownOn !== page ? null : current));
  }, [page]);

  // --- focus moves only when the customer changes page ------------------------------------------------
  const headingRef = useRef<HTMLHeadingElement>(null);
  const previousPage = useRef(page);
  useEffect(() => {
    if (previousPage.current !== page) {
      previousPage.current = page;
      headingRef.current?.focus();
    }
  }, [page]);

  // --- announcements of changed statuses (R23.15) -----------------------------------------------------
  const snapshot = useRef<StatusSnapshot | null>(null);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const latest = useRef({ t, announce, alert });
  useEffect(() => {
    latest.current = { t, announce, alert };
  });

  useEffect(() => {
    if (data === undefined || isPlaceholderData) return;
    const result = pollAnnouncements(snapshot.current, page, data.items);
    snapshot.current = result.snapshot;
    const texts = announcementTexts(result.announcements, (key, values) =>
      values === undefined ? latest.current.t(key) : latest.current.t(key, values),
    );
    for (const item of scheduleAnnouncements(texts)) {
      if (item.delayMs === 0) {
        latest.current.announce(item.text);
      } else {
        timers.current.push(setTimeout(() => latest.current.announce(item.text), item.delayMs));
      }
    }
  }, [data, isPlaceholderData, page]);

  useEffect(
    () => () => {
      for (const timer of timers.current) clearTimeout(timer);
    },
    [],
  );

  // A failed load is announced once when it appears, without moving focus (R23.14).
  useEffect(() => {
    if (isError) latest.current.alert(latest.current.t('portal.list.loadError'));
  }, [isError]);

  const state = listViewState({ data, isError });
  const pagination = paginationState(page, data?.total ?? 0);

  return (
    <TicketListView
      state={state}
      pagination={pagination}
      fallbackPage={fallback !== null && fallback.shownOn === page ? page : null}
      onRetry={() => {
        void query.refetch({ cancelRefetch: false });
      }}
      onRefresh={() => {
        void query.refetch({ cancelRefetch: false });
      }}
      headingRef={headingRef}
    />
  );
}
