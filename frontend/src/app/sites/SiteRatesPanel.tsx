/**
 * Billing-rate history for a site (Requirement 6.4, 6.6, 17.2). Effective-dated and non-overlapping;
 * a change chains a new row rather than editing an old one, so an already-billed period keeps the
 * rate it was billed at. The whole intended chain is submitted at once and the server reconciles and
 * validates it. The overtime billing rate is optional — left unset, overtime bills at the standard
 * rate. Billing figures are finance data: this panel renders only for a reader the server sends them
 * to (the card gates it), and editing is administrator-only.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { replaceSiteRates, siteKeys } from '@/api/sites';
import type { SiteRateInput, SiteResponse } from '@/api/types';
import { toError } from '@/lib/apiError';
import { useLanguage } from '@/lib/useLanguage';
import { formatCurrency, formatDate } from '@/lib/format';

const blankRow = (): SiteRateInput => ({
  billing_rate: '',
  overtime_billing_rate: null,
  effective_from: '',
  effective_to: null,
});

export function SiteRatesPanel({
  site,
  canManage,
}: {
  site: SiteResponse;
  canManage: boolean;
}) {
  const { t } = useTranslation();
  const language = useLanguage();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [rows, setRows] = useState<SiteRateInput[]>([]);
  const [errorKey, setErrorKey] = useState<string | null>(null);

  const rates = site.site_rates ?? [];

  const startEditing = () => {
    setRows(
      rates.length > 0
        ? rates.map((rate) => ({
            billing_rate: rate.billing_rate,
            overtime_billing_rate: rate.overtime_billing_rate,
            effective_from: rate.effective_from,
            effective_to: rate.effective_to,
          }))
        : [blankRow()],
    );
    setErrorKey(null);
    setEditing(true);
  };

  const setRow = (index: number, key: keyof SiteRateInput, value: string) =>
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, [key]: value || null } : row)));

  const mutation = useMutation({
    mutationFn: () => replaceSiteRates(site.id, rows),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: siteKeys.detail(site.id) });
      setEditing(false);
    },
    onError: (error) => setErrorKey(toError(error).key),
  });

  if (editing) {
    return (
      <div className="drawer__section">
        {rows.map((row, index) => (
          <div key={index} className="card" style={{ marginBlockEnd: 'var(--space)' }}>
            <div className="form-row">
              <label className="field">
                <span className="field__label">{t('site.billingRate')}</span>
                <input
                  className="input"
                  type="number"
                  step="0.01"
                  min="0"
                  value={row.billing_rate}
                  onChange={(event) => setRow(index, 'billing_rate', event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field__label">{t('site.overtimeBillingRate')}</span>
                <input
                  className="input"
                  type="number"
                  step="0.01"
                  min="0"
                  value={row.overtime_billing_rate ?? ''}
                  onChange={(event) => setRow(index, 'overtime_billing_rate', event.target.value)}
                />
              </label>
            </div>
            <div className="form-row">
              <label className="field">
                <span className="field__label">{t('rate.effectiveFrom')}</span>
                <input
                  className="input"
                  type="date"
                  value={row.effective_from}
                  onChange={(event) => setRow(index, 'effective_from', event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field__label">{t('rate.effectiveTo')}</span>
                <input
                  className="input"
                  type="date"
                  value={row.effective_to ?? ''}
                  onChange={(event) => setRow(index, 'effective_to', event.target.value)}
                />
              </label>
            </div>
            <div className="inline-actions">
              <button
                type="button"
                className="button button--small button--danger"
                onClick={() => setRows((prev) => prev.filter((_, i) => i !== index))}
              >
                {t('rate.remove')}
              </button>
            </div>
          </div>
        ))}
        <button
          type="button"
          className="button button--small"
          onClick={() => setRows((prev) => [...prev, blankRow()])}
        >
          {t('rate.add')}
        </button>
        {errorKey ? (
          <p className="feedback feedback--error" role="alert">
            {t(errorKey)}
          </p>
        ) : null}
        <div className="form__actions">
          <button type="button" className="button button--small" onClick={() => setEditing(false)}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="button button--small button--primary"
            disabled={mutation.isPending}
            onClick={() => {
              setErrorKey(null);
              mutation.mutate();
            }}
          >
            {mutation.isPending ? t('common.saving') : t('common.save')}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="drawer__section">
      {rates.length === 0 ? (
        <p className="state">{t('rate.none')}</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{t('rate.effectiveFrom')}</th>
                <th>{t('rate.effectiveTo')}</th>
                <th>{t('site.billingRate')}</th>
                <th>{t('site.overtimeBillingRate')}</th>
              </tr>
            </thead>
            <tbody>
              {rates.map((rate) => (
                <tr key={rate.id}>
                  <td>{formatDate(language, rate.effective_from)}</td>
                  <td>{rate.effective_to ? formatDate(language, rate.effective_to) : t('rate.open')}</td>
                  <td className="numeric">{formatCurrency(language, Number(rate.billing_rate))}</td>
                  <td className="numeric">
                    {rate.overtime_billing_rate
                      ? formatCurrency(language, Number(rate.overtime_billing_rate))
                      : t('site.standardOvertime')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {canManage ? (
        <div className="form__actions">
          <button type="button" className="button button--small" onClick={startEditing}>
            {t('rate.edit')}
          </button>
        </div>
      ) : null}
    </div>
  );
}
