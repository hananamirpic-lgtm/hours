/**
 * The administrator period screen (Requirement 15.4, 15.6, 15.7).
 *
 * Locking a month is an administrator action: it freezes the month's Approved entries so payroll and
 * billing cannot shift underneath accounting (Requirement 15.4). This screen lets an administrator
 * pick a month, attempt to lock it, and — when the month still holds entries that are not Approved —
 * see the warning list and choose to lock past them (Requirement 15.7). It also lists the months the
 * workflow has already touched and their state, and offers an unlock with a mandatory reason
 * (Requirement 15.6).
 *
 * The lock endpoint answers a first, un-forced attempt with a warning body rather than an error, so
 * this screen treats `locked: false` as "here is what stands in the way" and offers a forced retry,
 * not as a failure. Every string is a translation key; dates and the month picker go through the
 * shared locale-aware helpers; the layout uses logical properties for right-to-left.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { listPeriods, lockPeriod, periodKeys, unlockPeriod } from '@/api/periods';
import { timeEntryKeys } from '@/api/timeEntries';
import type { PeriodLockResult, PeriodState, UnapprovedEntry } from '@/api/types';
import { Drawer } from '@/components/management/Drawer';
import { EmptyState, ErrorState, LoadingState } from '@/components/management/QueryState';
import { toError } from '@/lib/apiError';
import { formatDate } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';
import type { Language } from '@/i18n';

/** An em-dash placeholder, held in a value so the no-literal-strings lint rule is satisfied. */
const DASH = '—';

/** The current calendar year/month, the sensible default target for a lock. */
const now = () => {
  const date = new Date();
  return { year: date.getFullYear(), month: date.getMonth() + 1 };
};

/** A `YYYY-MM` value for the month picker, and the parse back to year/month. */
const toMonthValue = (year: number, month: number): string =>
  `${year}-${String(month).padStart(2, '0')}`;

const parseMonthValue = (value: string): { year: number; month: number } | null => {
  const match = /^(\d{4})-(\d{2})$/.exec(value);
  if (!match) {
    return null;
  }
  return { year: Number(match[1]), month: Number(match[2]) };
};

/** A month label like "August 2025", localised through the shared date formatter. */
const monthLabel = (language: Language, year: number, month: number): string => {
  const iso = `${year}-${String(month).padStart(2, '0')}-01`;
  return new Intl.DateTimeFormat(language === 'he' ? 'he-IL' : 'en-GB', {
    year: 'numeric',
    month: 'long',
  }).format(new Date(iso));
};

