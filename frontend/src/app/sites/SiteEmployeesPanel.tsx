/**
 * Employee-to-site assignment from the site side (Requirement 7.1). Mirrors the employee-side control:
 * the set of employees expected at this site, submitted whole. The assignment is an expectation only
 * and never restricts where an employee records time (Requirement 7.2). Administrator only.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { employeeKeys, listEmployees } from '@/api/employees';
import { setSiteEmployees, siteKeys } from '@/api/sites';
import type { SiteResponse } from '@/api/types';
import { toError } from '@/lib/apiError';
import { ErrorState, LoadingState } from '@/components/management/QueryState';

const EMPLOYEE_PAGE = 200;

export function SiteEmployeesPanel({ site }: { site: SiteResponse }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set(site.employee_ids));
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const listParams = { status: 'active' as const, limit: EMPLOYEE_PAGE, offset: 0 };
  const employees = useQuery({
    queryKey: employeeKeys.list(listParams),
    queryFn: () => listEmployees(listParams),
  });

  useEffect(() => {
    setSelected(new Set(site.employee_ids));
  }, [site.employee_ids]);

  const filtered = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return (employees.data?.items ?? []).filter(
      (employee) =>
        !needle ||
        employee.full_name.toLocaleLowerCase().includes(needle) ||
        employee.full_name_en.toLocaleLowerCase().includes(needle),
    );
  }, [employees.data, search]);

  const mutation = useMutation({
    mutationFn: () => setSiteEmployees(site.id, Array.from(selected)),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: siteKeys.detail(site.id) });
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

  if (employees.isPending) {
    return <LoadingState />;
  }
  if (employees.isError) {
    return <ErrorState />;
  }

  return (
    <div className="drawer__section">
      <p className="subtitle">{t('assignment.siteHint')}</p>
      <input
        className="input"
        type="search"
        placeholder={t('assignment.searchEmployees')}
        aria-label={t('assignment.searchEmployees')}
        value={search}
        onChange={(event) => {
          setSearch(event.target.value);
          setSaved(false);
        }}
      />
      <ul className="assign-list" style={{ marginBlockStart: 'var(--space)' }}>
        {filtered.map((employee) => (
          <li key={employee.id} className="assign-list__item">
            <label className="checkbox-field">
              <input
                type="checkbox"
                checked={selected.has(employee.id)}
                onChange={() => toggle(employee.id)}
              />
              <span>
                {t('assignment.employeeOption', {
                  name: employee.full_name,
                  nameEn: employee.full_name_en,
                })}
              </span>
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
