/**
 * The user card (Requirement 1, 2.1, 2.3, 20.8): a login's identity, role, 2FA state and — for a site
 * manager — its assigned site scope. An administrator may edit it, deactivate it (which ends its
 * sessions at once, Requirement 20.8) or reactivate it, and assign a site manager's sites.
 *
 * There is no delete: a login is deactivated, never removed, so its audit trail and any historical
 * attribution survive. The card carries no credential — no password, no TOTP secret.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { deactivateUser, getUser, reactivateUser, userKeys } from '@/api/users';
import { UserForm } from '@/app/users/UserForm';
import { UserSitesPanel } from '@/app/users/UserSitesPanel';
import { ErrorState, LoadingState } from '@/components/management/QueryState';
import { toError } from '@/lib/apiError';
import { useLanguage } from '@/lib/useLanguage';
import { formatDate } from '@/lib/format';

function Detail({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div className="detail">
      <span className="detail__label">{label}</span>
      <span className="detail__value">{value ? value : '—'}</span>
    </div>
  );
}

export function UserCard({ userId }: { userId: string }) {
  const { t } = useTranslation();
  const language = useLanguage();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [errorKey, setErrorKey] = useState<string | null>(null);

  const user = useQuery({ queryKey: userKeys.detail(userId), queryFn: () => getUser(userId) });

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: userKeys.detail(userId) });
    await queryClient.invalidateQueries({ queryKey: userKeys.all });
  };

  const deactivate = useMutation({
    mutationFn: () => deactivateUser(userId),
    onSuccess: invalidate,
    onError: (error) => setErrorKey(toError(error).key),
  });

  const reactivate = useMutation({
    mutationFn: () => reactivateUser(userId),
    onSuccess: invalidate,
    onError: (error) => setErrorKey(toError(error).key),
  });

  if (user.isPending) {
    return <LoadingState />;
  }
  if (user.isError || !user.data) {
    return <ErrorState />;
  }

  if (editing) {
    return <UserForm user={user.data} onDone={() => setEditing(false)} onCancel={() => setEditing(false)} />;
  }

  const data = user.data;
  const twoFactor = data.is_2fa_enabled
    ? t('user.twoFactorOn')
    : data.is_2fa_enrolment_required
      ? t('user.twoFactorRequired')
      : data.is_2fa_enrolment_prompted
        ? t('user.twoFactorPrompted')
        : t('user.twoFactorOff');

  return (
    <div>
      <h3 className="drawer__title">{data.username}</h3>
      <span className={`pill ${data.is_active ? 'pill--active' : 'pill--muted'}`}>
        {data.is_active ? t('user.active') : t('user.inactive')}
      </span>

      <div className="inline-actions" style={{ marginBlockStart: 'var(--space)' }}>
        <button type="button" className="button button--small" onClick={() => setEditing(true)}>
          {t('common.edit')}
        </button>
        {data.is_active ? (
          <button
            type="button"
            className="button button--small button--danger"
            disabled={deactivate.isPending}
            onClick={() => {
              setErrorKey(null);
              deactivate.mutate();
            }}
          >
            {t('user.deactivate')}
          </button>
        ) : (
          <button
            type="button"
            className="button button--small"
            disabled={reactivate.isPending}
            onClick={() => {
              setErrorKey(null);
              reactivate.mutate();
            }}
          >
            {t('user.reactivate')}
          </button>
        )}
      </div>

      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey)}
        </p>
      ) : null}

      <div className="detail-grid" style={{ marginBlockStart: 'calc(var(--space) * 2)' }}>
        <Detail label={t('user.role')} value={t(`role.${data.role}`)} />
        <Detail label={t('user.language')} value={t(`user.language_${data.language}`)} />
        <Detail label={t('user.twoFactor')} value={twoFactor} />
        <Detail
          label={t('user.lastLogin')}
          value={data.last_login_at ? formatDate(language, data.last_login_at) : null}
        />
      </div>

      {data.role === 'site_manager' ? (
        <div className="drawer__section">
          <h4 className="drawer__section-title">{t('userSites.title')}</h4>
          <UserSitesPanel userId={data.id} siteIds={data.site_ids} />
        </div>
      ) : null}

      <p className="subtitle">
        {t('common.updatedAt', { value: formatDate(language, data.updated_at) })}
      </p>
    </div>
  );
}
