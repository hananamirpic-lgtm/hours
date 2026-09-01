/**
 * The manager hours view (Requirement 2.3, 2.4, 18.1, 22.3).
 *
 * A read of recorded shifts, laid out as a chronological per-employee day across sites. The server
 * scopes the list to the caller's sites — a site manager sees only the sites they run, an
 * administrator or accounting sees every site — so this screen applies no site restriction of its
 * own; it renders what the server returns. The filters (date range, employee, site, status, flag,
 * manual-only) are query parameters the list re-fetches on (Requirement 22.3).
 *
 * The flat, stable-sorted page is folded into per-employee days by `groupEntries`; each day shows its
 * entries grouped by site with a per-site subtotal and a daily total (Requirement 11.2 — the day
 * total is the sum of the per-site entries, never a single site's). Manual entries and anomalous ones
 * (an unassigned-site check-in, an implausible duration) carry a visible marker wherever they appear
 * (Requirement 12.4, 7.3, 10.5). Every string is a translation key and every date, time and duration
 * is formatted through the shared locale-aware helpers; the layout uses logical properties, so Hebrew
 * renders right-to-left with no change here.
 */

import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { employeeKeys, listEmployees } from '@/api/employees';
import { listSites, siteKeys } from '@/api/sites';
import { listTimeEntries, timeEntryKeys } from '@/api/timeEntries';
import type {
  TimeEntryFlag,
  TimeEntryListItem,
  TimeEntryStatus,
} from '@/api/types';
import { useAuth } from '@/auth/AuthProvider';
import { AuditPanel } from '@/app/audit/AuditPanel';
import { groupEntries, type EmployeeDay, type SiteGroup } from '@/app/hours/hoursView';
import { TimeEntryForm, type TimeEntryPrefill } from '@/app/hours/TimeEntryForm';
import { DeleteEntryPanel } from '@/app/hours/DeleteEntryPanel';
import { Drawer } from '@/components/management/Drawer';
import { Pagination } from '@/components/management/Pagination';
import { EmptyState, ErrorState, LoadingState } from '@/components/management/QueryState';
import { TimeEntryStatusPill } from '@/components/management/StatusPill';
import { formatDate, formatDuration, formatTime } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';
import type { Language } from '@/i18n';

const PAGE_SIZE = 50;
const OPTION_PAGE = 200;
const STATUSES: TimeEntryStatus[] = ['draft', 'review', 'approved', 'locked'];
const FLAGS: TimeEntryFlag[] = ['unassigned_site', 'implausible_duration'];

/**
 * Read prefill context from the URL query string, so a missing-report alert can link straight to the
 * manual-entry form with the employee, site and date it already knows filled in (Requirement 14.4,
 * 12.1). The alerting feature is Task 29; this reads the parameters it will pass. When any is present
 * the form opens in create mode on mount.
 */
const readPrefill = (): TimeEntryPrefill | null => {
  if (typeof window === 'undefined') {
    return null;
  }
  const params = new URLSearchParams(window.location.search);
  const employeeId = params.get('employee_id');
  const siteId = params.get('site_id');
  const workDate = params.get('work_date');
  if (!employeeId && !siteId && !workDate) {
    return null;
  }
  const prefill: TimeEntryPrefill = {};
  if (employeeId) {
    prefill.employeeId = employeeId;
  }
  if (siteId) {
    prefill.siteId = siteId;
  }
  if (workDate) {
    prefill.workDate = workDate;
  }
  return prefill;
};

