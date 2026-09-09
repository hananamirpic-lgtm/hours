/**
 * The employee directory (Requirement 3, 22).
 *
 * A stable-sorted, paginated list the server owns; search narrows the loaded page on the client over
 * both name forms and country (Requirement 21.5, 22.4), and a status filter re-fetches. A row opens
 * the employee card in a drawer; the "New employee" action opens the create form. Both writes are
 * administrator-only — the button is hidden for other roles, though the server is the real guard.
 */

import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { employeeKeys, listEmployees } from '@/api/employees';
import type { EmployeeListItem, EmployeeStatus } from '@/api/types';
import { useAuth } from '@/auth/AuthProvider';
import { EmployeeCard } from '@/app/employees/EmployeeCard';
import { EmployeeForm } from '@/app/employees/EmployeeForm';
import { Drawer } from '@/components/management/Drawer';
import { ListToolbar } from '@/components/management/ListToolbar';
import { Pagination } from '@/components/management/Pagination';
import { EmployeeStatusPill } from '@/components/management/StatusPill';
import { EmptyState, ErrorState, LoadingState } from '@/components/management/QueryState';
import { useLanguage } from '@/lib/useLanguage';
import { employeeNumberLabel } from '@/lib/employeeLabel';
import { formatDate } from '@/lib/format';

const PAGE_SIZE = 25;
const STATUSES: EmployeeStatus[] = ['active', 'on_leave', 'inactive', 'terminated'];

const matches = (item: EmployeeListItem, term: string): boolean => {
  if (!term) {
    return true;
  }
  const needle = term.trim().toLocaleLowerCase();
  return (
    item.full_name.toLocaleLowerCase().includes(needle) ||
    item.full_name_en.toLocaleLowerCase().includes(needle) ||
    (item.country ?? '').toLocaleLowerCase().includes(needle) ||
    (item.employee_number ?? '').includes(needle) ||
    (item.position ?? '').toLocaleLowerCase().includes(needle)
  );
};

export function EmployeesPage() {
  const { t } = useTranslation();
  const language = useLanguage();
  const { user } = useAuth();
  const canManage = user?.role === 'admin';

  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<EmployeeStatus | ''>('');
  const [offset, setOffset] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const params = { status: status || null, limit: PAGE_SIZE, offset };
  const query = useQuery({
    queryKey: employeeKeys.list(params),
    queryFn: () => listEmployees(params),
  });

  const filtered = useMemo(
    () => (query.data?.items ?? []).filter((item) => matches(item, search)),
    [query.data, search],
  );

  return (
    <section className="page-body">
      <div className="page-header">
        <h1>{t('nav.employees')}</h1>
        {canManage ? (
          <div className="page-header__actions">
            <button type="button" className="button button--primary" onClick={() => setCreating(true)}>
              {t('employee.new')}
            </button>
          </div>
        ) : null}
      </div>

      <ListToolbar search={search} onSearchChange={setSearch}>
        <select
          className="select"
          aria-label={t('employee.filterStatus')}
          value={status}
          onChange={(event) => {
            setStatus(event.target.value as EmployeeStatus | '');
            setOffset(0);
          }}
        >
          <option value="">{t('employee.filterAllStatuses')}</option>
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              {t(`employeeStatus.${value}`)}
            </option>
          ))}
        </select>
      </ListToolbar>

      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState />
      ) : filtered.length === 0 ? (
        <EmptyState messageKey="employee.empty" />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{t('employee.name')}</th>
                <th>{t('employee.nameEn')}</th>
                <th>{t('employee.country')}</th>
                <th>{t('employee.position')}</th>
                <th>{t('employee.startDate')}</th>
                <th>{t('common.status')}</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((item) => (
                <tr
                  key={item.id}
                  tabIndex={0}
                  onClick={() => setOpenId(item.id)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      setOpenId(item.id);
                    }
                  }}
                >
                  <td>{employeeNumberLabel(item.full_name, item.employee_number)}</td>
                  <td>{item.full_name_en}</td>
                  <td>{item.country ?? '—'}</td>
                  <td>{item.position ?? '—'}</td>
                  <td>{formatDate(language, item.start_date)}</td>
                  <td>
                    <EmployeeStatusPill status={item.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {query.data ? (
        <Pagination
          total={query.data.total}
          limit={PAGE_SIZE}
          offset={offset}
          onOffsetChange={setOffset}
        />
      ) : null}

      {openId ? (
        <Drawer title={t('employee.card')} onClose={() => setOpenId(null)}>
          <EmployeeCard employeeId={openId} canManage={canManage} onClosed={() => setOpenId(null)} />
        </Drawer>
      ) : null}

      {creating ? (
        <Drawer title={t('employee.new')} onClose={() => setCreating(false)}>
          <EmployeeForm
            onDone={(id) => {
              setCreating(false);
              setOpenId(id);
            }}
            onCancel={() => setCreating(false)}
          />
        </Drawer>
      ) : null}
    </section>
  );
}
