/**
 * Manual time entry and correction (Requirement 12.1–12.5).
 *
 * One form for two shapes. In *create* mode a manager or administrator records a completed shift by
 * hand when a scan failed: they pick the employee and site, state the date and the check-in and
 * check-out times, and give a reason (Requirement 12.1). In *correct* mode the employee, site and
 * date are fixed — only the times and the reason are editable — and the edit marks the entry manual
 * wherever it appears (Requirement 12.4). The reason is mandatory in both, and a blank one is refused
 * before the request is sent so the failure reads at the field rather than as a server error
 * (Requirement 12.3).
 *
 * The form is reachable from the hours view's "add manual entry" action and from a missing-report
 * alert, which prefills the employee, site and date it already knows so the manager only supplies the
 * times and the reason (the missing-report detection itself is Task 29; this form accepts the context
 * through `prefill`). The server applies the same overlap, plausibility and period-lock rules a
 * scanned entry obeys (Requirement 12.5); an overlap comes back a 409 that names the conflicting
 * entry, which this surfaces so the manager can find the shift they collided with.
 *
 * Times are entered as a canonical date plus wall-clock times in the reader's own timezone, and
 * combined into UTC ISO instants for the API — the reverse of how the hours view renders them — so a
 * manager types "08:00" and the server records the instant that was 08:00 locally. Every string is a
 * translation key and the layout uses logical properties, so Hebrew renders right-to-left unchanged.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

import { employeeKeys, listEmployees } from '@/api/employees';
import { listSites, siteKeys } from '@/api/sites';
import {
  correctTimeEntry,
  createTimeEntry,
  timeEntryKeys,
} from '@/api/timeEntries';
import type { TimeEntryListItem, TimeEntryResponse } from '@/api/types';
import { toError } from '@/lib/apiError';
import { toCanonicalDate } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';
import type { Language } from '@/i18n';

const OPTION_PAGE = 200;

/** Context a caller may prefill, e.g. from a missing-report alert (Requirement 14.4). */
export interface TimeEntryPrefill {
  employeeId?: string;
  siteId?: string;
  /** Canonical `YYYY-MM-DD`. */
  workDate?: string;
}

interface Fields {
  employeeId: string;
  siteId: string;
  date: string;
  checkInTime: string;
  checkOutTime: string;
  reason: string;
}

const emptyFields = (prefill?: TimeEntryPrefill): Fields => ({
  employeeId: prefill?.employeeId ?? '',
  siteId: prefill?.siteId ?? '',
  date: prefill?.workDate ?? toCanonicalDate(new Date()),
  checkInTime: '',
  checkOutTime: '',
  reason: '',
});

/** Split a UTC ISO instant into its local date and `HH:MM` time, for editing an existing entry. */
const splitLocal = (iso: string): { date: string; time: string } => {
  const value = new Date(iso);
  const hours = value.getHours().toString().padStart(2, '0');
  const minutes = value.getMinutes().toString().padStart(2, '0');
  return { date: toCanonicalDate(value), time: `${hours}:${minutes}` };
};

const fromEntry = (entry: TimeEntryListItem): Fields => {
  const checkIn = splitLocal(entry.check_in_at);
  const checkOut = entry.check_out_at ? splitLocal(entry.check_out_at) : { time: '' };
  return {
    employeeId: entry.employee_id,
    siteId: entry.site_id,
    date: checkIn.date,
    checkInTime: checkIn.time,
    checkOutTime: checkOut.time,
    reason: '',
  };
};

/** Combine a canonical date and a wall-clock `HH:MM`, both local, into a UTC ISO instant. */
const toIsoInstant = (date: string, time: string): string => {
  // `new Date('YYYY-MM-DDTHH:MM')` is parsed in the browser's local timezone; `toISOString` converts
  // to UTC, which is what the API stores. This is the inverse of how the hours view formats a time.
  return new Date(`${date}T${time}`).toISOString();
};

const employeeLabel = (
  language: Language,
  name: string,
  nameEn: string,
  employeeNumber?: string | null,
): string => {
  const primary = language === 'he' ? name : nameEn;
  const secondary = language === 'he' ? nameEn : name;
  const base = primary === secondary ? primary : `${primary} (${secondary})`;
  return employeeNumber ? `${base} #${employeeNumber}` : base;
};

