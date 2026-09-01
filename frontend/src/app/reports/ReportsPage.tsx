/**
 * The reports screen (Requirement 18.4, 18.6).
 *
 * Two views over the month's computed figures, both finance data (the server guards them for
 * administrators and accounting; the console hides the nav item from the employee role):
 *
 * - the **profitability dashboard** (Requirement 18.4) — total billing, employee cost and gross
 *   profit for the selected filters, with the full filter set the requirement names: month, employee,
 *   site, client and project. It reads `GET /api/reports/profitability`, which sums over the sites the
 *   filters admit, so a filtered figure reconciles with the by-site report over the same filters.
 * - the **missing-report list** (Requirement 14.3, 18.6) — the days an employee was expected at a site
 *   but a report is missing, of the three kinds. The administrator dashboard's attention counts link
 *   here with `?view=missing&kind=…` and the month's date range, so a count and the list it opens read
 *   alike.
 *
 * The active view and its filters live in the URL query string, so a dashboard link lands on the right
 * view already filtered and the page is shareable. Every string is a translation key; money goes
 * through `formatCurrency`, minutes through `formatDuration`, dates through `formatDate`, and the
 * layout uses logical properties, so Hebrew renders right-to-left unchanged.
 */

import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';

import { clientKeys, listClients } from '@/api/clients';
import { employeeKeys, listEmployees } from '@/api/employees';
import {
  readMissingReports,
  readProfitability,
  reportKeys,
  type MissingReportsParams,
  type ProfitabilityParams,
} from '@/api/reports';
import { listSites, siteKeys } from '@/api/sites';
import type { MissingReportKind } from '@/api/types';
import { useAuth } from '@/auth/AuthProvider';
import { SiteName } from '@/app/billing/SiteName';
import {
  currentPeriod,
  monthEnd,
  monthStart,
  parseAmount,
  parseMonthValue,
  toMonthValue,
} from '@/app/dashboard/dashboardView';
import { EmptyState, ErrorState, LoadingState } from '@/components/management/QueryState';
import { formatCurrency, formatDate } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';
import type { Language } from '@/i18n';

const OPTION_PAGE = 200;
const KINDS: MissingReportKind[] = ['missing_checkout', 'missing_checkin', 'both_missing'];

export function ReportsPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();

  // The profitability dashboard is billing and profit, so only administrators and accounting may read
  // it; a site manager reaches this screen for the missing-report list, which the server scopes to
  // their sites. Hiding the profitability tab keeps a manager off a view the server would refuse.
  const canReadProfitability = user?.role === 'admin' || user?.role === 'accounting';
  const requested = params.get('view') === 'missing' ? 'missing' : 'profitability';
  const view = canReadProfitability ? requested : 'missing';

  const setView = (next: 'profitability' | 'missing') => {
    const nextParams = new URLSearchParams(params);
    nextParams.set('view', next);
    setParams(nextParams);
  };

  return (
    <section className="page-body">
      <div className="page-header">
        <h1>{t('nav.reports')}</h1>
      </div>
      <p className="subtitle">{t('reports.subtitle')}</p>

      {canReadProfitability ? (
        <div className="toolbar" role="tablist" aria-label={t('reports.views')}>
          <button
            type="button"
            role="tab"
            aria-selected={view === 'profitability'}
            className={`button${view === 'profitability' ? ' button--primary' : ''}`}
            onClick={() => setView('profitability')}
          >
            {t('reports.profitability.tab')}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === 'missing'}
            className={`button${view === 'missing' ? ' button--primary' : ''}`}
            onClick={() => setView('missing')}
          >
            {t('reports.missing.tab')}
          </button>
        </div>
      ) : null}

      {view === 'profitability' ? (
        <ProfitabilityView params={params} setParams={setParams} />
      ) : (
        <MissingReportsView params={params} setParams={setParams} />
      )}
    </section>
  );
}

interface ViewProps {
  params: URLSearchParams;
  setParams: (next: URLSearchParams) => void;
}

// --------------------------------------------------------------------------- profitability (18.4)

