/**
 * Create or edit a staffing company (Requirement 1.3-1.8). `name` and `contact_person` are mandatory;
 * `hourly_rate` (a non-negative number), `telephone` and `comments` are optional. A blank optional is
 * sent as null and the server carries it through as absent.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

import {
  createStaffingCompany,
  staffingCompanyKeys,
  updateStaffingCompany,
} from '@/api/staffingCompanies';
import type {
  StaffingCompanyCreate,
  StaffingCompanyResponse,
  StaffingCompanyUpdate,
} from '@/api/types';
import { toError } from '@/lib/apiError';

interface Fields {
  name: string;
  contact_person: string;
  hourly_rate: string;
  telephone: string;
  comments: string;
}

const emptyFields: Fields = {
  name: '',
  contact_person: '',
  hourly_rate: '',
  telephone: '',
  comments: '',
};

const fromCompany = (company: StaffingCompanyResponse): Fields => ({
  name: company.name,
  contact_person: company.contact_person,
  hourly_rate: company.hourly_rate ?? '',
  telephone: company.telephone ?? '',
  comments: company.comments ?? '',
});

const toPayload = (fields: Fields): StaffingCompanyCreate => ({
  name: fields.name,
  contact_person: fields.contact_person,
  hourly_rate: fields.hourly_rate === '' ? null : fields.hourly_rate,
  telephone: fields.telephone || null,
  comments: fields.comments || null,
});

export function StaffingCompanyForm({
  company,
  onDone,
  onCancel,
}: {
  company?: StaffingCompanyResponse;
  onDone: (id: string) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const editing = company !== undefined;
  const [fields, setFields] = useState<Fields>(company ? fromCompany(company) : emptyFields);
  const [errorKey, setErrorKey] = useState<string | null>(null);

  const set = (key: keyof Fields) => (event: { target: { value: string } }) =>
    setFields((prev) => ({ ...prev, [key]: event.target.value }));

  const mutation = useMutation({
    mutationFn: async (): Promise<StaffingCompanyResponse> => {
      const payload = toPayload(fields);
      if (editing && company) {
        return updateStaffingCompany(company.id, payload as StaffingCompanyUpdate);
      }
      return createStaffingCompany(payload);
    },
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: staffingCompanyKeys.all });
      onDone(saved.id);
    },
    onError: (error) => setErrorKey(toError(error).key),
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
          <span className="field__label">{t('staffingCompany.name')}</span>
          <input className="input" value={fields.name} onChange={set('name')} required />
        </label>
        <label className="field">
          <span className="field__label">{t('staffingCompany.contactPerson')}</span>
          <input
            className="input"
            value={fields.contact_person}
            onChange={set('contact_person')}
            required
          />
        </label>
      </div>

      <div className="form-row">
        <label className="field">
          <span className="field__label">{t('staffingCompany.hourlyRate')}</span>
          <input
            className="input"
            type="number"
            min="0"
            step="0.01"
            value={fields.hourly_rate}
            onChange={set('hourly_rate')}
          />
        </label>
        <label className="field">
          <span className="field__label">{t('staffingCompany.telephone')}</span>
          <input className="input" type="tel" value={fields.telephone} onChange={set('telephone')} />
        </label>
      </div>

      <label className="field">
        <span className="field__label">{t('staffingCompany.comments')}</span>
        <input className="input" value={fields.comments} onChange={set('comments')} />
      </label>

      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey)}
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