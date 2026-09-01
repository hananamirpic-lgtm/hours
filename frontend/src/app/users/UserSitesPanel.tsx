/**
 * A site manager's site scope (Requirement 2.3). The set of sites here is exactly what the manager may
 * see and write; changing it widens or narrows their reach on their next request, with no token
 * reissue. The whole intended set is submitted at once, mirroring the employee-to-site control.
 * Administrator only, and shown only for the site-manager role — no other role has a site scope.
 *
 * The chooser lists sites with the assigned ones checked and searches the loaded page on the client;
 * one generous page is loaded, which the modest site estate fits within.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { listSites, siteKeys } from '@/api/sites';
import { setUserSites, userKeys } from '@/api/users';
import { toError } from '@/lib/apiError';
import { ErrorState, LoadingState } from '@/components/management/QueryState';

const SITE_PAGE = 200;

export function UserSitesPanel({ userId, siteIds }: { userId: string; siteIds: string[] }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<Set<string>>(() => new Set(siteIds));
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const listParams = { limit: SITE_PAGE, offset: 0 };
  const sites = useQuery({
    queryKey: siteKeys.list(listParams),
    queryFn: () => listSites(listParams),
  });

  const filtered = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return (sites.data?.items ?? []).filter(
      (site) =>
        !needle ||
        site.name.toLocaleLowerCase().includes(needle) ||
        site.site_number.toLocaleLowerCase().includes(needle),
    );
  }, [sites.data, search]);

  const mutation = useMutation({
    mutationFn: () => setUserSites(userId, Array.from(selected)),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: userKeys.detail(userId) });
      setSaved(true);
    },
    onError: (error) => setErrorKey(toError(error).key),
  });

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });

  if (sites.isPending) {
    return <LoadingState />;
  }
  if (sites.isError) {
    return <ErrorState />;
  }

  return (
    <div className="drawer__section">
      <p className="subtitle">{t('userSites.hint')}</p>
      <input
        className="input"
        type="search"
        placeholder={t('assignment.searchSites')}
        aria-label={t('assignment.searchSites')}
        value={search}
        onChange={(event) => {
          setSearch(event.target.value);
          setSaved(false);
        }}
      />
      <ul className="assign-list" style={{ marginBlockStart: 'var(--space)' }}>
        {filtered.map((site) => (
          <li key={site.id} className="assign-list__item">
            <label className="checkbox-field">
              <input type="checkbox" checked={selected.has(site.id)} onChange={() => toggle(site.id)} />
              <span>{t('assignment.siteOption', { name: site.name, number: site.site_number })}</span>
            </label>
          </li>
        ))}
      </ul>

      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey)}
        </p>
      ) : null}
      {saved ? <p className="feedback feedback--success">{t('assignment.saved')}</p> : null}

      <div className="form__actions">
        <button
          type="button"
          className="button button--small button--primary"
          disabled={mutation.isPending}
          onClick={() => {
            setErrorKey(null);
            setSaved(false);
            mutation.mutate();
          }}
        >
          {mutation.isPending ? t('common.saving') : t('assignment.save')}
        </button>
      </div>
    </div>
  );
}