function ProfitabilityView({ params, setParams }: ViewProps) {
  const { t } = useTranslation();
  const language = useLanguage();

  const fallback = toMonthValue(currentPeriod());
  const monthValue = params.get('month') ?? fallback;
  const period = useMemo(() => parseMonthValue(monthValue), [monthValue]);
  const employeeId = params.get('employee_id') ?? '';
  const siteId = params.get('site_id') ?? '';
  const clientId = params.get('client_id') ?? '';
  const project = params.get('project') ?? '';

  const set = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    next.set('view', 'profitability');
    if (value) {
      next.set(key, value);
    } else {
      next.delete(key);
    }
    setParams(next);
  };

  const queryParams: ProfitabilityParams = {
    year: period?.year ?? 0,
    month: period?.month ?? 0,
    employeeId: employeeId || null,
    siteId: siteId || null,
    clientId: clientId || null,
    project: project || null,
  };

  const query = useQuery({
    queryKey: reportKeys.profitability(queryParams),
    queryFn: () => readProfitability(queryParams),
    enabled: period !== null,
  });

  const employees = useQuery({
    queryKey: employeeKeys.list({ status: 'active', limit: OPTION_PAGE, offset: 0 }),
    queryFn: () => listEmployees({ status: 'active', limit: OPTION_PAGE, offset: 0 }),
  });
  const sites = useQuery({
    queryKey: siteKeys.list({ status: null, limit: OPTION_PAGE, offset: 0 }),
    queryFn: () => listSites({ status: null, limit: OPTION_PAGE, offset: 0 }),
  });
  const clients = useQuery({
    queryKey: clientKeys.list({ includeArchived: true, limit: OPTION_PAGE, offset: 0 }),
    queryFn: () => listClients({ includeArchived: true, limit: OPTION_PAGE, offset: 0 }),
  });

  return (
    <>
      <div className="toolbar hours-filters">
        <label className="field field--inline">
          <span className="field__label">{t('reports.month')}</span>
          <input
            className="input"
            type="month"
            value={monthValue}
            onChange={(event) => set('month', event.target.value)}
          />
        </label>

        <select
          className="select"
          aria-label={t('reports.filterEmployee')}
          value={employeeId}
          onChange={(event) => set('employee_id', event.target.value)}
        >
          <option value="">{t('reports.allEmployees')}</option>
          {(employees.data?.items ?? []).map((employee) => (
            <option key={employee.id} value={employee.id}>
              {employeeLabel(language, employee.full_name, employee.full_name_en)}
            </option>
          ))}
        </select>

        <select
          className="select"
          aria-label={t('reports.filterSite')}
          value={siteId}
          onChange={(event) => set('site_id', event.target.value)}
        >
          <option value="">{t('reports.allSites')}</option>
          {(sites.data?.items ?? []).map((site) => (
            <option key={site.id} value={site.id}>
              {site.name}
            </option>
          ))}
        </select>

        <select
          className="select"
          aria-label={t('reports.filterClient')}
          value={clientId}
          onChange={(event) => set('client_id', event.target.value)}
        >
          <option value="">{t('reports.allClients')}</option>
          {(clients.data?.items ?? []).map((client) => (
            <option key={client.id} value={client.id}>
              {client.name}
            </option>
          ))}
        </select>

        <label className="field field--inline">
          <span className="field__label">{t('reports.project')}</span>
          <input
            className="input"
            type="text"
            value={project}
            placeholder={t('reports.allProjects')}
            onChange={(event) => set('project', event.target.value)}
          />
        </label>
      </div>

      {query.isPending ? (
        <LoadingState />
      ) : query.isError || !query.data ? (
        <ErrorState />
      ) : (
        <div className="stat-grid">
          <div className="stat-card">
            <p className="stat-card__label">{t('reports.profitability.billing')}</p>
            <p className="stat-card__value numeric">
              {formatCurrency(language, parseAmount(query.data.total_billing))}
            </p>
          </div>
          <div className="stat-card">
            <p className="stat-card__label">{t('reports.profitability.cost')}</p>
            <p className="stat-card__value numeric">
              {formatCurrency(language, parseAmount(query.data.total_cost))}
            </p>
          </div>
          <div className="stat-card">
            <p className="stat-card__label">{t('reports.profitability.profit')}</p>
            <p className="stat-card__value numeric">
              {formatCurrency(language, parseAmount(query.data.total_profit))}
            </p>
          </div>
          <div className="stat-card">
            <p className="stat-card__label">{t('reports.profitability.siteCount')}</p>
            <p className="stat-card__value numeric">{query.data.site_count}</p>
          </div>
        </div>
      )}
    </>
  );
}

