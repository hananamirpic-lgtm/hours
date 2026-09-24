/**
 * The site card (Requirement 6, 7): its detail, billing-rate history, and the employees assigned to
 * it. Billing rates are finance data — a site manager's payload has them redacted, so the rates tab
 * appears only when the server sent the history (`site_rates` present). Editing, rate history and
 * assignment are administrator-only; a manager reads the site they run.
 */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { getSite, siteKeys } from '@/api/sites';
import { AuditPanel } from '@/app/audit/AuditPanel';
import { useAuth } from '@/auth/AuthProvider';
import { SiteEmployeesPanel } from '@/app/sites/SiteEmployeesPanel';
import { SiteForm } from '@/app/sites/SiteForm';
import { SiteQrPanel } from '@/app/sites/SiteQrPanel';
import { SiteRatesPanel } from '@/app/sites/SiteRatesPanel';
import { SiteStatusPill } from '@/components/management/StatusPill';
import { ErrorState, LoadingState } from '@/components/management/QueryState';
import { useLanguage } from '@/lib/useLanguage';
import { formatCurrency, formatDate } from '@/lib/format';

type Tab = 'details' | 'rates' | 'qr' | 'employees' | 'history';

function Detail({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div className="detail">
      <span className="detail__label">{label}</span>
      <span className="detail__value">{value ? value : '—'}</span>
    </div>
  );
}

export function SiteCard({
  siteId,
  canManage,
  onClosed,
}: {
  siteId: string;
  canManage: boolean;
  onClosed: () => void;
}) {
  const { t } = useTranslation();
  const language = useLanguage();
  const { user } = useAuth();
  const [tab, setTab] = useState<Tab>('details');
  const [editing, setEditing] = useState(false);

  // The audit history is readable by administrators and site managers (Requirement 13.6); a manager
  // may read only their own sites' history, which the server enforces and the panel surfaces as a 403.
  const canSeeHistory = user?.role === 'admin' || user?.role === 'site_manager';

  const query = useQuery({ queryKey: siteKeys.detail(siteId), queryFn: () => getSite(siteId) });

  if (query.isPending) {
    return <LoadingState />;
  }
  if (query.isError || !query.data) {
    return <ErrorState />;
  }

  const site = query.data;
  const canSeeBilling = site.site_rates !== undefined;

  if (editing) {
    return <SiteForm site={site} onDone={() => setEditing(false)} onCancel={() => setEditing(false)} />;
  }

  return (
    <div>
      <h3 className="drawer__title">{site.name}</h3>
      <p className="subtitle numeric">{site.site_number}</p>
      <SiteStatusPill status={site.status} />

      {canManage ? (
        <div className="inline-actions" style={{ marginBlockStart: 'var(--space)' }}>
          <button type="button" className="button button--small" onClick={() => setEditing(true)}>
            {t('common.edit')}
          </button>
          <button type="button" className="button button--small" onClick={onClosed}>
            {t('common.close')}
          </button>
        </div>
      ) : null}

      <div className="tab-row" style={{ marginBlockStart: 'calc(var(--space) * 2)' }}>
        <button
          type="button"
          className={tab === 'details' ? 'button button--small button--primary' : 'button button--small'}
          onClick={() => setTab('details')}
        >
          {t('site.tabDetails')}
        </button>
        {canSeeBilling ? (
          <button
            type="button"
            className={tab === 'rates' ? 'button button--small button--primary' : 'button button--small'}
            onClick={() => setTab('rates')}
          >
            {t('site.tabRates')}
          </button>
        ) : null}
        <button
          type="button"
          className={tab === 'qr' ? 'button button--small button--primary' : 'button button--small'}
          onClick={() => setTab('qr')}
        >
          {t('site.tabQr')}
        </button>
        {canManage ? (
          <button
            type="button"
            className={tab === 'employees' ? 'button button--small button--primary' : 'button button--small'}
            onClick={() => setTab('employees')}
          >
            {t('site.tabEmployees')}
          </button>
        ) : null}
        {canSeeHistory ? (
          <button
            type="button"
            className={tab === 'history' ? 'button button--small button--primary' : 'button button--small'}
            onClick={() => setTab('history')}
          >
            {t('site.tabHistory')}
          </button>
        ) : null}
      </div>

      {tab === 'details' ? (
        <div className="detail-grid">
          <Detail label={t('site.address')} value={site.address} />
          <Detail
            label={t('site.startDate')}
            value={site.start_date ? formatDate(language, site.start_date) : null}
          />

          <Detail label={t('site.qrMode')} value={t(`qrMode.${site.qr_mode}`)} />
          <Detail label={t('site.assignmentMode')} value={t(`assignmentMode.${site.assignment_mode}`)} />
          {canSeeBilling && site.billing_rate ? (
            <Detail
              label={t('site.currentBillingRate')}
              value={formatCurrency(language, Number(site.billing_rate))}
            />
          ) : null}
          <Detail label={t('site.notes')} value={site.notes} />
          <Detail
            label={t('site.assignedCount')}
            value={String(site.employee_ids.length)}
          />
        </div>
      ) : null}

      {tab === 'rates' && canSeeBilling ? <SiteRatesPanel site={site} canManage={canManage} /> : null}

      {tab === 'qr' ? <SiteQrPanel site={site} canManage={canManage} /> : null}

      {tab === 'employees' && canManage ? <SiteEmployeesPanel site={site} /> : null}

      {tab === 'history' && canSeeHistory ? (
        <AuditPanel entityType="sites" entityId={site.id} />
      ) : null}
    </div>
  );
}
