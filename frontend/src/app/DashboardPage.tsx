/**
 * The console landing screen — the administrator home dashboard (Requirement 18.5, 18.6).
 *
 * For the current month it shows the headline figures the requirement names (Requirement 18.5): the
 * number of active employees and active sites, the month's total work hours, billing, employee cost
 * and gross profit. Below them sits the attention section (Requirement 18.6): counts of employees
 * without a check-out, without a check-in, and days missing entirely, each of which links to the
 * missing-report list filtered to that kind.
 *
 * The figures are billing and profit, so the dashboard endpoint is finance-only (administrators and
 * accounting); a site manager also lands here (the console's index route) but may not read the
 * finance figures, so for them the screen keeps the greeting and the end-to-end health panel rather
 * than showing a permission error. The server enforces the guard regardless — this only decides what
 * to ask for.
 *
 * Every string is a translation key; money goes through `formatCurrency`, minutes through
 * `formatDuration`, and the layout uses logical properties, so Hebrew renders right-to-left unchanged.
 */

import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';

import { readDashboard, reportKeys } from '@/api/reports';
import type { DashboardReport } from '@/api/types';
import { useAuth } from '@/auth/AuthProvider';
import {
  attentionItems,
  hasAttention,
  monthEnd,
  monthStart,
  parseAmount,
  toMonthValue,
} from '@/app/dashboard/dashboardView';
import { SystemStatus } from '@/components/SystemStatus';
import { ErrorState, LoadingState } from '@/components/management/QueryState';
import { formatCurrency, formatDuration } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';
import type { Language } from '@/i18n';

export function DashboardPage() {
  const { t } = useTranslation();
  const { user } = useAuth();

  // Billing and profit are finance figures; only administrators and accounting may read the dashboard.
  // A site manager on the console index sees the greeting and health panel instead of a 403.
  const canReadFinance = user?.role === 'admin' || user?.role === 'accounting';

  return (
    <section className="page-body">
      <h1>{t('dashboard.greeting', { name: user?.username ?? '' })}</h1>
      <p className="subtitle">{t('dashboard.subtitle')}</p>
      {canReadFinance ? <DashboardFigures /> : null}
      <SystemStatus />
    </section>
  );
}

/** The current-month figures and the attention section, for a finance caller (Requirement 18.5, 18.6). */
function DashboardFigures() {
  const { t } = useTranslation();
  const language = useLanguage();

  const query = useQuery({
    queryKey: reportKeys.dashboard(),
    queryFn: readDashboard,
  });

  if (query.isPending) {
    return <LoadingState />;
  }
  if (query.isError || !query.data) {
    return <ErrorState />;
  }

  return <DashboardBody report={query.data} language={language} monthLabel={t('dashboard.forMonth')} />;
}

function DashboardBody({
  report,
  language,
  monthLabel,
}: {
  report: DashboardReport;
  language: Language;
  monthLabel: string;
}) {
  const { t } = useTranslation();
  const period = { year: report.year, month: report.month };
  const monthValue = toMonthValue(period);

  return (
    <>
      <h2 className="section-title">
        {monthLabel} {monthValue}
      </h2>
      <div className="stat-grid">
        <Stat label={t('dashboard.activeEmployees')} value={String(report.active_employees)} />
        <Stat label={t('dashboard.activeSites')} value={String(report.active_sites)} />
        <Stat label={t('dashboard.totalHours')} value={formatDuration(report.total_minutes)} />
        <Stat
          label={t('dashboard.billing')}
          value={formatCurrency(language, parseAmount(report.total_billing))}
        />
        <Stat
          label={t('dashboard.employeeCost')}
          value={formatCurrency(language, parseAmount(report.total_cost))}
        />
        <Stat
          label={t('dashboard.grossProfit')}
          value={formatCurrency(language, parseAmount(report.total_profit))}
        />
      </div>

      <h2 className="section-title">{t('dashboard.attention.title')}</h2>
      {hasAttention(report.attention) ? (
        <ul className="attention-list">
          {attentionItems(report.attention).map((item) => (
            <li key={item.kind}>
              <Link
                className={`attention-item${item.count > 0 ? ' attention-item--active' : ''}`}
                to={{
                  pathname: '/reports',
                  search: new URLSearchParams({
                    view: 'missing',
                    kind: item.kind,
                    date_from: monthStart(period),
                    date_to: monthEnd(period),
                  }).toString(),
                }}
              >
                <span>{t(item.labelKey)}</span>
                <span className="attention-item__count numeric">{item.count}</span>
              </Link>
            </li>
          ))}
        </ul>
      ) : (
        <p className="state" role="status">
          {t('dashboard.attention.allClear')}
        </p>
      )}
    </>
  );
}

/** One headline figure: a label above a large value. */
function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat-card">
      <p className="stat-card__label">{label}</p>
      <p className="stat-card__value numeric">{value}</p>
    </div>
  );
}
