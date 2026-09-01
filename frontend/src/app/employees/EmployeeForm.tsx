/**
 * Create or edit an employee's personal, contact and employment details (Requirement 3.1–3.3, 3.5).
 *
 * The mandatory fields are marked required and validated by the server too — a blank mandatory field
 * is a field-level error the API returns and this renders. On create, an opening pay row is optional:
 * a record can exist before pay is agreed, and rates are edited on the card afterwards. Status and
 * rate history are not edited here; they move through their own controls on the card.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

import { createEmployee, employeeKeys, updateEmployee } from '@/api/employees';
import type { EmployeeCreate, EmployeeResponse, EmployeeUpdate } from '@/api/types';
import { toError } from '@/lib/apiError';

interface Fields {
  full_name: string;
  full_name_en: string;
  passport_number: string;
  phone: string;
  country: string;
  emergency_contact_name: string;
  emergency_contact_phone: string;
  start_date: string;
  position: string;
  date_of_birth: string;
  address: string;
  notes: string;
}

const emptyFields: Fields = {
  full_name: '',
  full_name_en: '',
  passport_number: '',
  phone: '',
  country: '',
  emergency_contact_name: '',
  emergency_contact_phone: '',
  start_date: '',
  position: '',
  date_of_birth: '',
  address: '',
  notes: '',
};

const fromEmployee = (employee: EmployeeResponse): Fields => ({
  full_name: employee.full_name,
  full_name_en: employee.full_name_en,
  passport_number: employee.passport_number,
  phone: employee.phone,
  country: employee.country,
  emergency_contact_name: employee.emergency_contact_name,
  emergency_contact_phone: employee.emergency_contact_phone,
  start_date: employee.start_date,
  position: employee.position ?? '',
  date_of_birth: employee.date_of_birth ?? '',
  address: employee.address ?? '',
  notes: employee.notes ?? '',
});

const toPayload = (fields: Fields): EmployeeCreate => ({
  full_name: fields.full_name,
  full_name_en: fields.full_name_en,
  passport_number: fields.passport_number,
  phone: fields.phone,
  country: fields.country,
  emergency_contact_name: fields.emergency_contact_name,
  emergency_contact_phone: fields.emergency_contact_phone,
  start_date: fields.start_date,
  position: fields.position || null,
  date_of_birth: fields.date_of_birth || null,
  address: fields.address || null,
  notes: fields.notes || null,
});

export function EmployeeForm({
  employee,
  onDone,
  onCancel,
}: {
  employee?: EmployeeResponse;
  onDone: (id: string) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const editing = employee !== undefined;
  const [fields, setFields] = useState<Fields>(employee ? fromEmployee(employee) : emptyFields);
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [errorParams, setErrorParams] = useState<Record<string, string>>({});

  const set = (key: keyof Fields) => (event: { target: { value: string } }) =>
    setFields((prev) => ({ ...prev, [key]: event.target.value }));

  const mutation = useMutation({
    mutationFn: async (): Promise<EmployeeResponse> => {
      const payload = toPayload(fields);
      if (editing && employee) {
        return updateEmployee(employee.id, payload as EmployeeUpdate);
      }
      return createEmployee(payload);
    },
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: employeeKeys.all });
      onDone(saved.id);
    },
    onError: (error) => {
      const { key, params } = toError(error);
      setErrorKey(key);
      setErrorParams(params);
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    setErrorKey(null);
    mutation.mutate();
  };

  return (
    <form className="form" onSubmit={onSubmit}>
      <div className="form-row">
        <label className="field">
          <span className="field__label">{t('employee.name')}</span>
          <input className="input" value={fields.full_name} onChange={set('full_name')} required />
        </label>
        <label className="field">
          <span className="field__label">{t('employee.nameEn')}</span>
          <input className="input" value={fields.full_name_en} onChange={set('full_name_en')} required />
        </label>
      </div>

      <div className="form-row">
        <label className="field">
          <span className="field__label">{t('employee.passport')}</span>
          <input className="input" value={fields.passport_number} onChange={set('passport_number')} required />
        </label>
        <label className="field">
          <span className="field__label">{t('employee.phone')}</span>
          <input className="input" type="tel" value={fields.phone} onChange={set('phone')} required />
        </label>
      </div>

      <div className="form-row">
        <label className="field">
          <span className="field__label">{t('employee.country')}</span>
          <input className="input" value={fields.country} onChange={set('country')} required />
        </label>
        <label className="field">
          <span className="field__label">{t('employee.position')}</span>
          <input className="input" value={fields.position} onChange={set('position')} />
        </label>
      </div>

      <div className="form-row">
        <label className="field">
          <span className="field__label">{t('employee.emergencyName')}</span>
          <input
            className="input"
            value={fields.emergency_contact_name}
            onChange={set('emergency_contact_name')}
            required
          />
        </label>
        <label className="field">
          <span className="field__label">{t('employee.emergencyPhone')}</span>
          <input
            className="input"
            type="tel"
            value={fields.emergency_contact_phone}
            onChange={set('emergency_contact_phone')}
            required
          />
        </label>
      </div>

      <div className="form-row">
        <label className="field">
          <span className="field__label">{t('employee.startDate')}</span>
          <input className="input" type="date" value={fields.start_date} onChange={set('start_date')} required />
        </label>
        <label className="field">
          <span className="field__label">{t('employee.dateOfBirth')}</span>
          <input className="input" type="date" value={fields.date_of_birth} onChange={set('date_of_birth')} />
        </label>
      </div>

      <label className="field">
        <span className="field__label">{t('employee.address')}</span>
        <input className="input" value={fields.address} onChange={set('address')} />
      </label>

      <label className="field">
        <span className="field__label">{t('employee.notes')}</span>
        <input className="input" value={fields.notes} onChange={set('notes')} />
      </label>

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
          {mutation.isPending ? t('common.saving') : t('common.save')}
        </button>
      </div>
    </form>
  );
}