export function HoursPage() {
  const { t } = useTranslation();
  const language = useLanguage();
  const { user } = useAuth();
  // Managers and administrators may create, correct and delete entries; the employee role may not,
  // and accounting reads but does not write (Requirement 12.6, 2.4). The server enforces this too;
  // this hides the controls a reader could not use.
  const canManage = user?.role === 'admin' || user?.role === 'site_manager';

  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [employeeId, setEmployeeId] = useState('');
  const [siteId, setSiteId] = useState('');
  const [status, setStatus] = useState<TimeEntryStatus | ''>('');
  const [flag, setFlag] = useState<TimeEntryFlag | ''>('');
  const [manualOnly, setManualOnly] = useState(false);
  const [offset, setOffset] = useState(0);

  // Write panels: creating a new entry (optionally prefilled from a missing-report alert), correcting
  // one, or confirming a soft delete. Only one is open at a time.
  const [creating, setCreating] = useState<TimeEntryPrefill | null>(null);
  const [correcting, setCorrecting] = useState<TimeEntryListItem | null>(null);
  const [deleting, setDeleting] = useState<TimeEntryListItem | null>(null);
  // The audit history of one entry, opened from its row (Requirement 13.4). Admins and site managers
  // may read it — the same set that may edit — so it rides on `canManage`; the server scopes it.
  const [viewingHistory, setViewingHistory] = useState<TimeEntryListItem | null>(null);

  // On first mount, open the create form prefilled if the URL carried a missing-report context.
  useEffect(() => {
    if (!canManage) {
      return;
    }
    const prefill = readPrefill();
    if (prefill) {
      setCreating(prefill);
    }
  }, [canManage]);

  // Any filter change returns to the first page: the old offset may point past the smaller result.
  const onFilterChange = <T,>(setter: (value: T) => void) => (value: T) => {
    setter(value);
    setOffset(0);
  };

  const params = useMemo(
    () => ({
      date_from: dateFrom || null,
      date_to: dateTo || null,
      employee_id: employeeId || null,
      site_id: siteId || null,
      status: status || null,
      flags: flag ? [flag] : [],
      manual_only: manualOnly,
      limit: PAGE_SIZE,
      offset,
    }),
    [dateFrom, dateTo, employeeId, siteId, status, flag, manualOnly, offset],
  );

  const query = useQuery({
    queryKey: timeEntryKeys.list(params),
    queryFn: () => listTimeEntries(params),
  });

  // The two select options. Scoped by the server the same way the list is, so a manager's dropdowns
  // only offer their own sites and (for employees) the whole active roster to filter within them.
  const employeeParams = { status: 'active' as const, limit: OPTION_PAGE, offset: 0 };
  const employees = useQuery({
    queryKey: employeeKeys.list(employeeParams),
    queryFn: () => listEmployees(employeeParams),
  });
  const siteParams = { status: null, limit: OPTION_PAGE, offset: 0 };
  const sites = useQuery({
    queryKey: siteKeys.list(siteParams),
    queryFn: () => listSites(siteParams),
  });

  const days = useMemo(() => groupEntries(query.data?.items ?? []), [query.data]);

  return (
    <section className="page-body">
      <div className="page-header">
        <h1>{t('nav.hours')}</h1>
        {canManage ? (
          <div className="page-header__actions">
            <button
              type="button"
              className="button button--primary"
              onClick={() => setCreating({})}
            >
              {t('manualEntry.new')}
            </button>
          </div>
        ) : null}
      </div>
      <p className="subtitle">{t('hours.subtitle')}</p>

      <div className="toolbar hours-filters">
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
          aria-label={t('hours.filterEmployee')}
          value={employeeId}
          onChange={(event) => onFilterChange(setEmployeeId)(event.target.value)}
        >
          <option value="">{t('hours.allEmployees')}</option>
          {(employees.data?.items ?? []).map((employee) => (
            <option key={employee.id} value={employee.id}>
              {employeeLabel(language, employee.full_name, employee.full_name_en)}
            </option>
          ))}
        </select>

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

        <select
          className="select"
          aria-label={t('hours.filterStatus')}
          value={status}
          onChange={(event) => onFilterChange(setStatus)(event.target.value as TimeEntryStatus | '')}
        >
          <option value="">{t('hours.allStatuses')}</option>
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              {t(`timeEntryStatus.${value}`)}
            </option>
          ))}
        </select>

        <select
          className="select"
          aria-label={t('hours.filterFlag')}
          value={flag}
          onChange={(event) => onFilterChange(setFlag)(event.target.value as TimeEntryFlag | '')}
        >
          <option value="">{t('hours.allFlags')}</option>
          {FLAGS.map((value) => (
            <option key={value} value={value}>
              {t(`timeEntryFlag.${value}`)}
            </option>
          ))}
        </select>

        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={manualOnly}
            onChange={(event) => onFilterChange(setManualOnly)(event.target.checked)}
          />
          <span>{t('hours.manualOnly')}</span>
        </label>
      </div>

      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState />
      ) : days.length === 0 ? (
        <EmptyState messageKey="hours.empty" />
      ) : (
        <div className="hours-days">
          {days.map((day) => (
            <DayCard
              key={day.key}
              day={day}
              language={language}
              canManage={canManage}
              onCorrect={setCorrecting}
              onDelete={setDeleting}
              onHistory={setViewingHistory}
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

      {creating ? (
        <Drawer title={t('manualEntry.new')} onClose={() => setCreating(null)}>
          <TimeEntryForm
            prefill={creating}
            onDone={() => setCreating(null)}
            onCancel={() => setCreating(null)}
          />
        </Drawer>
      ) : null}

      {correcting ? (
        <Drawer title={t('manualEntry.correct')} onClose={() => setCorrecting(null)}>
          <TimeEntryForm
            entry={correcting}
            onDone={() => setCorrecting(null)}
            onCancel={() => setCorrecting(null)}
          />
        </Drawer>
      ) : null}

      {deleting ? (
        <Drawer title={t('manualEntry.deleteTitle')} onClose={() => setDeleting(null)}>
          <DeleteEntryPanel
            entry={deleting}
            onDone={() => setDeleting(null)}
            onCancel={() => setDeleting(null)}
          />
        </Drawer>
      ) : null}

      {viewingHistory ? (
        <Drawer title={t('audit.title')} onClose={() => setViewingHistory(null)}>
          <AuditPanel entityType="time_entries" entityId={viewingHistory.id} />
        </Drawer>
      ) : null}
    </section>
  );
}

/** An option label: the reader's-language name first, the other in parentheses to disambiguate. */
const employeeLabel = (language: Language, name: string, nameEn: string): string => {
  const primary = language === 'he' ? name : nameEn;
  const secondary = language === 'he' ? nameEn : name;
  return primary === secondary ? primary : `${primary} (${secondary})`;
};

interface RowActions {
  canManage: boolean;
  onCorrect: (entry: TimeEntryListItem) => void;
  onDelete: (entry: TimeEntryListItem) => void;
  onHistory: (entry: TimeEntryListItem) => void;
}

/** One employee's day: a header with the name, date and total, then a block per site. */
function DayCard({
  day,
  language,
  canManage,
  onCorrect,
  onDelete,
  onHistory,
}: { day: EmployeeDay; language: Language } & RowActions) {
  const { t } = useTranslation();
  return (
    <article className="hours-day">
      <header className="hours-day__header">
        <div className="hours-day__who">
          <span className="hours-day__name">
            {employeeLabel(language, day.employeeName, day.employeeNameEn)}
          </span>
          <span className="hours-day__date">{formatDate(language, day.workDate)}</span>
          {day.siteCount > 1 ? (
            <span className="tag">{t('hours.multiSite', { count: day.siteCount })}</span>
          ) : null}
        </div>
        <div className="hours-day__total">
          <span className="detail__label">{t('hours.dayTotal')}</span>
          <span className="numeric hours-day__total-value">{formatDuration(day.totalMinutes)}</span>
          {day.hasOpenShift ? <span className="tag tag--warn">{t('hours.openShift')}</span> : null}
        </div>
      </header>

      {day.siteGroups.map((group) => (
        <SiteBlock
          key={group.siteId}
          group={group}
          language={language}
          canManage={canManage}
          onCorrect={onCorrect}
          onDelete={onDelete}
          onHistory={onHistory}
        />
      ))}
    </article>
  );
}

/** One site's entries within a day, with the per-site subtotal. */
function SiteBlock({
  group,
  language,
  canManage,
  onCorrect,
  onDelete,
  onHistory,
}: { group: SiteGroup; language: Language } & RowActions) {
  const { t } = useTranslation();
  return (
    <div className="hours-site">
      <div className="hours-site__header">
        <span className="hours-site__name">{group.siteName}</span>
        <span className="numeric hours-site__subtotal">
          {t('hours.siteSubtotal')} {formatDuration(group.subtotalMinutes)}
        </span>
      </div>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>{t('hours.checkIn')}</th>
              <th>{t('hours.checkOut')}</th>
              <th>{t('hours.duration')}</th>
              <th>{t('common.status')}</th>
              <th>{t('hours.markers')}</th>
              {canManage ? <th>{t('common.actions')}</th> : null}
            </tr>
          </thead>
          <tbody>
            {group.entries.map((entry) => (
              <EntryRow
                key={entry.id}
                entry={entry}
                language={language}
                canManage={canManage}
                onCorrect={onCorrect}
                onDelete={onDelete}
                onHistory={onHistory}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** One time entry as a row: its times, duration, status and any manual or anomaly marker. */
function EntryRow({
  entry,
  language,
  canManage,
  onCorrect,
  onDelete,
  onHistory,
}: { entry: TimeEntryListItem; language: Language } & RowActions) {
  const { t } = useTranslation();
  // A locked entry is frozen: the month it belongs to has been closed, so it cannot be corrected or
  // deleted except by an admin override (Requirement 15.5), which is a period-screen action, not a
  // row one. Hiding the buttons keeps the row honest about what it will let a manager do.
  const editable = entry.status !== 'locked';
  return (
    <tr>
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
      <td>
        <span className="hours-markers">
          {entry.is_manual ? (
            <span className="tag tag--manual" title={t('hours.manualHint')}>
              {t('hours.manual')}
            </span>
          ) : null}
          {entry.flags.map((name) => (
            <span key={name} className="tag tag--anomaly" title={t('hours.anomalyHint')}>
              {t(`timeEntryFlag.${name}`, { defaultValue: name })}
            </span>
          ))}
        </span>
      </td>
      {canManage ? (
        <td>
          <span className="row-actions">
            {editable ? (
              <>
                <button
                  type="button"
                  className="button button--small"
                  onClick={() => onCorrect(entry)}
                >
                  {t('common.edit')}
                </button>
                <button
                  type="button"
                  className="button button--small button--danger"
                  onClick={() => onDelete(entry)}
                >
                  {t('common.delete')}
                </button>
              </>
            ) : (
              <span className="detail__label">{t('manualEntry.lockedNoEdit')}</span>
            )}
            {/* The history is readable whatever the status: a locked entry's audit trail is exactly
                what a dispute over it needs (Requirement 13.4). */}
            <button
              type="button"
              className="button button--small"
              onClick={() => onHistory(entry)}
            >
              {t('audit.tab')}
            </button>
          </span>
        </td>
      ) : null}
    </tr>
  );
}
