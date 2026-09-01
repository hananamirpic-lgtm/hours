import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { getHealth, getReadiness, healthKeys } from '@/api/health';
import type { DependencyStatus } from '@/api/types';

function DependencyRow({ check }: { check: DependencyStatus }) {
  const { t } = useTranslation();
  const label = t(`health.dependency.${check.name}`, { defaultValue: check.name });

  return (
    <li className="status-list__item">
      <span>{label}</span>
      <span>
        <span className={check.status === 'up' ? 'badge badge--up' : 'badge badge--down'}>
          {t(`health.status.${check.status}`)}
        </span>
        <span className="numeric">{t('health.latency', { value: check.latency_ms })}</span>
      </span>
    </li>
  );
}

/**
 * Proves the whole path end to end on first load: browser → Vite proxy → API → dependencies.
 */
export function SystemStatus() {
  const { t } = useTranslation();

  const liveness = useQuery({ queryKey: healthKeys.liveness, queryFn: getHealth });
  const readiness = useQuery({ queryKey: healthKeys.readiness, queryFn: getReadiness });

  return (
    <section className="card">
      <h2 className="card__title">{t('health.title')}</h2>

      <ul className="status-list">
        <li className="status-list__item">
          <span>{t('health.liveness')}</span>
          {liveness.isPending ? (
            <span className="numeric">{t('health.loading')}</span>
          ) : liveness.isError ? (
            <span className="badge badge--down">{t('health.unreachable')}</span>
          ) : (
            <span>
              <span className="badge badge--up">{t('health.status.healthy')}</span>
              <span className="numeric">{liveness.data.version}</span>
            </span>
          )}
        </li>

        <li className="status-list__item">
          <span>{t('health.readiness')}</span>
          {readiness.isPending ? (
            <span className="numeric">{t('health.loading')}</span>
          ) : readiness.isError ? (
            <span className="badge badge--down">{t('health.unreachable')}</span>
          ) : (
            <span className={readiness.data.status === 'healthy' ? 'badge badge--up' : 'badge badge--down'}>
              {t(`health.status.${readiness.data.status}`)}
            </span>
          )}
        </li>
      </ul>

      {readiness.data ? (
        <ul className="status-list">
          {readiness.data.checks.map((check) => (
            <DependencyRow key={check.name} check={check} />
          ))}
        </ul>
      ) : null}
    </section>
  );
}
