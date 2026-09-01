/**
 * Confirm a soft delete of a time entry, with a mandatory reason (Requirement 12.7, 12.3).
 *
 * A deletion is never a hard delete: the server sets `deleted_at` and `delete_reason` and retains the
 * row for audit. So the confirmation asks for a reason rather than a bare yes — a manager must say why
 * the shift is being removed, and a blank reason is refused before the request is sent (the server
 * refuses it too). The panel names the entry it will remove so the manager is confirming the right
 * one, and surfaces a server refusal — a locked month, for one — as a translated message rather than a
 * raw status.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

import { deleteTimeEntry, timeEntryKeys } from '@/api/timeEntries';
import type { TimeEntryListItem } from '@/api/types';
import { toError } from '@/lib/apiError';
import { formatDate, formatTime } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';

export function DeleteEntryPanel({
  entry,
  onDone,
  onCancel,
}: {
  entry: TimeEntryListItem;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const language = useLanguage();
  const queryClient = useQueryClient();

  const [reason, setReason] = useState('');
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [errorParams, setErrorParams] = useState<Record<string, string>>({});

  const mutation = useMutation({
    mutationFn: () => deleteTimeEntry(entry.id, { reason: reason.trim() }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: timeEntryKeys.all });
      onDone();
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
    if (!reason.trim()) {
      setFieldError('manualEntry.reasonRequired');
      return;
    }
    mutation.mutate();
  };

  const label =
    language === 'he' ? entry.employee_name : entry.employee_name_en;

  return (
    <form className="form" onSubmit={onSubmit}>
      <p>
        {t('manualEntry.deletePrompt', {
          employee: label,
          site: entry.site_name,
          date: formatDate(language, entry.work_date),
          from: formatTime(language, entry.check_in_at),
          to: entry.check_out_at ? formatTime(language, entry.check_out_at) : t('hours.stillOpen'),
        })}
      </p>

      <label className="field">
        <span className="field__label">{t('manualEntry.reason')}</span>
        <textarea
          className="input"
          rows={2}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
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
        <button
          type="submit"
          className="button button--primary button--danger"
          disabled={mutation.isPending}
        >
          {mutation.isPending ? t('common.saving') : t('manualEntry.confirmDelete')}
        </button>
      </div>
    </form>
  );
}
