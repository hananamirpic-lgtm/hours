/**
 * Create or edit a client (Requirement 5.1, 5.2, 5.5). Only the name is mandatory; email is
 * format-checked by the server, and payment terms are an integer number of days plus optional free
 * text. A blank optional field is sent empty and the server normalises it to absent.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

import { clientKeys, createClient, updateClient } from '@/api/clients';
import type { ClientCreate, ClientResponse, ClientUpdate } from '@/api/types';
import { toError } from '@/lib/apiError';

interface Fields {
  name: string;
  company: string;
  company_number: string;
  contact_person: string;
  phone: string;
  email: string;
  address: string;
  payment_terms_days: string;
  payment_terms_notes: string;
  notes: string;
}

const emptyFields: Fields = {
  name: '',
  company: '',
  company_number: '',
  contact_person: '',
  phone: '',
  email: '',
  address: '',
  payment_terms_days: '',
  payment_terms_notes: '',
  notes: '',
};

const fromClient = (client: ClientResponse): Fields => ({
  name: client.name,
  company: client.company ?? '',
  company_number: client.company_number ?? '',
  contact_person: client.contact_person ?? '',
  phone: client.phone ?? '',
  email: client.email ?? '',
  address: client.address ?? '',
  payment_terms_days: client.payment_terms_days === null ? '' : String(client.payment_terms_days),
  payment_terms_notes: client.payment_terms_notes ?? '',
  notes: client.notes ?? '',
});

const toPayload = (fields: Fields): ClientCreate => ({
  name: fields.name,
  company: fields.company || null,
  company_number: fields.company_number || null,
  contact_person: fields.contact_person || null,
  phone: fields.phone || null,
  email: fields.email || null,
  address: fields.address || null,
  payment_terms_days: fields.payment_terms_days === '' ? null : Number(fields.payment_terms_days),
  payment_terms_notes: fields.payment_terms_notes || null,
  notes: fields.notes || null,
});

export function ClientForm({
  client,
  onDone,
  onCancel,
}: {
  client?: ClientResponse;
  onDone: (id: string) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const editing = client !== undefined;
  const [fields, setFields] = useState<Fields>(client ? fromClient(client) : emptyFields);
  const [errorKey, setErrorKey] = useState<string | null>(null);

  const set = (key: keyof Fields) => (event: { target: { value: string } }) =>
    setFields((prev) => ({ ...prev, [key]: event.target.value }));

  const mutation = useMutation({
    mutationFn: async (): Promise<ClientResponse> => {
      const payload = toPayload(fields);
      if (editing && client) {
        return updateClient(client.id, payload as ClientUpdate);
      }
      return createClient(payload);
    },
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: clientKeys.all });
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
      <label className="field">
        <span className="field__label">{t('client.name')}</span>
        <input className="input" value={fields.name} onChange={set('name')} required />
      </label>

      <div className="form-row">
        <label className="field">
          <span className="field__label">{t('client.company')}</span>
          <input className="input" value={fields.company} onChange={set('company')} />
        </label>
        <label className="field">
          <span className="field__label">{t('client.companyNumber')}</span>
          <input className="input" value={fields.company_number} onChange={set('company_number')} />
        </label>
      </div>

      <div className="form-row">
        <label className="field">
          <span className="field__label">{t('client.contactPerson')}</span>
          <input className="input" value={fields.contact_person} onChange={set('contact_person')} />
        </label>
        <label className="field">
          <span className="field__label">{t('client.phone')}</span>
          <input className="input" type="tel" value={fields.phone} onChange={set('phone')} />
        </label>
      </div>

      <label className="field">
        <span className="field__label">{t('client.email')}</span>
        <input className="input" type="email" value={fields.email} onChange={set('email')} />
      </label>

      <label className="field">
        <span className="field__label">{t('client.address')}</span>
        <input className="input" value={fields.address} onChange={set('address')} />
      </label>

      <div className="form-row">
        <label className="field">
          <span className="field__label">{t('client.paymentTermsDays')}</span>
          <input
            className="input"
            type="number"
            min="0"
            value={fields.payment_terms_days}
            onChange={set('payment_terms_days')}
          />
        </label>
        <label className="field">
          <span className="field__label">{t('client.paymentTermsNotes')}</span>
          <input className="input" value={fields.payment_terms_notes} onChange={set('payment_terms_notes')} />
        </label>
      </div>

      <label className="field">
        <span className="field__label">{t('client.notes')}</span>
        <input className="input" value={fields.notes} onChange={set('notes')} />
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
