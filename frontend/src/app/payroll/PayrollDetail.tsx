/**
 * The per-employee monthly payroll drill-down (Requirement 16.6, 16.8).
 *
 * Opened from a payroll row, this shows one record in full: the four pay buckets with their hours and
 * pay, the allowances (travel, bonuses), the deductions, and the month's total (Requirement 16.6);
 * then the per-site cost allocation, each site with its hours and its share of the cost, and a footer
 * asserting that the shares sum to the worked pay (Requirement 16.8). It also offers a recalculate for
 * an open month, so accounting can refresh a draft after an entry changes without leaving the card.
 *
 * The figures are the backend's, computed with `Decimal` and `ROUND_HALF_UP` (Requirement 16.7); this
 * only formats them. Every string is a translation key; money goes through `formatCurrency`, minutes
 * through `formatDuration`; the layout uses logical properties for right-to-left.
 */

import { useMutation } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { calculatePayroll } from '@/api/payroll';
import type { PayrollRecord, SiteAllocation } from '@/api/types';
import { allocationMinutes, allocationsTotal, parseAmount, payBuckets } from '@/app/payroll/payrollView';
import { SiteName } from '@/app/payroll/SiteName';
import { toError } from '@/lib/apiError';
import { formatCurrency, formatDateTime, formatDuration } from '@/lib/format';
import type { Language } from '@/i18n';

export function PayrollDetail({
  record,
  language,
  onRecalculated,
}: {
  record: PayrollRecord;
  language: Language;
  onRecalculated: (updated: PayrollRecord) => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [errorParams, setErrorParams] = useState<Record<string, string>>({});

  const buckets = payBuckets(record);
  const isFinal = record.status === 'final';

  const recalc = useMutation({
    mutationFn: () =>
      calculatePayroll({
        employee_id: record.employee_id,
        year: record.year,
        month: record.month,
      }),
    onSuccess: (updated) => {
      setErrorKey(null);
      setErrorParams({});
      void onRecalculated(updated);
    },
    onError: (error) => {
      const translated = toError(error);
      setErrorKey(translated.key);
      setErrorParams(translated.params);
    },
  });

  return (
    <div className="detail">
      {/* The four pay buckets: each its hours and its pay (Requirement 16.6). */}
      <h3 className="section-title">{t('payroll.buckets')}</h3>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>{t('payroll.bucketHead')}</th>
              <th className="numeric">{t('payroll.hours')}</th>
              <th className="numeric">{t('payroll.pay')}</th>
            </tr>
          </thead>
          <tbody>
            {buckets.map((bucket) => (
              <tr key={bucket.key}>
                <td>{t(`payroll.bucket.${bucket.key}`)}</td>
                <td className="numeric">{formatDuration(bucket.minutes)}</td>
                <td className="numeric">{formatCurrency(language, bucket.pay)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Allowances, deductions and the total (Requirement 16.6). */}
      <h3 className="section-title">{t('payroll.adjustments')}</h3>
      <dl className="detail__grid">
        <div className="detail__row">
          <dt className="detail__label">{t('payroll.travel')}</dt>
          <dd className="numeric">{formatCurrency(language, parseAmount(record.travel))}</dd>
        </div>
        <div className="detail__row">
          <dt className="detail__label">{t('payroll.bonuses')}</dt>
          <dd className="numeric">{formatCurrency(language, parseAmount(record.bonuses))}</dd>
        </div>
        <div className="detail__row">
          <dt className="detail__label">{t('payroll.deductions')}</dt>
          <dd className="numeric">{formatCurrency(language, parseAmount(record.deductions))}</dd>
        </div>
        <div className="detail__row detail__row--total">
          <dt className="detail__label">{t('payroll.total')}</dt>
          <dd className="numeric">{formatCurrency(language, parseAmount(record.total_pay))}</dd>
        </div>
      </dl>

      {/* The per-site cost allocation (Requirement 16.8). */}
      <h3 className="section-title">{t('payroll.allocation')}</h3>
      {record.allocations.length === 0 ? (
        <p className="state">{t('payroll.noAllocation')}</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{t('payroll.site')}</th>
                <th className="numeric">{t('payroll.hours')}</th>
                <th className="numeric">{t('payroll.cost')}</th>
              </tr>
            </thead>
            <tbody>
              {record.allocations.map((allocation) => (
                <AllocationRow key={allocation.site_id} allocation={allocation} language={language} />
              ))}
            </tbody>
            <tfoot>
              <tr className="detail__row--total">
                <td>{t('payroll.allocationTotal')}</td>
                <td className="numeric">{formatDuration(allocationMinutesSum(record))}</td>
                <td className="numeric">{formatCurrency(language, allocationsTotal(record))}</td>
              </tr>
            </tfoot>
          </table>
        </div>
      )}

      {record.calculated_at ? (
        <p className="detail__label">
          {t('payroll.calculatedAt', { value: formatDateTime(language, record.calculated_at) })}
        </p>
      ) : null}

      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey, errorParams)}
        </p>
      ) : null}

      <div className="form__actions">
        {isFinal ? (
          <span className="detail__label">{t('payroll.finalNote')}</span>
        ) : (
          <button
            type="button"
            className="button button--primary"
            disabled={recalc.isPending}
            onClick={() => recalc.mutate()}
          >
            {recalc.isPending ? t('common.saving') : t('payroll.recalculateRow')}
          </button>
        )}
      </div>
    </div>
  );
}

/** One site's row in the allocation table: its hours and its share of the cost (Requirement 16.8). */
function AllocationRow({
  allocation,
  language,
}: {
  allocation: SiteAllocation;
  language: Language;
}) {
  return (
    <tr>
      <td>
        <SiteName siteId={allocation.site_id} />
      </td>
      <td className="numeric">{formatDuration(allocationMinutes(allocation))}</td>
      <td className="numeric">{formatCurrency(language, parseAmount(allocation.cost))}</td>
    </tr>
  );
}

/** The minutes summed across every allocation, for the allocation table's footer. */
const allocationMinutesSum = (record: PayrollRecord): number =>
  record.allocations.reduce((sum, allocation) => sum + allocationMinutes(allocation), 0);
