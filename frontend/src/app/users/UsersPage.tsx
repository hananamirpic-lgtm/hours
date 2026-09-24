/**
 * The user directory (Requirement 1, 2.1, 20.8). A stable-sorted, paginated list the server owns;
 * search narrows the loaded page on the client over the username, and a role filter re-fetches. Both
 * active and deactivated logins show by default so an administrator can find and reactivate a
 * deactivated one; a toggle hides the inactive. A row opens the user card. The whole screen is
 * administrator-only, enforced on the server and hidden from other roles in the navigation.
 */

import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import type { UserListItem, UserRole } from '@/api/types';
import { listUsers, userKeys } from '@/api/users';
import { UserCard } from '@/app/users/UserCard';
import { UserForm } from '@/app/users/UserForm';
import { Drawer } from '@/components/management/Drawer';
import { ListToolbar } from '@/components/management/ListToolbar';
import { Pagination } from '@/components/management/Pagination';
import { EmptyState, ErrorState, LoadingState } from '@/components/management/QueryState';

const PAGE_SIZE = 25;
// Employee logins are managed from the Employees tab (Requirement 5), so the Users tab lists and
// filters only the three console roles.
const CONSOLE_ROLES: UserRole[] = ['admin', 'site_manager', 'accounting'];

const matches = (item: UserListItem, term: string): boolean => {
  if (!term) {
    return true;
  }
  return item.username.toLocaleLowerCase().includes(term.trim().toLocaleLowerCase());
};

export function UsersPage() {
  const { t } = useTranslation();
  const [search, setSearch] = useState('');
  const [role, setRole] = useState<UserRole | ''>('');
  const [includeInactive, setIncludeInactive] = useState(true);
  const [offset, setOffset] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const params = {
    role: role || null,
    includeInactive,
    limit: PAGE_SIZE,
    offset,
  };
  const query = useQuery({
    queryKey: userKeys.list(params),
    queryFn: () => listUsers(params),
  });

  const filtered = useMemo(
    () =>
      (query.data?.items ?? []).filter(
        (item) => item.role !== 'employee' && matches(item, search),
      ),
    [query.data, search],
  );

  return (
    <section className="page-body">
      <div className="page-header">
        <h1>{t('nav.users')}</h1>
        <div className="page-header__actions">
          <button type="button" className="button button--primary" onClick={() => setCreating(true)}>
            {t('user.new')}
          </button>
        </div>
      </div>

      <ListToolbar search={search} onSearchChange={setSearch}>
        <label className="field field--inline">
          <span className="field__label">{t('user.filterRole')}</span>
          <select
            className="input"
            value={role}
            onChange={(event) => {
              setRole(event.target.value as UserRole | '');
              setOffset(0);
            }}
          >
            <option value="">{t('user.allRoles')}</option>
            {CONSOLE_ROLES.map((value) => (
              <option key={value} value={value}>
                {t(`role.${value}`)}
              </option>
            ))}
          </select>
        </label>
        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={includeInactive}
            onChange={(event) => {
              setIncludeInactive(event.target.checked);
              setOffset(0);
            }}
          />
          <span>{t('user.showInactive')}</span>
        </label>
      </ListToolbar>

      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState />
      ) : filtered.length === 0 ? (
        <EmptyState messageKey="user.empty" />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{t('user.username')}</th>
                <th>{t('user.role')}</th>
                <th>{t('user.twoFactor')}</th>
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
                  <td>{item.username}</td>
                  <td>{t(`role.${item.role}`)}</td>
                  <td>
                    {item.is_2fa_enabled ? (
                      <span className="pill pill--active">{t('user.twoFactorOn')}</span>
                    ) : (
                      <span className="pill pill--muted">{t('user.twoFactorOff')}</span>
                    )}
                  </td>
                  <td>
                    {item.is_active ? (
                      <span className="pill pill--active">{t('user.active')}</span>
                    ) : (
                      <span className="pill pill--muted">{t('user.inactive')}</span>
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
        <Drawer title={t('user.card')} onClose={() => setOpenId(null)}>
          <UserCard userId={openId} />
        </Drawer>
      ) : null}

      {creating ? (
        <Drawer title={t('user.new')} onClose={() => setCreating(false)}>
          <UserForm
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
