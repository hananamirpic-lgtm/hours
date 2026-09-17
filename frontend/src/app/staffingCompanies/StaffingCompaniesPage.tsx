/**
 * The staffing-company directory (Requirement 1, 3). A stable-sorted, paginated list the server owns;
 * search narrows the loaded page on the client over name and contact person. A row opens an edit
 * drawer. Delete is guarded: when active employees are still linked the server answers 409 and names
 * them, which this renders so the admin knows exactly who to deactivate first (Requirement 3.2, 3.3).
 * The whole screen is administrator-only, enforced again on the server.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { ApiError } from '@/api/client';
import {
  deleteStaffingCompany,
  getStaffingCompany,
  listStaffingCompanies,
  staffingCompanyKeys,
} from '@/api/staffingCompanies';
import type { StaffingCompanyListItem, StaffingCompanyResponse } from '@/api/types';
import { useAuth } from '@/auth/AuthProvider';
import { isOperationalAdmin } from '@/app/navigation';
import { StaffingCompanyForm } from '@/app/staffingCompanies/StaffingCompanyForm';
import { Drawer } from '@/components/management/Drawer';
import { ListToolbar } from '@/components/management/ListToolbar';
import { Pagination } from '@/components/management/Pagination';
import { EmptyState, ErrorState, LoadingState } from '@/components/management/QueryState';

const PAGE_SIZE = 25;

const matches = (item: StaffingCompanyListItem, term: string): boolean => {
  if (!term) {
    return true;
  }
  const needle = term.trim().toLocaleLowerCase();
  return (
    item.name.toLocaleLowerCase().includes(needle) ||
    item.contact_person.toLocaleLowerCase().includes(needle)
  );
};

interface BlockingEmployee {
  id: string;
  full_name: string;
}

/** An edit drawer that loads the full company, then renders the form; also carries the delete action. */
function EditDrawer({ companyId, onClose }: { companyId: string; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [blockers, setBlockers] = useState<BlockingEmployee[] | null>(null);
  const [deleteErrorKey, setDeleteErrorKey] = useState<string | null>(null);

  const query = useQuery({
    queryKey: staffingCompanyKeys.detail(companyId),
    queryFn: () => getStaffingCompany(companyId),
  });

  const remove = useMutation({
    mutationFn: () => deleteStaffingCompany(companyId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: staffingCompanyKeys.all });
      onClose();
    },
    onError: (error) => {
      setDeleteErrorKey(null);
      setBlockers(null);
      if (error instanceof ApiError && error.code === 'staffing_company_has_active_employees') {
        const employees = (error.params as { employees?: BlockingEmployee[] })?.employees ?? [];
        setBlockers(employees);
        return;
      }
      setDeleteErrorKey(error instanceof ApiError && error.code ? `apiError.${error.code}` : 'apiError.unknown');
    },
  });

  if (query.isPending) {
    return <LoadingState />;
  }
  if (query.isError || !query.data) {
    return <ErrorState />;
  }
  const company: StaffingCompanyResponse = query.data;

  return (
    <div>
      <StaffingCompanyForm company={company} onDone={onClose} onCancel={onClose} />

      <div className="form__actions" style={{ marginBlockStart: 'calc(var(--space) * 2)' }}>
        <button
          type="button"
          className="button button--danger"
          onClick={() => {
            setBlockers(null);
            setDeleteErrorKey(null);
            remove.mutate();
          }}
          disabled={remove.isPending}
        >
          {t('staffingCompany.delete')}
        </button>
      </div>

      {blockers ? (
        <div className="feedback feedback--error" role="alert">
          <p>{t('staffingCompany.deleteBlocked')}</p>
          <ul>
            {blockers.map((employee) => (
              <li key={employee.id}>{employee.full_name}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {deleteErrorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(deleteErrorKey)}
        </p>
      ) : null}
    </div>
  );
}

export function StaffingCompaniesPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const canManage = isOperationalAdmin(user?.role);

  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const params = { limit: PAGE_SIZE, offset };
  const query = useQuery({
    queryKey: staffingCompanyKeys.list(params),
    queryFn: () => listStaffingCompanies(params),
  });

  const filtered = useMemo(
    () => (query.data?.items ?? []).filter((item) => matches(item, search)),
    [query.data, search],
  );

  return (
    <section className="page-body">
      <div className="page-header">
        <h1>{t('nav.staffingCompanies')}</h1>
        {canManage ? (
          <div className="page-header__actions">
            <button type="button" className="button button--primary" onClick={() => setCreating(true)}>
              {t('staffingCompany.new')}
            </button>
          </div>
        ) : null}
      </div>

      <ListToolbar search={search} onSearchChange={setSearch} />

      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState />
      ) : filtered.length === 0 ? (
        <EmptyState messageKey="staffingCompany.empty" />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{t('staffingCompany.name')}</th>
                <th>{t('staffingCompany.contactPerson')}</th>
                <th>{t('staffingCompany.hourlyRate')}</th>
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
                  <td>{item.name}</td>
                  <td>{item.contact_person}</td>
                  <td className="numeric">{item.hourly_rate ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {query.data ? (
        <Pagination total={query.data.total} limit={PAGE_SIZE} offset={offset} onOffsetChange={setOffset} />
      ) : null}

      {openId ? (
        <Drawer title={t('staffingCompany.card')} onClose={() => setOpenId(null)}>
          <EditDrawer companyId={openId} onClose={() => setOpenId(null)} />
        </Drawer>
      ) : null}

      {creating ? (
        <Drawer title={t('staffingCompany.new')} onClose={() => setCreating(false)}>
          <StaffingCompanyForm
            onDone={() => setCreating(false)}
            onCancel={() => setCreating(false)}
          />
        </Drawer>
      ) : null}
    </section>
  );
}