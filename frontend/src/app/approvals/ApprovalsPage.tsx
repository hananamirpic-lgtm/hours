/**
 * The approval screen (Requirement 15.1–15.3).
 *
 * With location verification out of scope, manager approval is the principal control over attendance
 * accuracy, so this screen carries real weight. A manager reads the recorded shifts for their sites —
 * the server scopes the list, so no site restriction is applied here — and advances them along the
 * ladder Draft → Review → Approved (Requirement 15.2, 15.3). The entries are grouped two ways the
 * reader can toggle between: **by site** (then by employee within it), because a manager runs sites
 * and approving a site's hours is the natural unit; and **by employee** across sites, for a reviewer
 * working down a roster. The grouping is pure (`approvalView`) and the component is a thin
 * presentation over it.
 *
 * A bulk action sends only the entries a single forward step is legal for (`advanceableIds`), so
 * "approve" on a group of mixed Review and Draft entries advances the Review ones and leaves the
 * Draft ones for their own step — the server would otherwise reject the whole call for an illegal
 * jump (Requirement 15.2). An administrator additionally sees a per-entry reversal control that steps
 * a status back one rung with a mandatory reason, recorded in the audit log (Requirement 15.6).
 *
 * Every string is a translation key and every date, time and duration goes through the shared
 * locale-aware helpers; the layout uses logical properties, so Hebrew renders right-to-left with no
 * change here.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { bulkChangeStatus, periodKeys } from '@/api/periods';
import { listSites, siteKeys } from '@/api/sites';
import { listTimeEntries, timeEntryKeys } from '@/api/timeEntries';
import type { TimeEntryListItem, TimeEntryStatus } from '@/api/types';
import { useAuth } from '@/auth/AuthProvider';
import {
  advanceableIds,
  groupByEmployee,
  groupBySite,
  type EmployeeApprovalGroup,
  type SiteApprovalGroup,
  type SiteEmployeeGroup,
  type StatusTally,
} from '@/app/approvals/approvalView';
import { Drawer } from '@/components/management/Drawer';
import { Pagination } from '@/components/management/Pagination';
import { EmptyState, ErrorState, LoadingState } from '@/components/management/QueryState';
import { TimeEntryStatusPill } from '@/components/management/StatusPill';
import { toError } from '@/lib/apiError';
import { formatDate, formatDuration, formatTime } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';
import type { Language } from '@/i18n';

const PAGE_SIZE = 100;
const OPTION_PAGE = 200;
/** An em-dash placeholder, held in a value so the no-literal-strings lint rule is satisfied. */
const DASH = '—';
type Grouping = 'site' | 'employee';

/** An option/label: the reader's-language name first, the other in parentheses to disambiguate. */
const employeeLabel = (language: Language, name: string, nameEn: string): string => {
  const primary = language === 'he' ? name : nameEn;
  const secondary = language === 'he' ? nameEn : name;
  return primary === secondary ? primary : `${primary} (${secondary})`;
};

