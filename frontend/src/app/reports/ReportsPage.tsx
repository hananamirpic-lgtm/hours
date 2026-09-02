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
  readEmployeeReport,
  readMissingReports,
  readProfitability,
  reportKeys,
  type EmployeeReportParams,
  type MissingReportsParams,
  type ProfitabilityParams,
} from '@/api/reports';
import { listSites, siteKeys } from '@/api/sites';
import type { EmployeeReport, MissingReportKind } from '@/api/types';
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
import {
  employeeReportFilename,
  employeeReportHeader,
  employeeReportName,
  employeeReportRowCells,
  employeeReportTotalsCells,
  toEmployeeReportCsv,
  type EmployeeReportLabels,
  type EmployeeReportRenderOptions,
} from '@/app/reports/employeeReportDownload';
import { EmptyState, ErrorState, LoadingState } from '@/components/management/QueryState';
import { triggerBrowserDownload } from '@/lib/download';
import { formatCurrency, formatDate, formatDuration } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';
import type { Language } from '@/i18n';

const OPTION_PAGE = 200;
const KINDS: MissingReportKind[] = ['missing_checkout', 'missing_checkin', 'both_missing'];

type ReportView = 'profitability' | 'by-employee' | 'missing';

export function ReportsPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();

  // The profitability dashboard is billing and profit, so only administrators and accounting may read
  // it; a site manager reaches this screen for the by-employee report and the missing-report list,
  // both of which the server scopes to their sites. The by-employee endpoint is open to all three
  // finance-adjacent roles (admin, accounting, site_manager), so its tab shows for each of them —
  // the server strips cost from a manager's payload, so a manager sees hours without wage. Employees
  // never reach the console. Each tab is hidden from a role the server would refuse.
  const canReadProfitability = user?.role === 'admin' || user?.role === 'accounting';
  const canReadByEmployee =
    user?.role === 'admin' || user?.role === 'accounting' || user?.role === 'site_manager';

  const availableViews = useMemo<ReportView[]>(() => {
    const views: ReportView[] = [];
    if (canReadProfitability) {
      views.push('profitability');
    }
    if (canReadByEmployee) {
      views.push('by-employee');
    }
    views.push('missing');
    return views;
  }, [canReadProfitability, canReadByEmployee]);

  const requested = params.get('view') as ReportView | null;
  const view: ReportView =
    requested && availableViews.includes(requested) ? requested : availableViews[0];

  const setView = (next: ReportView) => {
    const nextParams = new URLSearchParams(params);
    nextParams.set('view', next);
    setParams(nextParams);
  };

  const tabLabels: Record<ReportView, string> = {
    profitability: t('reports.profitability.tab'),
    'by-employee': t('reports.byEmployee.tab'),
    missing: t('reports.missing.tab'),
  };

  return (
    <section className="page-body">
      <div className="page-header">
        <h1>{t('nav.reports')}</h1>
      </div>
      <p className="subtitle">{t('reports.subtitle')}</p>

      {availableViews.length > 1 ? (
        <div className="toolbar" role="tablist" aria-label={t('reports.views')}>
          {availableViews.map((candidate) => (
            <button
              key={candidate}
              type="button"
              role="tab"
              aria-selected={view === candidate}
              className={`button${view === candidate ? ' button--primary' : ''}`}
              onClick={() => setView(candidate)}
            >
              {tabLabels[candidate]}
            </button>
          ))}
        </div>
      ) : null}

      {view === 'profitability' ? (
        <ProfitabilityView params={params} setParams={setParams} />
      ) : view === 'by-employee' ? (
        <ByEmployeeView params={params} setParams={setParams} />
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

// --------------------------------------------------------------------------- by employee (18.4, 18.7)

function ByEmployeeView({ params, setParams }: ViewProps) {
  const { t } = useTranslation();
  const language = useLanguage();

  const fallback = toMonthValue(currentPeriod());
  const monthValue = params.get('month') ?? fallback;
  const period = useMemo(() => parseMonthValue(monthValue), [monthValue]);
  const employeeId = params.get('employee_id') ?? '';

  const set = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    next.set('view', 'by-employee');
    if (value) {
      next.set(key, value);
    } else {
      next.delete(key);
    }
    setParams(next);
  };

  const queryParams: EmployeeReportParams = {
    year: period?.year ?? 0,
    month: period?.month ?? 0,
    employeeId: employeeId || null,
  };

  const query = useQuery({
    queryKey: reportKeys.byEmployee(queryParams),
    queryFn: () => readEmployeeReport(queryParams),
    enabled: period !== null,
  });

  const employees = useQuery({
    queryKey: employeeKeys.list({ status: 'active', limit: OPTION_PAGE, offset: 0 }),
    queryFn: () => listEmployees({ status: 'active', limit: OPTION_PAGE, offset: 0 }),
  });

  const report = query.data;

  // The one security-relevant switch: cost is present for a finance reader and null for a site
  // manager, whose payload the server strips of wage. When it is null the cost column is omitted from
  // the table and from every export, so a manager never sees or downloads a wage — not even a zero.
  const includeCost = report != null && report.total_cost !== null;

  const labels: EmployeeReportLabels = {
    employee: t('reports.byEmployee.employee'),
    regular: t('reports.byEmployee.regular'),
    overtime: t('reports.byEmployee.overtime'),
    shabbat: t('reports.byEmployee.shabbat'),
    holiday: t('reports.byEmployee.holiday'),
    total: t('reports.byEmployee.total'),
    cost: t('reports.byEmployee.cost'),
    totalsRow: t('reports.byEmployee.totalsRow'),
  };

  const renderOptions: EmployeeReportRenderOptions = { includeCost, language, labels };

  const printTitle = t('reports.byEmployee.printTitle', { period: monthValue });

  const downloadCsv = () => {
    if (!report) {
      return;
    }
    const csv = toEmployeeReportCsv(report, renderOptions);
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    triggerBrowserDownload(blob, employeeReportFilename(report, 'csv'));
  };

  const downloadPdf = () => {
    if (!report) {
      return;
    }
    printEmployeeReport(report, renderOptions, {
      title: printTitle,
      filtersLabel: t('reports.byEmployee.filtersPeriod', { period: monthValue }),
      dir: language === 'he' ? 'rtl' : 'ltr',
      lang: language,
    });
  };

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

        <div className="inline-actions">
          <button
            type="button"
            className="button button--small button--primary"
            disabled={!report || report.rows.length === 0}
            onClick={downloadCsv}
          >
            {t('reports.byEmployee.downloadCsv')}
          </button>
          <button
            type="button"
            className="button button--small"
            disabled={!report || report.rows.length === 0}
            onClick={downloadPdf}
          >
            {t('reports.byEmployee.downloadPdf')}
          </button>
        </div>
      </div>

      {query.isPending ? (
        <LoadingState />
      ) : query.isError || !report ? (
        <ErrorState />
      ) : report.rows.length === 0 ? (
        <EmptyState messageKey="reports.byEmployee.empty" />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{labels.employee}</th>
                <th className="numeric">{labels.regular}</th>
                <th className="numeric">{labels.overtime}</th>
                <th className="numeric">{labels.shabbat}</th>
                <th className="numeric">{labels.holiday}</th>
                <th className="numeric">{labels.total}</th>
                {includeCost ? <th className="numeric">{labels.cost}</th> : null}
              </tr>
            </thead>
            <tbody>
              {report.rows.map((row) => (
                <tr key={row.employee_id}>
                  <td>{employeeReportName(language, row)}</td>
                  <td className="numeric">{formatDuration(row.regular_minutes)}</td>
                  <td className="numeric">{formatDuration(row.overtime_minutes)}</td>
                  <td className="numeric">{formatDuration(row.shabbat_minutes)}</td>
                  <td className="numeric">{formatDuration(row.holiday_minutes)}</td>
                  <td className="numeric">{formatDuration(row.total_minutes)}</td>
                  {includeCost ? (
                    <td className="numeric">{formatCurrency(language, parseAmount(row.cost))}</td>
                  ) : null}
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <th>{labels.totalsRow}</th>
                <td className="numeric" />
                <td className="numeric" />
                <td className="numeric" />
                <td className="numeric" />
                <td className="numeric">{formatDuration(report.total_minutes)}</td>
                {includeCost ? (
                  <td className="numeric">
                    {formatCurrency(language, parseAmount(report.total_cost))}
                  </td>
                ) : null}
              </tr>
            </tfoot>
          </table>
        </div>
      )}
    </>
  );
}

interface PrintOptions {
  title: string;
  filtersLabel: string;
  dir: 'rtl' | 'ltr';
  lang: Language;
}

/**
 * Render the by-employee report into a self-contained, print-only document and open the browser's
 * print dialog (the reader picks "Save as PDF"). A new window carries only the report — a heading with
 * the period and filters (Requirement 18.7), the same table, and the totals row — so the print does
 * not depend on the app chrome. The document respects the reader's language direction, and the cost
 * column appears only when the payload carried it, so a site manager's print is wage-free. Values are
 * built by the same pure helpers the table and CSV use, so the three agree. No PDF library is
 * involved: the browser's own print-to-PDF does the conversion, which keeps the change dependency-free.
 */
function printEmployeeReport(
  report: EmployeeReport,
  options: EmployeeReportRenderOptions,
  print: PrintOptions,
): void {
  const win = window.open('', '_blank');
  if (!win) {
    return;
  }

  const escapeHtml = (value: string): string =>
    value
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');

  const headerCells = employeeReportHeader(options)
    .map((cell) => `<th>${escapeHtml(cell)}</th>`)
    .join('');
  const bodyRows = report.rows
    .map(
      (row) =>
        `<tr>${employeeReportRowCells(row, options)
          .map((cell) => `<td>${escapeHtml(cell)}</td>`)
          .join('')}</tr>`,
    )
    .join('');
  const totalsRow = `<tr class="totals">${employeeReportTotalsCells(report, options)
    .map((cell) => `<td>${escapeHtml(cell)}</td>`)
    .join('')}</tr>`;

  const doc = win.document;
  doc.open();
  doc.write(
    `<!doctype html><html lang="${print.lang}" dir="${print.dir}"><head><meta charset="utf-8">` +
      `<title>${escapeHtml(print.title)}</title><style>` +
      'body{font-family:system-ui,sans-serif;margin:24px;color:#111}' +
      'h1{font-size:18px;margin:0 0 4px}' +
      'p{margin:0 0 16px;color:#555;font-size:12px}' +
      'table{border-collapse:collapse;width:100%;font-size:12px}' +
      'th,td{border:1px solid #ccc;padding:6px 8px;text-align:start}' +
      'thead th{background:#f2f2f2}' +
      'tr.totals td{font-weight:700;background:#f9f9f9}' +
      '</style></head><body>' +
      `<h1>${escapeHtml(print.title)}</h1>` +
      `<p>${escapeHtml(print.filtersLabel)}</p>` +
      `<table><thead><tr>${headerCells}</tr></thead>` +
      `<tbody>${bodyRows}</tbody>` +
      `<tfoot>${totalsRow}</tfoot></table>` +
      '</body></html>',
  );
  doc.close();
  win.focus();
  win.print();
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
