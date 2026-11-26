/**
 * The dashboard markup (task 24.2): props in, markup out. No query, no auth, no visibility, so a test can
 * render every state with `renderToStaticMarkup`. `DashboardPage` holds the query and passes the state.
 *
 * Every state is text: the "not current" label and the error message are sentences, and the three counts
 * are numbers under a visible name (R23.12). Each count links to the ticket list with its filters (R17.4).
 */
import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import {
  DASHBOARD_COUNTS,
  countValue,
  dashboardListTarget,
  type DashboardViewState,
} from './dashboardModel';
import { SERVICE_NAMESPACE } from './namespace';
import './dashboard.css';

export function DashboardView({ state }: { state: DashboardViewState }) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const idBase = useId();
  const { counts } = state;

  return (
    <section className="page-body dashboard" aria-labelledby={`${idBase}-title`}>
      <header className="page-header">
        <div>
          <h1 id={`${idBase}-title`}>{t('management.dashboard.title')}</h1>
          <p className="subtitle">{t('management.dashboard.subtitle')}</p>
        </div>
      </header>

      {state.numberWarning !== null && (
        <p className="dashboard-warning" role="status">
          {t('management.dashboard.numberWarning', { remaining: state.numberWarning })}
        </p>
      )}

      {state.showError && (
        <p className="form__error dashboard-error" role="alert">
          {t('management.dashboard.loadError')}
        </p>
      )}

      {state.content === 'loading' && <p role="status">{t('management.common.loading')}</p>}

      {counts !== null && (
        <>
          {state.stale && <p className="dashboard-stale">{t('management.dashboard.stale')}</p>}
          <ul className="stat-grid dashboard-counts">
            {DASHBOARD_COUNTS.map((entry) => {
              const labelId = `${idBase}-${entry.key}-label`;
              const valueId = `${idBase}-${entry.key}-value`;
              const linkId = `${idBase}-${entry.key}-link`;
              return (
                <li key={entry.key} className="stat-card dashboard-count">
                  <h2 id={labelId} className="dashboard-count__label">
                    {t(entry.labelKey)}
                  </h2>
                  <p id={valueId} className="dashboard-count__value">
                    <bdi dir="ltr">{countValue(counts, entry.key)}</bdi>
                  </p>
                  {entry.hintKey !== null && <p className="subtitle">{t(entry.hintKey)}</p>}
                  <Link
                    id={linkId}
                    className="button dashboard-count__link"
                    to={dashboardListTarget(entry.key)}
                    aria-labelledby={`${labelId} ${valueId} ${linkId}`}
                  >
                    {t('management.dashboard.showTickets')}
                  </Link>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}