export function ApprovalsPage() {
  const { t } = useTranslation();
  const language = useLanguage();
  const { user } = useAuth();
  const isAdmin = user?.role === 'admin';

  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [siteId, setSiteId] = useState('');
  const [grouping, setGrouping] = useState<Grouping>('site');
  const [offset, setOffset] = useState(0);
  // The entry an administrator is reversing, if any (Requirement 15.6).
  const [reversing, setReversing] = useState<TimeEntryListItem | null>(null);

  const onFilterChange = <T,>(setter: (value: T) => void) => (value: T) => {
    setter(value);
    setOffset(0);
  };

  const params = useMemo(
    () => ({
      date_from: dateFrom || null,
      date_to: dateTo || null,
      site_id: siteId || null,
      // The screen approves reported hours; a still-open shift is not ready to approve, so listing
      // completed and in-flight entries is enough. No status filter — every rung is shown so a
      // manager sees what is left to do.
      limit: PAGE_SIZE,
      offset,
    }),
    [dateFrom, dateTo, siteId, offset],
  );

  const query = useQuery({
    queryKey: timeEntryKeys.list(params),
    queryFn: () => listTimeEntries(params),
  });

  const siteParams = { status: null, limit: OPTION_PAGE, offset: 0 };
  const sites = useQuery({
    queryKey: siteKeys.list(siteParams),
    queryFn: () => listSites(siteParams),
  });

  const items = useMemo(() => query.data?.items ?? [], [query.data]);
  const bySite = useMemo(() => groupBySite(items), [items]);
  const byEmployee = useMemo(() => groupByEmployee(items), [items]);

  return (
    <section className="page-body">
      <div className="page-header">
        <h1>{t('nav.approvals')}</h1>
      </div>
      <p className="subtitle">{t('approvals.subtitle')}</p>

      <div className="toolbar">
        <label className="field field--inline">
          <span className="field__label">{t('hours.dateFrom')}</span>
          <input
            className="input"
            type="date"
            value={dateFrom}
            onChange={(event) => onFilterChange(setDateFrom)(event.target.value)}
          />
        </label>
        <label className="field field--inline">
          <span className="field__label">{t('hours.dateTo')}</span>
          <input
            className="input"
            type="date"
            value={dateTo}
            onChange={(event) => onFilterChange(setDateTo)(event.target.value)}
          />
        </label>

        <select
          className="select"
          aria-label={t('hours.filterSite')}
          value={siteId}
          onChange={(event) => onFilterChange(setSiteId)(event.target.value)}
        >
          <option value="">{t('hours.allSites')}</option>
          {(sites.data?.items ?? []).map((site) => (
            <option key={site.id} value={site.id}>
              {site.name}
            </option>
          ))}
        </select>

        <div className="segmented" role="group" aria-label={t('approvals.groupBy')}>
          <button
            type="button"
            className={`button button--small ${grouping === 'site' ? 'button--primary' : ''}`}
            aria-pressed={grouping === 'site'}
            onClick={() => setGrouping('site')}
          >
            {t('approvals.bySite')}
          </button>
          <button
            type="button"
            className={`button button--small ${grouping === 'employee' ? 'button--primary' : ''}`}
            aria-pressed={grouping === 'employee'}
            onClick={() => setGrouping('employee')}
          >
            {t('approvals.byEmployee')}
          </button>
        </div>
      </div>

      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState />
      ) : items.length === 0 ? (
        <EmptyState messageKey="approvals.empty" />
      ) : grouping === 'site' ? (
        <div className="approval-groups">
          {bySite.map((site) => (
            <SiteGroupCard
              key={site.siteId}
              group={site}
              language={language}
              isAdmin={isAdmin}
              onReverse={setReversing}
            />
          ))}
        </div>
      ) : (
        <div className="approval-groups">
          {byEmployee.map((employee) => (
            <EmployeeGroupCard
              key={employee.employeeId}
              group={employee}
              language={language}
              isAdmin={isAdmin}
              onReverse={setReversing}
            />
          ))}
        </div>
      )}

      {query.data ? (
        <Pagination
          total={query.data.total}
          limit={PAGE_SIZE}
          offset={offset}
          onOffsetChange={setOffset}
        />
      ) : null}

      {reversing ? (
        <Drawer title={t('approvals.reverseTitle')} onClose={() => setReversing(null)}>
          <ReversePanel
            entry={reversing}
            language={language}
            onDone={() => setReversing(null)}
            onCancel={() => setReversing(null)}
          />
        </Drawer>
      ) : null}
    </section>
  );
}

/** A compact read-out of a group's status tally, so the reader sees what is left to approve. */
function TallyBadges({ tally }: { tally: StatusTally }) {
  const { t } = useTranslation();
  const parts: Array<[TimeEntryStatus, number]> = [
    ['draft', tally.draft],
    ['review', tally.review],
    ['approved', tally.approved],
    ['locked', tally.locked],
  ];
  return (
    <span className="approval-tally">
      {parts
        .filter(([, count]) => count > 0)
        .map(([status, count]) => (
          <span key={status} className="tag">
            {t('approvals.tally', { status: t(`timeEntryStatus.${status}`), count })}
          </span>
        ))}
    </span>
  );
}

