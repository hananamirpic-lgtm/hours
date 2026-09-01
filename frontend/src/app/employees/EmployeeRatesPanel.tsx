/**
 * Pay-rate history for an employee (Requirement 3.3, 16.9). The history is effective-dated and
 * non-overlapping; a change chains a new row rather than editing an old one, so a past period keeps
 * the rate it was paid at. The whole intended chain is submitted at once and the server reconciles
 * and validates it — a rejected chain leaves the existing history untouched.
 *
 * Rates are wage data: this panel renders only for a reader permitted to see them (the card gates it),
 * and editing is administrator-only.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { employeeKeys, replaceEmployeeRates } from '@/api/employees';
import type { EmployeeRateInput, EmployeeResponse } from '@/api/types';
import { toError } from '@/lib/apiError';
import { useLanguage } from '@/lib/useLanguage';
import { formatCurrency, formatDate } from '@/lib/format';

const blankRow = (): EmployeeRateInput => ({
  hourly_wage: '',
  overtime_rate: '',
  shabbat_holiday_rate: '',
  travel_allowance_daily: '0',
  effective_from: '',
  effective_to: null,
});

export function EmployeeRatesPanel({
  employee,
  canManage,
}: {
  employee: EmployeeResponse;
  canManage: boolean;
}) {
  const { t } = useTranslation();
  const language = useLanguage();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [rows, setRows] = useState<EmployeeRateInput[]>([]);
  const [errorKey, setErrorKey] = useState<string | null>(null);

  const rates = employee.rates ?? [];

  const startEditing = () => {
    setRows(
      rates.length > 0
        ? rates.map((rate) => ({
            hourly_wage: rate.hourly_wage,
            overtime_rate: rate.overtime_rate,
            shabbat_holiday_rate: rate.shabbat_holiday_rate,
            travel_allowance_daily: rate.travel_allowance_daily,
            effective_from: rate.effective_from,
            effective_to: rate.effective_to,
          }))
        : [blankRow()],
    );
    setErrorKey(null);
    setEditing(true);
  };

  const setRow = (index: number, key: keyof EmployeeRateInput, value: string) =>
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, [key]: value || null } : row)));

  const mutation = useMutation({
    mutationFn: () =>
      replaceEmployeeRates(
        employee.id,
        rows.map((row) => ({ ...row, travel_allowance_daily: row.travel_allowance_daily || '0' })),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: employeeKeys.detail(employee.id) });
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
                <span className="field__label">{t('rate.hourlyWage')}</span>
                <input
                  className="input"
                  type="number"
                  step="0.01"
                  min="0"
                  value={row.hourly_wage}
                  onChange={(event) => setRow(index, 'hourly_wage', event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field__label">{t('rate.overtimeRate')}</span>
                <input
                  className="input"
                  type="number"
                  step="0.01"
                  min="0"
                  value={row.overtime_rate}
                  onChange={(event) => setRow(index, 'overtime_rate', event.target.value)}
                />
              </label>
            </div>
            <div className="form-row">
              <label className="field">
                <span className="field__label">{t('rate.shabbatHolidayRate')}</span>
                <input
                  className="input"
                  type="number"
                  step="0.01"
                  min="0"
                  value={row.shabbat_holiday_rate}
                  onChange={(event) => setRow(index, 'shabbat_holiday_rate', event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field__label">{t('rate.travelAllowance')}</span>
                <input
                  className="input"
                  type="number"
                  step="0.01"
                  min="0"
                  value={row.travel_allowance_daily ?? ''}
                  onChange={(event) => setRow(index, 'travel_allowance_daily', event.target.value)}
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
                <th>{t('rate.hourlyWage')}</th>
                <th>{t('rate.overtimeRate')}</th>
                <th>{t('rate.shabbatHolidayRate')}</th>
                <th>{t('rate.travelAllowance')}</th>
              </tr>
            </thead>
            <tbody>
              {rates.map((rate) => (
                <tr key={rate.id}>
                  <td>{formatDate(language, rate.effective_from)}</td>
                  <td>{rate.effective_to ? formatDate(language, rate.effective_to) : t('rate.open')}</td>
                  <td className="numeric">{formatCurrency(language, Number(rate.hourly_wage))}</td>
                  <td className="numeric">{formatCurrency(language, Number(rate.overtime_rate))}</td>
                  <td className="numeric">{formatCurrency(language, Number(rate.shabbat_holiday_rate))}</td>
                  <td className="numeric">{formatCurrency(language, Number(rate.travel_allowance_daily))}</td>
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
