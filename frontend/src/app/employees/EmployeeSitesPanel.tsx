/**
 * Employee-to-site assignment from the employee side (Requirement 7.1). The assignment records which
 * sites an employee is *expected* at; it never restricts where they may record time (Requirement 7.2).
 * The whole intended set is submitted at once, mirroring the site-side control. Administrator only.
 *
 * The chooser lists every site with the assigned ones checked. It searches the loaded page on the
 * client; a very large estate would page, but the assignment set is small and the sites list is
 * modest, so a single generous page is loaded here.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { employeeKeys, getEmployeeSites, setEmployeeSites } from '@/api/employees';
import { listSites, siteKeys } from '@/api/sites';
import { toError } from '@/lib/apiError';
import { ErrorState, LoadingState } from '@/components/management/QueryState';

const SITE_PAGE = 200;

export function EmployeeSitesPanel({ employeeId }: { employeeId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const assigned = useQuery({
    queryKey: employeeKeys.sites(employeeId),
    queryFn: () => getEmployeeSites(employeeId),
  });

  const listParams = { limit: SITE_PAGE, offset: 0 };
  const sites = useQuery({
    queryKey: siteKeys.list(listParams),
    queryFn: () => listSites(listParams),
  });

  useEffect(() => {
    if (assigned.data) {
      setSelected(new Set(assigned.data.site_ids));
    }
  }, [assigned.data]);

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
    mutationFn: () => setEmployeeSites(employeeId, Array.from(selected)),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: employeeKeys.sites(employeeId) });
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

  if (assigned.isPending || sites.isPending) {
    return <LoadingState />;
  }
  if (assigned.isError || sites.isError) {
    return <ErrorState />;
  }

  return (
    <div className="drawer__section">
      <p className="subtitle">{t('assignment.employeeHint')}</p>
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
      {saved ? (
        <p className="feedback feedback--success">{t('assignment.saved')}</p>
      ) : null}

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
