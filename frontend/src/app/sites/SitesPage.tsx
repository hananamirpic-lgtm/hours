/**
 * The site directory (Requirement 6, 7). A stable-sorted, paginated list scoped by the server to the
 * caller's sites; search narrows the loaded page on the client over name and site number, and a
 * status filter re-fetches. A row opens the site card. Creating and editing are administrator-only;
 * a site manager reads the sites they run but does not see billing rates, which the server redacts.
 */

import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { listSites, siteKeys } from '@/api/sites';
import type { SiteListItem, SiteStatus } from '@/api/types';
import { useAuth } from '@/auth/AuthProvider';
import { SiteCard } from '@/app/sites/SiteCard';
import { SiteForm } from '@/app/sites/SiteForm';
import { Drawer } from '@/components/management/Drawer';
import { ListToolbar } from '@/components/management/ListToolbar';
import { Pagination } from '@/components/management/Pagination';
import { SiteStatusPill } from '@/components/management/StatusPill';
import { EmptyState, ErrorState, LoadingState } from '@/components/management/QueryState';

const PAGE_SIZE = 25;
const STATUSES: SiteStatus[] = ['active', 'completed', 'on_hold'];

const matches = (item: SiteListItem, term: string): boolean => {
  if (!term) {
    return true;
  }
  const needle = term.trim().toLocaleLowerCase();
  return (
    item.name.toLocaleLowerCase().includes(needle) ||
    item.site_number.toLocaleLowerCase().includes(needle)
  );
};

export function SitesPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const canManage = user?.role === 'admin';

  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<SiteStatus | ''>('');
  const [offset, setOffset] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const params = { status: status || null, limit: PAGE_SIZE, offset };
  const query = useQuery({ queryKey: siteKeys.list(params), queryFn: () => listSites(params) });

  const filtered = useMemo(
    () => (query.data?.items ?? []).filter((item) => matches(item, search)),
    [query.data, search],
  );

  return (
    <section className="page-body">
      <div className="page-header">
        <h1>{t('nav.sites')}</h1>
        {canManage ? (
          <div className="page-header__actions">
            <button type="button" className="button button--primary" onClick={() => setCreating(true)}>
              {t('site.new')}
            </button>
          </div>
        ) : null}
      </div>

      <ListToolbar search={search} onSearchChange={setSearch}>
        <select
          className="select"
          aria-label={t('site.filterStatus')}
          value={status}
          onChange={(event) => {
            setStatus(event.target.value as SiteStatus | '');
            setOffset(0);
          }}
        >
          <option value="">{t('site.filterAllStatuses')}</option>
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              {t(`siteStatus.${value}`)}
            </option>
          ))}
        </select>
      </ListToolbar>

      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState />
      ) : filtered.length === 0 ? (
        <EmptyState messageKey="site.empty" />
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
                  <td className="numeric">{item.site_number}</td>
                  <td>
                    <SiteStatusPill status={item.status} />
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
        <Drawer title={t('site.card')} onClose={() => setOpenId(null)}>
          <SiteCard siteId={openId} canManage={canManage} onClosed={() => setOpenId(null)} />
        </Drawer>
      ) : null}

      {creating ? (
        <Drawer title={t('site.new')} onClose={() => setCreating(false)}>
          <SiteForm onDone={(id) => { setCreating(false); setOpenId(id); }} onCancel={() => setCreating(false)} />
        </Drawer>
      ) : null}
    </section>
  );
}
