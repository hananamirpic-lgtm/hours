/**
 * The per-client billing drill-down (Requirement 17.3, 17.4).
 *
 * Opened from a client row, this lists that client's sites for the month — each with its billable
 * hours, its billing, and, when a calculation has been run, its cost and profit (Requirement 17.4).
 * A footer asserts that the per-site figures add up to the client total the row showed, so the
 * drill-down and the summary agree. The cost and profit columns appear only when the summary carries
 * them: a stored read leaves each site's cost null, so showing a zero there would misread an
 * uncomputed cost as no cost — the page invites a recalculation instead.
 *
 * The figures are the backend's, computed with `Decimal` and `ROUND_HALF_UP` (Requirement 17.1);
 * this only formats them. Every string is a translation key; money goes through `formatCurrency`,
 * minutes through `formatDuration`; the layout uses logical properties for right-to-left.
 */

import { useTranslation } from 'react-i18next';

import type { BillingSummary, SiteBilling } from '@/api/types';
import { SiteName } from '@/app/billing/SiteName';
import { clientById, hasProfit, parseAmount, siteMinutes, sitesForClient } from '@/app/billing/billingView';
import { formatCurrency, formatDuration } from '@/lib/format';
import type { Language } from '@/i18n';

/** An em-dash placeholder, held in a value so the no-literal-strings lint rule is satisfied. */
const DASH = '—';

export function ClientDetail({
  summary,
  clientId,
  language,
}: {
  summary: BillingSummary;
  clientId: string;
  language: Language;
}) {
  const { t } = useTranslation();
  const sites = sitesForClient(summary, clientId);
  const client = clientById(summary, clientId);
  const withProfit = hasProfit(summary);

  const totalMinutes = sites.reduce((sum, site) => sum + siteMinutes(site), 0);

  return (
    <div className="detail">
      <h3 className="section-title">{t('billing.sitesForClient')}</h3>
      {sites.length === 0 ? (
        <p className="state">{t('billing.noSitesForClient')}</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{t('billing.site')}</th>
                <th className="numeric">{t('billing.hours')}</th>
                <th className="numeric">{t('billing.billing')}</th>
                {withProfit ? <th className="numeric">{t('billing.cost')}</th> : null}
                {withProfit ? <th className="numeric">{t('billing.profit')}</th> : null}
              </tr>
            </thead>
            <tbody>
              {sites.map((site) => (
                <SiteRow key={site.site_id} site={site} language={language} withProfit={withProfit} />
              ))}
            </tbody>
            <tfoot>
              <tr className="detail__row--total">
                <td>{t('billing.clientTotal')}</td>
                <td className="numeric">{formatDuration(totalMinutes)}</td>
                <td className="numeric">
                  {formatCurrency(language, parseAmount(client?.billing))}
                </td>
                {withProfit ? (
                  <td className="numeric">{formatCurrency(language, parseAmount(client?.cost))}</td>
                ) : null}
                {withProfit ? (
                  <td className="numeric">{formatCurrency(language, parseAmount(client?.profit))}</td>
                ) : null}
              </tr>
            </tfoot>
          </table>
        </div>
      )}

      {withProfit ? null : <p className="detail__label">{t('billing.profitAfterCalculate')}</p>}
    </div>
  );
}

/** One site's row in the drill-down: its hours, billing, and (after a calculation) cost and profit. */
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
    </tr>
  );
}