/** The forward-step buttons for a set of entries: advance the eligible ones to Review or Approved. */
function AdvanceActions({ entries }: { entries: TimeEntryListItem[] }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [errorParams, setErrorParams] = useState<Record<string, string>>({});

  const mutation = useMutation({
    mutationFn: (target: TimeEntryStatus) =>
      bulkChangeStatus({ entry_ids: advanceableIds(entries, target), target_status: target }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: timeEntryKeys.all });
      await queryClient.invalidateQueries({ queryKey: periodKeys.all });
    },
    onError: (error) => {
      const translated = toError(error);
      setErrorKey(translated.key);
      setErrorParams(translated.params);
    },
  });

  const toReview = advanceableIds(entries, 'review');
  const toApproved = advanceableIds(entries, 'approved');

  const run = (target: TimeEntryStatus) => {
    setErrorKey(null);
    setErrorParams({});
    mutation.mutate(target);
  };

  return (
    <div className="approval-actions">
      <button
        type="button"
        className="button button--small"
        disabled={toReview.length === 0 || mutation.isPending}
        onClick={() => run('review')}
      >
        {t('approvals.toReview', { count: toReview.length })}
      </button>
      <button
        type="button"
        className="button button--small button--primary"
        disabled={toApproved.length === 0 || mutation.isPending}
        onClick={() => run('approved')}
      >
        {t('approvals.toApproved', { count: toApproved.length })}
      </button>
      {errorKey ? (
        <span className="feedback feedback--error" role="alert">
          {t(errorKey, errorParams)}
        </span>
      ) : null}
    </div>
  );
}

interface GroupCardProps {
  language: Language;
  isAdmin: boolean;
  onReverse: (entry: TimeEntryListItem) => void;
}

/** One site: its per-employee sub-groups, a site-wide tally, and a site-wide advance action. */
function SiteGroupCard({
  group,
  language,
  isAdmin,
  onReverse,
}: { group: SiteApprovalGroup } & GroupCardProps) {
  return (
    <article className="approval-group">
      <header className="approval-group__header">
        <span className="approval-group__title">{group.siteName}</span>
        <TallyBadges tally={group.tally} />
        <AdvanceActions entries={group.entries} />
      </header>
      {group.employees.map((employee) => (
        <EmployeeBlock
          key={employee.employeeId}
          employee={employee}
          language={language}
          isAdmin={isAdmin}
          onReverse={onReverse}
        />
      ))}
    </article>
  );
}

/** One employee within a site: their entries and an advance action for just this person's hours. */
function EmployeeBlock({
  employee,
  language,
  isAdmin,
  onReverse,
}: { employee: SiteEmployeeGroup } & GroupCardProps) {
  return (
    <div className="approval-employee">
      <div className="approval-employee__header">
        <span className="approval-employee__name">
          {employeeLabel(language, employee.employeeName, employee.employeeNameEn)}
        </span>
        <TallyBadges tally={employee.tally} />
        <AdvanceActions entries={employee.entries} />
      </div>
      <EntryTable entries={employee.entries} language={language} isAdmin={isAdmin} onReverse={onReverse} />
    </div>
  );
}

/** One employee across sites: their entries and an advance action for the whole set. */
function EmployeeGroupCard({
  group,
  language,
  isAdmin,
  onReverse,
}: { group: EmployeeApprovalGroup } & GroupCardProps) {
  return (
    <article className="approval-group">
      <header className="approval-group__header">
        <span className="approval-group__title">
          {employeeLabel(language, group.employeeName, group.employeeNameEn)}
        </span>
        <TallyBadges tally={group.tally} />
        <AdvanceActions entries={group.entries} />
      </header>
      <EntryTable entries={group.entries} language={language} isAdmin={isAdmin} onReverse={onReverse} />
    </article>
  );
}

