/**
 * The billing screen (Requirement 17.3–17.7).
 *
 * Billing is billing data, so the whole screen is for administrators and accounting only: the server
 * puts every billing endpoint behind a finance-role guard (Requirement 17.7), and the console hides
 * the nav item from the other roles, so a site manager or an employee never arrives here. This screen
 * reads what the guarded endpoints return and does no redaction of its own.
 *
 * It shows, for a chosen month, the period's billing: a per-site table (each site's billable hours,
 * billing, and — once a calculation has run — cost and profit) and a per-client summary that sums the
 * sites (Requirement 17.3, 17.4). Selecting a client opens a drill-down listing that client's sites
 * (Requirement 17.3). Above both, the excluded-hours notice states the count and hours of unapproved
 * entries left out, so a low billing figure is never mistaken for a low month (Requirement 17.6).
 *
 * A recalculate action recomputes the month from its Approved or Locked entries and returns the live
 * cost and profit; a plain read (`GET /api/billing`) reports the billed amounts with the cost columns
 * empty, because cost and profit are computed at calculation time (Requirement 17.4). The screen
 * therefore shows the profit columns only after a calculation, and otherwise invites one, rather than
 * dressing an uncomputed cost up as ₪0.00.
 *
 * Every string is a translation key; money goes through `formatCurrency`, minutes through
 * `formatDuration`, and the layout uses logical properties, so Hebrew renders right-to-left unchanged.
 */

