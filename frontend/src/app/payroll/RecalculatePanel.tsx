/**
 * The recalculate panel (Requirement 16.5, 16.10).
 *
 * Recomputing an employee's month is a per-employee action: the calculate endpoint takes one employee
 * and month, reads that month's Approved or Locked entries, and replaces the existing draft rather
 * than duplicating it (Requirement 16.10). This panel names the month it was opened for, lets the user
 * pick the employee (pre-selected when the list was already filtered to one), and offers the optional
 * monthly adjustments — a travel override, bonuses and deductions (Requirement 16.5). Left blank, the
 * service derives travel from the daily rate over the days worked and treats the adjustments as
 * nothing, which is the common case; the fields are there for the month that needs a correction.
 *
 * The adjustment inputs are decimal strings sent as-is, so the backend's `Decimal` never passes
 * through a float (Requirement 16.7). Every string is a translation key; the layout is logical.
 */

import { useMutation } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { calculatePayroll } from '@/api/payroll';
import type { EmployeeListItem, PayrollCalculateRequest } from '@/api/types';
import { toError } from '@/lib/apiError';
import type { Language } from '@/i18n';

/** A blank or well-formed non-negative decimal; the amount fields validate against this before submit. */
const DECIMAL_PATTERN = /^\d+(\.\d{1,2})?$/;

const employeeLabel = (language: Language, name: string, nameEn: string): string => {
  const primary = language === 'he' ? name : nameEn;
  const secondary = language === 'he' ? nameEn : name;
  return primary === secondary ? primary : `${primary} (${secondary})`;
};

export function RecalculatePanel({
  year,
  month,
  employees,
  initialEmployeeId,
  language,
  onDone,
  onCancel,
}: {
  year: number;
  month: number;
  employees: EmployeeListItem[];
  initialEmployeeId: string | null;
  language: Language;
  onDone: () => void | Promise<void>;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [employeeId, setEmployeeId] = useState(initialEmployeeId ?? '');
  const [travel, setTravel] = useState('');
  const [bonuses, setBonuses] = useState('');
  const [deductions, setDeductions] = useState('');
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [errorParams, setErrorParams] = useState<Record<string, string>>({});

  const mutation = useMutation({
    mutationFn: (payload: PayrollCalculateRequest) => calculatePayroll(payload),
    onSuccess: () => {
      void onDone();
    },
    onError: (error) => {
      const translated = toError(error);
      setErrorKey(translated.key);
      setErrorParams(translated.params);
    },
  });

  const onSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    setErrorKey(null);
    setErrorParams({});
    setFieldError(null);

    if (!employeeId) {
      setFieldError('payroll.employeeRequired');
      return;
    }
    for (const value of [travel, bonuses, deductions]) {
      if (value.trim() !== '' && !DECIMAL_PATTERN.test(value.trim())) {
        setFieldError('payroll.amountInvalid');
        return;
      }
    }

    const payload: PayrollCalculateRequest = { employee_id: employeeId, year, month };
    if (travel.trim() !== '') {
      payload.travel = travel.trim();
    }
    if (bonuses.trim() !== '') {
      payload.bonuses = bonuses.trim();
    }
    if (deductions.trim() !== '') {
      payload.deductions = deductions.trim();
    }
    mutation.mutate(payload);
  };

  return (
    <form className="form" onSubmit={onSubmit}>
      <p>{t('payroll.recalculatePrompt')}</p>

      <label className="field">
        <span className="field__label">{t('payroll.employee')}</span>
        <select
          className="select"
          value={employeeId}
          onChange={(event) => setEmployeeId(event.target.value)}
          required
        >
          <option value="">{t('payroll.selectEmployee')}</option>
          {employees.map((employee) => (
            <option key={employee.id} value={employee.id}>
              {employeeLabel(language, employee.full_name, employee.full_name_en)}
            </option>
          ))}
        </select>
      </label>

      <label className="field">
        <span className="field__label">{t('payroll.travelOverride')}</span>
        <input
          className="input"
          type="text"
          inputMode="decimal"
          value={travel}
          onChange={(event) => setTravel(event.target.value)}
          placeholder={t('payroll.derivedPlaceholder')}
        />
      </label>

      <label className="field">
        <span className="field__label">{t('payroll.bonuses')}</span>
        <input
          className="input"
          type="text"
          inputMode="decimal"
          value={bonuses}
          onChange={(event) => setBonuses(event.target.value)}
          placeholder={t('payroll.zeroPlaceholder')}
        />
      </label>

      <label className="field">
        <span className="field__label">{t('payroll.deductions')}</span>
        <input
          className="input"
          type="text"
          inputMode="decimal"
          value={deductions}
          onChange={(event) => setDeductions(event.target.value)}
          placeholder={t('payroll.zeroPlaceholder')}
        />
      </label>

      {fieldError ? (
        <p className="feedback feedback--error" role="alert">
          {t(fieldError)}
        </p>
      ) : null}
      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey, errorParams)}
        </p>
      ) : null}

      <div className="form__actions">
        <button type="button" className="button" onClick={onCancel}>
          {t('common.cancel')}
        </button>
        <button type="submit" className="button button--primary" disabled={mutation.isPending}>
          {mutation.isPending ? t('common.saving') : t('payroll.confirmRecalculate')}
        </button>
      </div>
    </form>
  );
}