export function PeriodsPage() {
  const { t } = useTranslation();
  const language = useLanguage();
  const queryClient = useQueryClient();

  const initial = now();
  const [monthValue, setMonthValue] = useState(toMonthValue(initial.year, initial.month));
  // The warning list from an un-forced lock that found unapproved entries (Requirement 15.7).
  const [warning, setWarning] = useState<PeriodLockResult | null>(null);
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [errorParams, setErrorParams] = useState<Record<string, string>>({});
  // The month an administrator is unlocking, if any (Requirement 15.6).
  const [unlocking, setUnlocking] = useState<PeriodState | null>(null);

  const target = useMemo(() => parseMonthValue(monthValue), [monthValue]);

  const periods = useQuery({
    queryKey: periodKeys.list(),
    queryFn: listPeriods,
  });

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: periodKeys.all });
    await queryClient.invalidateQueries({ queryKey: timeEntryKeys.all });
  };

  const lockMutation = useMutation({
    mutationFn: (force: boolean) => {
      if (target === null) {
        return Promise.reject(new Error('no target month'));
      }
      return lockPeriod(target.year, target.month, { force });
    },
    onSuccess: async (result) => {
      if (result.locked) {
        setWarning(null);
        await invalidate();
      } else {
        // The month held unapproved entries and nothing was locked: show the warning list.
        setWarning(result);
      }
    },
    onError: (error) => {
      const translated = toError(error);
      setErrorKey(translated.key);
      setErrorParams(translated.params);
    },
  });

  const attemptLock = (force: boolean) => {
    setErrorKey(null);
    setErrorParams({});
    if (!force) {
      setWarning(null);
    }
    lockMutation.mutate(force);
  };

  return (
    <section className="page-body">
      <div className="page-header">
        <h1>{t('nav.periods')}</h1>
      </div>
      <p className="subtitle">{t('periods.subtitle')}</p>

      <div className="toolbar">
        <label className="field field--inline">
          <span className="field__label">{t('periods.month')}</span>
          <input
            className="input"
            type="month"
            value={monthValue}
            onChange={(event) => {
              setMonthValue(event.target.value);
              setWarning(null);
              setErrorKey(null);
            }}
          />
        </label>
        <button
          type="button"
          className="button button--primary"
          disabled={target === null || lockMutation.isPending}
          onClick={() => attemptLock(false)}
        >
          {t('periods.lock')}
        </button>
      </div>

      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey, errorParams)}
        </p>
      ) : null}

      {warning ? (
        <div className="feedback feedback--warn" role="alert">
          <p>{t('periods.warnUnapproved', { count: warning.unapproved.length })}</p>
          <UnapprovedList entries={warning.unapproved} language={language} />
          <div className="form__actions">
            <button type="button" className="button" onClick={() => setWarning(null)}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="button button--primary button--danger"
              disabled={lockMutation.isPending}
              onClick={() => attemptLock(true)}
            >
              {t('periods.lockAnyway')}
            </button>
          </div>
        </div>
      ) : null}

      <h2 className="section-title">{t('periods.history')}</h2>
      {periods.isPending ? (
        <LoadingState />
      ) : periods.isError ? (
        <ErrorState />
      ) : (periods.data?.items ?? []).length === 0 ? (
        <EmptyState messageKey="periods.none" />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{t('periods.month')}</th>
                <th>{t('common.status')}</th>
                <th>{t('periods.lockedAt')}</th>
                <th>{t('periods.unlockedAt')}</th>
                <th>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {(periods.data?.items ?? []).map((period) => (
                <tr key={`${period.year}-${period.month}`}>
                  <td>{monthLabel(language, period.year, period.month)}</td>
                  <td>
                    <span className={`pill ${period.is_locked ? 'pill--muted' : 'pill--active'}`}>
                      {t(period.is_locked ? 'periods.locked' : 'periods.open')}
                    </span>
                  </td>
                  <td>{period.locked_at ? formatDate(language, period.locked_at) : '—'}</td>
                  <td>{period.unlocked_at ? formatDate(language, period.unlocked_at) : '—'}</td>
                  <td>
                    {period.is_locked ? (
                      <button
                        type="button"
                        className="button button--small"
                        onClick={() => setUnlocking(period)}
                      >
                        {t('periods.unlock')}
                      </button>
                    ) : (
                      <span className="detail__label">{DASH}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {unlocking ? (
        <Drawer title={t('periods.unlockTitle')} onClose={() => setUnlocking(null)}>
          <UnlockPanel
            period={unlocking}
            language={language}
            onDone={async () => {
              setUnlocking(null);
              await invalidate();
            }}
            onCancel={() => setUnlocking(null)}
          />
        </Drawer>
      ) : null}
    </section>
  );
}

/** The entries standing in the way of a clean lock (Requirement 15.7). */
function UnapprovedList({
  entries,
  language,
}: {
  entries: UnapprovedEntry[];
  language: Language;
}) {
  const { t } = useTranslation();
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>{t('manualEntry.date')}</th>
            <th>{t('common.status')}</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr key={entry.id}>
              <td>{formatDate(language, entry.work_date)}</td>
              <td>{t(`timeEntryStatus.${entry.status}`)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Unlock a month with a mandatory reason (Requirement 15.6). */
function UnlockPanel({
  period,
  language,
  onDone,
  onCancel,
}: {
  period: PeriodState;
  language: Language;
  onDone: () => void | Promise<void>;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [reason, setReason] = useState('');
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [errorParams, setErrorParams] = useState<Record<string, string>>({});

  const mutation = useMutation({
    mutationFn: () => unlockPeriod(period.year, period.month, { reason: reason.trim() }),
    onSuccess: () => {
      void onDone();
    },
    onError: (error) => {
      const translated = toError(error);
      setErrorKey(translated.key);
      setErrorParams(translated.params);
    },
  });

  const onSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    setErrorKey(null);
    setErrorParams({});
    setFieldError(null);
    if (!reason.trim()) {
      setFieldError('manualEntry.reasonRequired');
      return;
    }
    mutation.mutate();
  };

  return (
    <form className="form" onSubmit={onSubmit}>
      <p>{t('periods.unlockPrompt', { month: monthLabel(language, period.year, period.month) })}</p>

      <label className="field">
        <span className="field__label">{t('manualEntry.reason')}</span>
        <textarea
          className="input"
          rows={2}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder={t('periods.unlockReasonPlaceholder')}
          required
        />
      </label>

      {fieldError ? (
        <p className="feedback feedback--error" role="alert">
          {t(fieldError)}
        </p>
      ) : null}
      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey, errorParams)}
        </p>
      ) : null}

      <div className="form__actions">
        <button type="button" className="button" onClick={onCancel}>
          {t('common.cancel')}
        </button>
        <button type="submit" className="button button--primary" disabled={mutation.isPending}>
          {mutation.isPending ? t('common.saving') : t('periods.confirmUnlock')}
        </button>
      </div>
    </form>
  );
}