import { useMutation, useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { billingKeys, calculateBilling, readBilling } from '@/api/billing';
import type { BillingSummary, ClientBilling, SiteBilling } from '@/api/types';
import { ClientDetail } from '@/app/billing/ClientDetail';
import { ClientName, useClientName } from '@/app/billing/ClientName';
import { SiteName } from '@/app/billing/SiteName';
import { hasExcluded, hasProfit, parseAmount, siteMinutes } from '@/app/billing/billingView';
import { Drawer } from '@/components/management/Drawer';
import { EmptyState, ErrorState, LoadingState } from '@/components/management/QueryState';
import { PayrollStatusPill } from '@/components/management/StatusPill';
import { toError } from '@/lib/apiError';
import { formatCurrency, formatDuration } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';
import type { Language } from '@/i18n';

/** An em-dash placeholder, held in a value so the no-literal-strings lint rule is satisfied. */
const DASH = '—';

/** The current calendar year/month, the sensible default period to read. */
const now = () => {
  const date = new Date();
  return { year: date.getFullYear(), month: date.getMonth() + 1 };
};

const toMonthValue = (year: number, month: number): string =>
  `${year}-${String(month).padStart(2, '0')}`;

const parseMonthValue = (value: string): { year: number; month: number } | null => {
  const match = /^(\d{4})-(\d{2})$/.exec(value);
  if (!match) {
    return null;
  }
  return { year: Number(match[1]), month: Number(match[2]) };
};

export function BillingPage() {
  const { t } = useTranslation();
  const language = useLanguage();

  const initial = now();
  const [monthValue, setMonthValue] = useState(toMonthValue(initial.year, initial.month));
  const [viewingClient, setViewingClient] = useState<string | null>(null);
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [errorParams, setErrorParams] = useState<Record<string, string>>({});

  // The freshly calculated summary, when one has been run this session. It carries the live cost and
  // profit the stored read leaves empty (Requirement 17.4); until then the screen shows the read.
  const [calculated, setCalculated] = useState<BillingSummary | null>(null);

  const period = useMemo(() => parseMonthValue(monthValue), [monthValue]);
  const nameForClient = useClientName();

  const query = useQuery({
    queryKey: billingKeys.summary({ year: period?.year ?? 0, month: period?.month ?? 0 }),
    queryFn: () => readBilling({ year: period!.year, month: period!.month }),
    enabled: period !== null,
  });

  const recalc = useMutation({
    mutationFn: () => calculateBilling({ year: period!.year, month: period!.month }),
    onSuccess: (summary) => {
      setErrorKey(null);
      setErrorParams({});
      setCalculated(summary);
    },
    onError: (error) => {
      const translated = toError(error);
      setErrorKey(translated.key);
      setErrorParams(translated.params);
    },
  });

  const onMonthChange = (value: string) => {
    setMonthValue(value);
    // A new period's calculated result does not carry over — the read below refetches, and a fresh
    // calculation is a click away.
    setCalculated(null);
    setErrorKey(null);
    setViewingClient(null);
  };

  // Show the freshly calculated summary when one matches the chosen period; otherwise the stored read.
  const summary: BillingSummary | undefined =
    calculated && period && calculated.year === period.year && calculated.month === period.month
      ? calculated
      : query.data;

  const withProfit = summary ? hasProfit(summary) : false;
  const viewingSummary = summary ?? null;

  return (
    <section className="page-body">
      <div className="page-header">
        <h1>{t('nav.billing')}</h1>
        <div className="page-header__actions">
          <button
            type="button"
            className="button button--primary"
            disabled={period === null || recalc.isPending}
            onClick={() => {
              setErrorKey(null);
              recalc.mutate();
            }}
          >
            {recalc.isPending ? t('common.saving') : t('billing.recalculate')}
          </button>
        </div>
      </div>
      <p className="subtitle">{t('billing.subtitle')}</p>

      <div className="toolbar">
        <label className="field field--inline">
          <span className="field__label">{t('billing.month')}</span>
          <input
            className="input"
            type="month"
            value={monthValue}
            onChange={(event) => onMonthChange(event.target.value)}
          />
        </label>
      </div>

      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey, errorParams)}
        </p>
      ) : null}

      {/* The excluded-hours notice: unapproved entries left out of the figures (Requirement 17.6). */}
      {summary && hasExcluded(summary) ? (
        <p className="feedback feedback--warn" role="status">
          {t('billing.excludedNotice', {
            count: summary.excluded_entry_count,
            hours: formatDuration(summary.excluded_minutes),
          })}
        </p>
      ) : null}

      {/* A stored read has no cost or profit yet; invite a calculation rather than showing empty columns. */}
      {summary && !withProfit && summary.sites.length > 0 ? (
        <p className="state" role="status">
          {t('billing.profitAfterCalculate')}
        </p>
      ) : null}

      {query.isPending && !summary ? (
        <LoadingState />
      ) : query.isError && !summary ? (
        <ErrorState />
      ) : !summary || summary.sites.length === 0 ? (
        <EmptyState messageKey="billing.empty" />
      ) : (
        <>
          {/* Per-client summary, each row a drill-down into that client's sites (Requirement 17.3). */}
          <h2 className="section-title">{t('billing.byClient')}</h2>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('billing.client')}</th>
                  <th className="numeric">{t('billing.billing')}</th>
                  {withProfit ? <th className="numeric">{t('billing.cost')}</th> : null}
                  {withProfit ? <th className="numeric">{t('billing.profit')}</th> : null}
                  <th>{t('common.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {summary.clients.map((client) => (
                  <ClientRow
                    key={client.client_id}
                    client={client}
                    name={nameForClient(client.client_id)}
                    language={language}
                    withProfit={withProfit}
                    onView={() => setViewingClient(client.client_id)}
                  />
                ))}
              </tbody>
              <tfoot>
                <tr className="detail__row--total">
                  <td>{t('billing.total')}</td>
                  <td className="numeric">
                    {formatCurrency(language, parseAmount(summary.total_billing))}
                  </td>
                  {withProfit ? (
                    <td className="numeric">
                      {formatCurrency(language, parseAmount(summary.total_cost))}
                    </td>
                  ) : null}
                  {withProfit ? (
                    <td className="numeric">
                      {formatCurrency(language, parseAmount(summary.total_profit))}
                    </td>
                  ) : null}
                  <td />
                </tr>
              </tfoot>
            </table>
          </div>

          {/* Per-site detail across the whole period (Requirement 17.3, 17.4). */}
          <h2 className="section-title">{t('billing.bySite')}</h2>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('billing.site')}</th>
                  <th>{t('billing.client')}</th>
                  <th className="numeric">{t('billing.hours')}</th>
                  <th className="numeric">{t('billing.billing')}</th>
                  {withProfit ? <th className="numeric">{t('billing.cost')}</th> : null}
                  {withProfit ? <th className="numeric">{t('billing.profit')}</th> : null}
                  <th>{t('common.status')}</th>
                </tr>
              </thead>
              <tbody>
                {summary.sites.map((site) => (
                  <SiteRow
                    key={site.site_id}
                    site={site}
                    language={language}
                    withProfit={withProfit}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {viewingClient && viewingSummary ? (
        <Drawer
          title={t('billing.clientTitle', { name: nameForClient(viewingClient) })}
          onClose={() => setViewingClient(null)}
        >
          <ClientDetail summary={viewingSummary} clientId={viewingClient} language={language} />
        </Drawer>
      ) : null}
    </section>
  );
}

/** One client's summary row: its billing, cost and profit, with a drill-down into its sites. */
function ClientRow({
  client,
  name,
  language,
  withProfit,
  onView,
}: {
  client: ClientBilling;
  name: string;
  language: Language;
  withProfit: boolean;
  onView: () => void;
}) {
  const { t } = useTranslation();
  return (
    <tr>
      <td>
        <button type="button" className="link-button" onClick={onView}>
          {name}
        </button>
      </td>
      <td className="numeric">{formatCurrency(language, parseAmount(client.billing))}</td>
      {withProfit ? (
        <td className="numeric">{formatCurrency(language, parseAmount(client.cost))}</td>
      ) : null}
      {withProfit ? (
        <td className="numeric">{formatCurrency(language, parseAmount(client.profit))}</td>
      ) : null}
      <td>
        <button type="button" className="button button--small" onClick={onView}>
          {t('billing.view')}
        </button>
      </td>
    </tr>
  );
}

/** One site's row: its client, billable hours, billing, and (after a calculation) cost and profit. */
function SiteRow({
  site,
  language,
  withProfit,
}: {
  site: SiteBilling;
  language: Language;
  withProfit: boolean;
}) {
  return (
    <tr>
      <td>
        <SiteName siteId={site.site_id} />
      </td>
      <td>
        <ClientName clientId={site.client_id} />
      </td>
      <td className="numeric">{formatDuration(siteMinutes(site))}</td>
      <td className="numeric">{formatCurrency(language, parseAmount(site.billing))}</td>
      {withProfit ? (
        <td className="numeric">
          {site.cost === null ? DASH : formatCurrency(language, parseAmount(site.cost))}
        </td>
      ) : null}
      {withProfit ? (
        <td className="numeric">
          {site.profit === null ? DASH : formatCurrency(language, parseAmount(site.profit))}
        </td>
      ) : null}
      <td>
        <PayrollStatusPill status={site.status} />
      </td>
    </tr>
  );
}
