/**
 * Create or edit a site (Requirement 6.1–6.4, 7.3). Name, number and owning client are mandatory;
 * status, QR mode and assignment mode carry the requirement's defaults. On create an opening billing
 * rate is offered so the site can be billed at once, but it is optional — a site can exist before its
 * rate is agreed, and the rate history is edited on the card afterwards. There is no coordinate or
 * location field, by requirement (6.8).
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

import { clientKeys, listClients } from '@/api/clients';
import { createSite, siteKeys, updateSite } from '@/api/sites';
import type {
  AssignmentMode,
  QrMode,
  SiteCreate,
  SiteResponse,
  SiteStatus,
  SiteUpdate,
} from '@/api/types';
import { toError } from '@/lib/apiError';

const STATUSES: SiteStatus[] = ['active', 'completed', 'on_hold'];
const QR_MODES: QrMode[] = ['unified', 'separate'];
const ASSIGNMENT_MODES: AssignmentMode[] = ['open', 'strict'];

interface Fields {
  name: string;
  site_number: string;
  client_id: string;
  address: string;
  start_date: string;
  status: SiteStatus;
  qr_mode: QrMode;
  assignment_mode: AssignmentMode;
  notes: string;
  billing_rate: string;
  overtime_billing_rate: string;
  rate_effective_from: string;
}

const emptyFields: Fields = {
  name: '',
  site_number: '',
  client_id: '',
  address: '',
  start_date: '',
  status: 'active',
  qr_mode: 'unified',
  assignment_mode: 'open',
  notes: '',
  billing_rate: '',
  overtime_billing_rate: '',
  rate_effective_from: '',
};

const fromSite = (site: SiteResponse): Fields => ({
  name: site.name,
  site_number: site.site_number,
  client_id: site.client_id,
  address: site.address ?? '',
  start_date: site.start_date ?? '',
  status: site.status,
  qr_mode: site.qr_mode,
  assignment_mode: site.assignment_mode,
  notes: site.notes ?? '',
  billing_rate: '',
  overtime_billing_rate: '',
  rate_effective_from: '',
});

export function SiteForm({
  site,
  onDone,
  onCancel,
}: {
  site?: SiteResponse;
  onDone: (id: string) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const editing = site !== undefined;
  const [fields, setFields] = useState<Fields>(site ? fromSite(site) : emptyFields);
  const [errorKey, setErrorKey] = useState<string | null>(null);

  const clientParams = { includeArchived: false, limit: 200, offset: 0 };
  const clients = useQuery({
    queryKey: clientKeys.list(clientParams),
    queryFn: () => listClients(clientParams),
  });

  const set =
    <K extends keyof Fields>(key: K) =>
    (event: { target: { value: string } }) =>
      setFields((prev) => ({ ...prev, [key]: event.target.value as Fields[K] }));

  const mutation = useMutation({
    mutationFn: async (): Promise<SiteResponse> => {
      const base = {
        name: fields.name,
        site_number: fields.site_number,
        client_id: fields.client_id,
        address: fields.address || null,
        start_date: fields.start_date || null,
        status: fields.status,
        qr_mode: fields.qr_mode,
        assignment_mode: fields.assignment_mode,
      };
      if (editing && site) {
        return updateSite(site.id, base as SiteUpdate);
      }
      const payload: SiteCreate = { ...base };
      if (fields.billing_rate && fields.rate_effective_from) {
        payload.rate = {
          billing_rate: fields.billing_rate,
          overtime_billing_rate: fields.overtime_billing_rate || null,
          effective_from: fields.rate_effective_from,
        };
      }
      return createSite(payload);
    },
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: siteKeys.all });
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
          <span className="field__label">{t('site.name')}</span>
          <input className="input" value={fields.name} onChange={set('name')} required />
        </label>
        <label className="field">
          <span className="field__label">{t('site.number')}</span>
          <input className="input" value={fields.site_number} onChange={set('site_number')} required />
        </label>
      </div>

      <label className="field">
        <span className="field__label">{t('site.client')}</span>
        <select className="select" value={fields.client_id} onChange={set('client_id')} required>
          <option value="" disabled>
            {t('site.selectClient')}
          </option>
          {(clients.data?.items ?? []).map((client) => (
            <option key={client.id} value={client.id}>
              {client.name}
            </option>
          ))}
        </select>
      </label>

      <label className="field">
        <span className="field__label">{t('site.address')}</span>
        <input className="input" value={fields.address} onChange={set('address')} />
      </label>

      <label className="field">
        <span className="field__label">{t('site.startDate')}</span>
        <input className="input" type="date" value={fields.start_date} onChange={set('start_date')} />
      </label>

      <div className="form-row">
        <label className="field">
          <span className="field__label">{t('common.status')}</span>
          <select className="select" value={fields.status} onChange={set('status')}>
            {STATUSES.map((value) => (
              <option key={value} value={value}>
                {t(`siteStatus.${value}`)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="field__label">{t('site.qrMode')}</span>
          <select className="select" value={fields.qr_mode} onChange={set('qr_mode')}>
            {QR_MODES.map((value) => (
              <option key={value} value={value}>
                {t(`qrMode.${value}`)}
              </option>
            ))}
          </select>
        </label>
      </div>

      <label className="field">
        <span className="field__label">{t('site.assignmentMode')}</span>
        <select className="select" value={fields.assignment_mode} onChange={set('assignment_mode')}>
          {ASSIGNMENT_MODES.map((value) => (
            <option key={value} value={value}>
              {t(`assignmentMode.${value}`)}
            </option>
          ))}
        </select>
      </label>

      <label className="field">
        <span className="field__label">{t('site.notes')}</span>
        <input className="input" value={fields.notes} onChange={set('notes')} />
      </label>

      {!editing ? (
        <div className="drawer__section">
          <h4 className="drawer__section-title">{t('site.openingRate')}</h4>
          <p className="subtitle">{t('site.openingRateHint')}</p>
          <div className="form-row">
            <label className="field">
              <span className="field__label">{t('site.billingRate')}</span>
              <input
                className="input"
                type="number"
                step="0.01"
                min="0"
                value={fields.billing_rate}
                onChange={set('billing_rate')}
              />
            </label>
            <label className="field">
              <span className="field__label">{t('site.overtimeBillingRate')}</span>
              <input
                className="input"
                type="number"
                step="0.01"
                min="0"
                value={fields.overtime_billing_rate}
                onChange={set('overtime_billing_rate')}
              />
            </label>
          </div>
          <label className="field">
            <span className="field__label">{t('rate.effectiveFrom')}</span>
            <input
              className="input"
              type="date"
              value={fields.rate_effective_from}
              onChange={set('rate_effective_from')}
            />
          </label>
        </div>
      ) : null}

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