/** A table of entries: site, day, times, duration, status, and (for an admin) a reversal button. */
function EntryTable({
  entries,
  language,
  isAdmin,
  onReverse,
}: {
  entries: TimeEntryListItem[];
  language: Language;
  isAdmin: boolean;
  onReverse: (entry: TimeEntryListItem) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>{t('manualEntry.site')}</th>
            <th>{t('manualEntry.date')}</th>
            <th>{t('hours.checkIn')}</th>
            <th>{t('hours.checkOut')}</th>
            <th>{t('hours.duration')}</th>
            <th>{t('common.status')}</th>
            {isAdmin ? <th>{t('common.actions')}</th> : null}
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr key={entry.id}>
              <td>{entry.site_name}</td>
              <td>{formatDate(language, entry.work_date)}</td>
              <td className="numeric">{formatTime(language, entry.check_in_at)}</td>
              <td className="numeric">
                {entry.check_out_at ? formatTime(language, entry.check_out_at) : t('hours.stillOpen')}
              </td>
              <td className="numeric">
                {entry.total_minutes === null ? '—' : formatDuration(entry.total_minutes)}
              </td>
              <td>
                <TimeEntryStatusPill status={entry.status} />
              </td>
              {isAdmin ? (
                <td>
                  {/* A reversal steps a status back one rung; Draft has nowhere to go back to. */}
                  {entry.status === 'draft' ? (
                    <span className="detail__label">{DASH}</span>
                  ) : (
                    <button
                      type="button"
                      className="button button--small"
                      onClick={() => onReverse(entry)}
                    >
                      {t('approvals.reverse')}
                    </button>
                  )}
                </td>
              ) : null}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The rung one step below `status`, or null for Draft (nothing below it). */
const previousStatus = (status: TimeEntryStatus): TimeEntryStatus | null => {
  const ladder: TimeEntryStatus[] = ['draft', 'review', 'approved', 'locked'];
  const rank = ladder.indexOf(status);
  return rank > 0 ? ladder[rank - 1] : null;
};

/**
 * The administrator's reversal panel (Requirement 15.6): step one entry back one rung with a
 * mandatory reason, recorded in the audit log. A blank reason is refused before the request is sent;
 * the server refuses it too.
 */
function ReversePanel({
  entry,
  language,
  onDone,
  onCancel,
}: {
  entry: TimeEntryListItem;
  language: Language;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const target = previousStatus(entry.status);

  const [reason, setReason] = useState('');
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [errorParams, setErrorParams] = useState<Record<string, string>>({});

  const mutation = useMutation({
    mutationFn: () =>
      bulkChangeStatus({
        entry_ids: [entry.id],
        target_status: target as TimeEntryStatus,
        reason: reason.trim(),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: timeEntryKeys.all });
      await queryClient.invalidateQueries({ queryKey: periodKeys.all });
      onDone();
    },
    onError: (error) => {
      const translated = toError(error);
      setErrorKey(translated.key);
      setErrorParams(translated.params);
    },
  });

  if (target === null) {
    return <p>{t('approvals.cannotReverseDraft')}</p>;
  }

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
      <p>
        {t('approvals.reversePrompt', {
          employee: employeeLabel(language, entry.employee_name, entry.employee_name_en),
          site: entry.site_name,
          date: formatDate(language, entry.work_date),
          from: t(`timeEntryStatus.${entry.status}`),
          to: t(`timeEntryStatus.${target}`),
        })}
      </p>

      <label className="field">
        <span className="field__label">{t('manualEntry.reason')}</span>
        <textarea
          className="input"
          rows={2}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder={t('approvals.reasonPlaceholder')}
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
          {mutation.isPending ? t('common.saving') : t('approvals.confirmReverse')}
        </button>
      </div>
    </form>
  );
}