// --------------------------------------------------------------------------- missing reports (14.3, 18.6)

function MissingReportsView({ params, setParams }: ViewProps) {
  const { t } = useTranslation();
  const language = useLanguage();

  const period = currentPeriod();
  const dateFrom = params.get('date_from') ?? monthStart(period);
  const dateTo = params.get('date_to') ?? monthEnd(period);
  const kind = (params.get('kind') as MissingReportKind | null) ?? '';

  const set = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    next.set('view', 'missing');
    if (value) {
      next.set(key, value);
    } else {
      next.delete(key);
    }
    setParams(next);
  };

  const queryParams: MissingReportsParams = { dateFrom, dateTo };
  const query = useQuery({
    queryKey: reportKeys.missingReports(queryParams),
    queryFn: () => readMissingReports(queryParams),
    enabled: Boolean(dateFrom) && Boolean(dateTo),
  });

  // The kind filter is applied client-side: the endpoint returns every kind for the range, and the
  // dashboard's attention link narrows to one kind. Keeping it here means one request serves the
  // list and each filtered view without a round trip per kind.
  const findings = (query.data?.findings ?? []).filter((finding) =>
    kind ? finding.kind === kind : true,
  );

  return (
    <>
      <div className="toolbar hours-filters">
        <label className="field field--inline">
          <span className="field__label">{t('reports.dateFrom')}</span>
          <input
            className="input"
            type="date"
            value={dateFrom}
            onChange={(event) => set('date_from', event.target.value)}
          />
        </label>
        <label className="field field--inline">
          <span className="field__label">{t('reports.dateTo')}</span>
          <input
            className="input"
            type="date"
            value={dateTo}
            onChange={(event) => set('date_to', event.target.value)}
          />
        </label>
        <select
          className="select"
          aria-label={t('reports.missing.filterKind')}
          value={kind}
          onChange={(event) => set('kind', event.target.value)}
        >
          <option value="">{t('reports.missing.allKinds')}</option>
          {KINDS.map((value) => (
            <option key={value} value={value}>
              {t(`missingReportKind.${value}`)}
            </option>
          ))}
        </select>
      </div>

      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState />
      ) : findings.length === 0 ? (
        <EmptyState messageKey="reports.missing.empty" />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{t('reports.missing.employee')}</th>
                <th>{t('reports.missing.date')}</th>
                <th>{t('reports.missing.site')}</th>
                <th>{t('reports.missing.kind')}</th>
              </tr>
            </thead>
            <tbody>
              {findings.map((finding) => (
                <tr key={`${finding.employee_id}-${finding.work_date}-${finding.site_id}-${finding.kind}`}>
                  <td>{employeeLabel(language, finding.employee_name, finding.employee_name_en)}</td>
                  <td className="numeric">{formatDate(language, finding.work_date)}</td>
                  <td>
                    <SiteName siteId={finding.site_id} />
                  </td>
                  <td>
                    <span className="tag tag--anomaly">{t(`missingReportKind.${finding.kind}`)}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

/** An option/row label: the reader's-language name first, the other in parentheses to disambiguate. */
const employeeLabel = (language: Language, name: string, nameEn: string): string => {
  const primary = language === 'he' ? name : nameEn;
  const secondary = language === 'he' ? nameEn : name;
  return primary === secondary ? primary : `${primary} (${secondary})`;
};
