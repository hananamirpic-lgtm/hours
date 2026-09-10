/**
 * The client directory (Requirement 5). A stable-sorted, paginated list the server owns; search
 * narrows the loaded page on the client over name, company and company number. Archived clients are
 * hidden unless the toggle asks for them. A row opens the client card, which lists the client's sites
 * (Requirement 5.3). Reading is open to finance roles; creating is administrator-only.
 */

import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { clientKeys, listClients } from '@/api/clients';
import type { ClientListItem } from '@/api/types';
import { useAuth } from '@/auth/AuthProvider';
import { isOperationalAdmin } from '@/app/navigation';
import { ClientCard } from '@/app/clients/ClientCard';
import { ClientForm } from '@/app/clients/ClientForm';
import { Drawer } from '@/components/management/Drawer';
import { ListToolbar } from '@/components/management/ListToolbar';
import { Pagination } from '@/components/management/Pagination';
import { EmptyState, ErrorState, LoadingState } from '@/components/management/QueryState';

const PAGE_SIZE = 25;

const matches = (item: ClientListItem, term: string): boolean => {
  if (!term) {
    return true;
  }
  const needle = term.trim().toLocaleLowerCase();
  return (
    item.name.toLocaleLowerCase().includes(needle) ||
    (item.company ?? '').toLocaleLowerCase().includes(needle) ||
    (item.company_number ?? '').toLocaleLowerCase().includes(needle)
  );
};

export function ClientsPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const canManage = isOperationalAdmin(user?.role);

  const [search, setSearch] = useState('');
  const [includeArchived, setIncludeArchived] = useState(false);
  const [offset, setOffset] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const params = { includeArchived, limit: PAGE_SIZE, offset };
  const query = useQuery({
    queryKey: clientKeys.list(params),
    queryFn: () => listClients(params),
  });

  const filtered = useMemo(
    () => (query.data?.items ?? []).filter((item) => matches(item, search)),
    [query.data, search],
  );

  return (
    <section className="page-body">
      <div className="page-header">
        <h1>{t('nav.clients')}</h1>
        {canManage ? (
          <div className="page-header__actions">
            <button type="button" className="button button--primary" onClick={() => setCreating(true)}>
              {t('client.new')}
            </button>
          </div>
        ) : null}
      </div>

      <ListToolbar search={search} onSearchChange={setSearch}>
        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={includeArchived}
            onChange={(event) => {
              setIncludeArchived(event.target.checked);
              setOffset(0);
            }}
          />
          <span>{t('client.showArchived')}</span>
        </label>
      </ListToolbar>

      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState />
      ) : filtered.length === 0 ? (
        <EmptyState messageKey="client.empty" />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{t('client.name')}</th>
                <th>{t('client.company')}</th>
                <th>{t('client.companyNumber')}</th>
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
                  <td>{item.name}</td>
                  <td>{item.company ?? '—'}</td>
                  <td>{item.company_number ?? '—'}</td>
                  <td>
                    {item.is_archived ? (
                      <span className="pill pill--muted">{t('client.archived')}</span>
                    ) : (
                      <span className="pill pill--active">{t('client.activeStatus')}</span>
                    )}
                  </td>
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
        <Drawer title={t('client.card')} onClose={() => setOpenId(null)}>
          <ClientCard clientId={openId} canManage={canManage} onClosed={() => setOpenId(null)} />
        </Drawer>
      ) : null}

      {creating ? (
        <Drawer title={t('client.new')} onClose={() => setCreating(false)}>
          <ClientForm onDone={(id) => { setCreating(false); setOpenId(id); }} onCancel={() => setCreating(false)} />
        </Drawer>
      ) : null}
    </section>
  );
}
