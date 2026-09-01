/**
 * The employee card (Requirement 3): personal information, employment details, pay rates, documents,
 * status and photo. Read for anyone permitted to see the employee; the write controls — edit, status,
 * rates, documents, photo, site assignment — appear only for an administrator (`canManage`), and the
 * server enforces the same boundary.
 *
 * Wage fields arrive only for a finance or admin reader; a site manager's payload has them removed by
 * redaction, so their absence is rendered as "not shown" rather than "unset". The card reads that by
 * whether `rates` is present at all.
 */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { employeeKeys, getEmployee } from '@/api/employees';
import { AuditPanel } from '@/app/audit/AuditPanel';
import { useAuth } from '@/auth/AuthProvider';
import { EmployeeDocumentsPanel } from '@/app/employees/EmployeeDocumentsPanel';
import { EmployeeForm } from '@/app/employees/EmployeeForm';
import { TimeEntryForm } from '@/app/hours/TimeEntryForm';
import { EmployeePhoto } from '@/app/employees/EmployeePhoto';
import { EmployeeRatesPanel } from '@/app/employees/EmployeeRatesPanel';
import { EmployeeSitesPanel } from '@/app/employees/EmployeeSitesPanel';
import { EmployeeStatusControl } from '@/app/employees/EmployeeStatusControl';
import { EmployeeStatusPill } from '@/components/management/StatusPill';
import { ErrorState, LoadingState } from '@/components/management/QueryState';
import { useLanguage } from '@/lib/useLanguage';
import { formatDate } from '@/lib/format';

type Tab = 'details' | 'rates' | 'documents' | 'sites' | 'history';

function Detail({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div className="detail">
      <span className="detail__label">{label}</span>
      <span className="detail__value">{value ? value : '—'}</span>
    </div>
  );
}

export function EmployeeCard({
  employeeId,
  canManage,
  onClosed,
}: {
  employeeId: string;
  canManage: boolean;
  onClosed: () => void;
}) {
  const { t } = useTranslation();
  const language = useLanguage();
  const { user } = useAuth();
  const [tab, setTab] = useState<Tab>('details');
  const [editing, setEditing] = useState(false);
  // When set, the manual time-entry form is shown for this employee (the QR-less fallback: a manager
  // records a shift by hand when the employee could not scan). Reuses the Hours-page form in create
  // mode, prefilled with this employee.
  const [recordingEntry, setRecordingEntry] = useState(false);

  // The audit history is readable by administrators and site managers (Requirement 13.6); the server
  // scopes a manager to employees at their sites and answers a 403 otherwise, which the panel shows.
  // Accounting reads the card but not its history, so the tab is hidden for them.
  const canSeeHistory = user?.role === 'admin' || user?.role === 'site_manager';

  const query = useQuery({
    queryKey: employeeKeys.detail(employeeId),
    queryFn: () => getEmployee(employeeId),
  });

  if (query.isPending) {
    return <LoadingState />;
  }
  if (query.isError || !query.data) {
    return <ErrorState />;
  }

  const employee = query.data;
  const canSeeWage = employee.rates !== undefined;

  if (editing) {
    return (
      <EmployeeForm
        employee={employee}
        onDone={() => setEditing(false)}
        onCancel={() => setEditing(false)}
      />
    );
  }

  if (recordingEntry) {
    return (
      <div>
        <h3 className="drawer__title">{t('manualEntry.new')}</h3>
        <p className="subtitle">{employee.full_name}</p>
        <TimeEntryForm
          prefill={{ employeeId: employee.id }}
          onDone={() => setRecordingEntry(false)}
          onCancel={() => setRecordingEntry(false)}
        />
      </div>
    );
  }

  return (
    <div>
      <div className="photo-row">
        <EmployeePhoto employee={employee} canManage={canManage} />
        <div>
          <h3 className="drawer__title">{employee.full_name}</h3>
          <p className="subtitle detail__value">{employee.full_name_en}</p>
          <EmployeeStatusPill status={employee.status} />
          {employee.has_expired_document ? (
            <span className="pill pill--danger">{t('employee.expiredDocument')}</span>
          ) : null}
        </div>
      </div>

      {canManage ? (
        <div className="inline-actions" style={{ marginBlockStart: 'var(--space)' }}>
          <button type="button" className="button button--small" onClick={() => setEditing(true)}>
            {t('common.edit')}
          </button>
          <button
            type="button"
            className="button button--small"
            onClick={() => setRecordingEntry(true)}
          >
            {t('manualEntry.new')}
          </button>
          <EmployeeStatusControl employee={employee} onDone={onClosed} />
        </div>
      ) : null}

      <div className="tab-row" style={{ marginBlockStart: 'calc(var(--space) * 2)' }}>
        <button
          type="button"
          className={tab === 'details' ? 'button button--small button--primary' : 'button button--small'}
          onClick={() => setTab('details')}
        >
          {t('employee.tabDetails')}
        </button>
        {canSeeWage ? (
          <button
            type="button"
            className={tab === 'rates' ? 'button button--small button--primary' : 'button button--small'}
            onClick={() => setTab('rates')}
          >
            {t('employee.tabRates')}
          </button>
        ) : null}
        {canManage ? (
          <>
            <button
              type="button"
              className={tab === 'documents' ? 'button button--small button--primary' : 'button button--small'}
              onClick={() => setTab('documents')}
            >
              {t('employee.tabDocuments')}
            </button>
            <button
              type="button"
              className={tab === 'sites' ? 'button button--small button--primary' : 'button button--small'}
              onClick={() => setTab('sites')}
            >
              {t('employee.tabSites')}
            </button>
          </>
        ) : null}
        {canSeeHistory ? (
          <button
            type="button"
            className={tab === 'history' ? 'button button--small button--primary' : 'button button--small'}
            onClick={() => setTab('history')}
          >
            {t('employee.tabHistory')}
          </button>
        ) : null}
      </div>

      {tab === 'details' ? (
        <div className="detail-grid">
          <Detail label={t('employee.passport')} value={employee.passport_number} />
          <Detail label={t('employee.phone')} value={employee.phone} />
          <Detail label={t('employee.country')} value={employee.country} />
          <Detail label={t('employee.position')} value={employee.position} />
          <Detail
            label={t('employee.startDate')}
            value={formatDate(language, employee.start_date)}
          />
          <Detail
            label={t('employee.dateOfBirth')}
            value={employee.date_of_birth ? formatDate(language, employee.date_of_birth) : null}
          />
          <Detail label={t('employee.address')} value={employee.address} />
          <Detail label={t('employee.emergencyName')} value={employee.emergency_contact_name} />
          <Detail label={t('employee.emergencyPhone')} value={employee.emergency_contact_phone} />
          <Detail label={t('employee.notes')} value={employee.notes} />
        </div>
      ) : null}

      {tab === 'rates' && canSeeWage ? (
        <EmployeeRatesPanel employee={employee} canManage={canManage} />
      ) : null}

      {tab === 'documents' && canManage ? (
        <EmployeeDocumentsPanel employeeId={employee.id} />
      ) : null}

      {tab === 'sites' && canManage ? <EmployeeSitesPanel employeeId={employee.id} /> : null}

      {tab === 'history' && canSeeHistory ? (
        <AuditPanel entityType="employees" entityId={employee.id} />
      ) : null}
    </div>
  );
}