export function TimeEntryForm({
  entry,
  prefill,
  onDone,
  onCancel,
}: {
  /** The entry to correct; when absent the form creates a new manual entry. */
  entry?: TimeEntryListItem;
  prefill?: TimeEntryPrefill;
  onDone: (saved: TimeEntryResponse) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const language = useLanguage();
  const queryClient = useQueryClient();
  const correcting = entry !== undefined;

  const [fields, setFields] = useState<Fields>(entry ? fromEntry(entry) : emptyFields(prefill));
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [errorParams, setErrorParams] = useState<Record<string, string>>({});
  const [fieldError, setFieldError] = useState<string | null>(null);

  const set = (key: keyof Fields) => (event: { target: { value: string } }) =>
    setFields((prev) => ({ ...prev, [key]: event.target.value }));

  // The two select sources, scoped by the server the same way the list is: a manager's dropdowns
  // only offer their own sites. Fetched only when creating — a correction fixes the employee and
  // site, and shows them read-only.
  const employeeParams = { status: 'active' as const, limit: OPTION_PAGE, offset: 0 };
  const employees = useQuery({
    queryKey: employeeKeys.list(employeeParams),
    queryFn: () => listEmployees(employeeParams),
    enabled: !correcting,
  });
  const siteParams = { status: null, limit: OPTION_PAGE, offset: 0 };
  const sites = useQuery({
    queryKey: siteKeys.list(siteParams),
    queryFn: () => listSites(siteParams),
    enabled: !correcting,
  });

  const entryLabels = useMemo(() => {
    if (!entry) {
      return null;
    }
    return {
      employee: employeeLabel(language, entry.employee_name, entry.employee_name_en, entry.employee_number),
      site: entry.site_name,
    };
  }, [entry, language]);

  const mutation = useMutation({
    mutationFn: async (): Promise<TimeEntryResponse> => {
      if (correcting && entry) {
        return correctTimeEntry(entry.id, {
          check_in_at: toIsoInstant(fields.date, fields.checkInTime),
          check_out_at: toIsoInstant(fields.date, fields.checkOutTime),
          reason: fields.reason.trim(),
        });
      }
      return createTimeEntry({
        employee_id: fields.employeeId,
        site_id: fields.siteId,
        check_in_at: toIsoInstant(fields.date, fields.checkInTime),
        check_out_at: toIsoInstant(fields.date, fields.checkOutTime),
        reason: fields.reason.trim(),
      });
    },
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: timeEntryKeys.all });
      onDone(saved);
    },
    onError: (error) => {
      const translated = toError(error);
      setErrorKey(translated.key);
      setErrorParams(translated.params);
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    setErrorKey(null);
    setErrorParams({});
    setFieldError(null);

    // A blank reason is refused before the request (Requirement 12.3); the server would reject it too,
    // but catching it here reads at the field rather than as a 422.
    if (!fields.reason.trim()) {
      setFieldError('manualEntry.reasonRequired');
      return;
    }
    if (!correcting && (!fields.employeeId || !fields.siteId)) {
      setFieldError('manualEntry.employeeSiteRequired');
      return;
    }
    if (!fields.checkInTime || !fields.checkOutTime) {
      setFieldError('manualEntry.timesRequired');
      return;
    }
    if (toIsoInstant(fields.date, fields.checkOutTime) <= toIsoInstant(fields.date, fields.checkInTime)) {
      setFieldError('manualEntry.checkOutAfterCheckIn');
      return;
    }
    mutation.mutate();
  };

  return (
    <form className="form" onSubmit={onSubmit}>
      {correcting && entryLabels ? (
        <div className="form-row">
          <div className="field">
            <span className="field__label">{t('manualEntry.employee')}</span>
            <span className="field__static">{entryLabels.employee}</span>
          </div>
          <div className="field">
            <span className="field__label">{t('manualEntry.site')}</span>
            <span className="field__static">{entryLabels.site}</span>
          </div>
        </div>
      ) : (
        <div className="form-row">
          <label className="field">
            <span className="field__label">{t('manualEntry.employee')}</span>
            <select
              className="select"
              value={fields.employeeId}
              onChange={set('employeeId')}
              required
            >
              <option value="">{t('manualEntry.selectEmployee')}</option>
              {(employees.data?.items ?? []).map((employee) => (
                <option key={employee.id} value={employee.id}>
                  {employeeLabel(language, employee.full_name, employee.full_name_en)}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field__label">{t('manualEntry.site')}</span>
            <select className="select" value={fields.siteId} onChange={set('siteId')} required>
              <option value="">{t('manualEntry.selectSite')}</option>
              {(sites.data?.items ?? []).map((site) => (
                <option key={site.id} value={site.id}>
                  {site.name}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}

      <label className="field">
        <span className="field__label">{t('manualEntry.date')}</span>
        <input className="input" type="date" value={fields.date} onChange={set('date')} required />
      </label>

      <div className="form-row">
        <label className="field">
          <span className="field__label">{t('manualEntry.checkInTime')}</span>
          <input
            className="input"
            type="time"
            value={fields.checkInTime}
            onChange={set('checkInTime')}
            required
          />
        </label>
        <label className="field">
          <span className="field__label">{t('manualEntry.checkOutTime')}</span>
          <input
            className="input"
            type="time"
            value={fields.checkOutTime}
            onChange={set('checkOutTime')}
            required
          />
        </label>
      </div>

      <label className="field">
        <span className="field__label">{t('manualEntry.reason')}</span>
        <textarea
          className="input"
          rows={2}
          value={fields.reason}
          onChange={set('reason')}
          placeholder={t('manualEntry.reasonPlaceholder')}
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
          {mutation.isPending ? t('common.saving') : t('common.save')}
        </button>
      </div>
    </form>
  );
}
