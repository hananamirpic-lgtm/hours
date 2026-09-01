/**
 * The client card (Requirement 5): its commercial detail and the list of sites it owns
 * (Requirement 5.3). Administrators may edit, and may delete only when no site carries a time entry —
 * a refused delete offers archival instead (Requirement 5.4), which the card surfaces as the recorded
 * alternative and lets the user take in one step.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { archiveClient, clientKeys, deleteClient, getClient, getClientSites } from '@/api/clients';
import { ClientForm } from '@/app/clients/ClientForm';
import { SiteStatusPill } from '@/components/management/StatusPill';
import { ErrorState, LoadingState } from '@/components/management/QueryState';
import { hasAction, toError } from '@/lib/apiError';
import { useLanguage } from '@/lib/useLanguage';
import { formatDate } from '@/lib/format';

function Detail({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div className="detail">
      <span className="detail__label">{label}</span>
      <span className="detail__value">{value ? value : '—'}</span>
    </div>
  );
}

export function ClientCard({
  clientId,
  canManage,
  onClosed,
}: {
  clientId: string;
  canManage: boolean;
  onClosed: () => void;
}) {
  const { t } = useTranslation();
  const language = useLanguage();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [offerArchive, setOfferArchive] = useState(false);

  const client = useQuery({ queryKey: clientKeys.detail(clientId), queryFn: () => getClient(clientId) });
  const sites = useQuery({ queryKey: clientKeys.sites(clientId), queryFn: () => getClientSites(clientId) });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: clientKeys.all });

  const remove = useMutation({
    mutationFn: () => deleteClient(clientId),
    onSuccess: async () => {
      await invalidate();
      onClosed();
    },
    onError: (error) => {
      if (hasAction(error, 'archive')) {
        setOfferArchive(true);
        setErrorKey('client.deleteBlocked');
      } else {
        setErrorKey(toError(error).key);
      }
    },
  });

  const archive = useMutation({
    mutationFn: () => archiveClient(clientId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: clientKeys.detail(clientId) });
      await invalidate();
      setOfferArchive(false);
      setErrorKey(null);
    },
    onError: (error) => setErrorKey(toError(error).key),
  });

  if (client.isPending) {
    return <LoadingState />;
  }
  if (client.isError || !client.data) {
    return <ErrorState />;
  }

  if (editing) {
    return (
      <ClientForm client={client.data} onDone={() => setEditing(false)} onCancel={() => setEditing(false)} />
    );
  }

  const data = client.data;

  return (
    <div>
      <h3 className="drawer__title">{data.name}</h3>
      {data.is_archived ? <span className="pill pill--muted">{t('client.archived')}</span> : null}

      {canManage ? (
        <div className="inline-actions" style={{ marginBlockStart: 'var(--space)' }}>
          <button type="button" className="button button--small" onClick={() => setEditing(true)}>
            {t('common.edit')}
          </button>
          {!data.is_archived ? (
            <>
              <button
                type="button"
                className="button button--small button--danger"
                disabled={remove.isPending}
                onClick={() => {
                  setErrorKey(null);
                  setOfferArchive(false);
                  remove.mutate();
                }}
              >
                {t('common.delete')}
              </button>
              <button
                type="button"
                className="button button--small"
                disabled={archive.isPending}
                onClick={() => archive.mutate()}
              >
                {t('client.archive')}
              </button>
            </>
          ) : null}
        </div>
      ) : null}

      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey)}
          {offerArchive ? (
            <>
              {' '}
              <button
                type="button"
                className="button button--small"
                disabled={archive.isPending}
                onClick={() => archive.mutate()}
              >
                {t('client.archive')}
              </button>
            </>
          ) : null}
        </p>
      ) : null}

      <div className="detail-grid" style={{ marginBlockStart: 'calc(var(--space) * 2)' }}>
        <Detail label={t('client.company')} value={data.company} />
        <Detail label={t('client.companyNumber')} value={data.company_number} />
        <Detail label={t('client.contactPerson')} value={data.contact_person} />
        <Detail label={t('client.phone')} value={data.phone} />
        <Detail label={t('client.email')} value={data.email} />
        <Detail label={t('client.address')} value={data.address} />
        <Detail
          label={t('client.paymentTermsDays')}
          value={data.payment_terms_days === null ? null : String(data.payment_terms_days)}
        />
        <Detail label={t('client.paymentTermsNotes')} value={data.payment_terms_notes} />
        <Detail label={t('client.notes')} value={data.notes} />
      </div>

      <div className="drawer__section">
        <h4 className="drawer__section-title">{t('client.sites')}</h4>
        {sites.isPending ? (
          <LoadingState />
        ) : sites.isError ? (
          <ErrorState />
        ) : sites.data.items.length === 0 ? (
          <p className="state">{t('client.noSites')}</p>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('site.name')}</th>
                  <th>{t('site.number')}</th>
                  <th>{t('common.status')}</th>
                </tr>
              </thead>
              <tbody>
                {sites.data.items.map((site) => (
                  <tr key={site.id}>
                    <td>{site.name}</td>
                    <td className="numeric">{site.site_number}</td>
                    <td>
                      <SiteStatusPill status={site.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <p className="subtitle">
        {t('common.updatedAt', { value: formatDate(language, data.updated_at) })}
      </p>
    </div>
  );
}
