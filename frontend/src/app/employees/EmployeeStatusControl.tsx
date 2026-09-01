/**
 * Change an employee's status (Requirement 3.4, 3.8). There is no delete: deactivation is a status
 * change, and the record and its history remain. A reason is optional but recorded, so it is offered
 * whenever a status other than the current one is chosen. Terminated is a terminal state the server
 * will not let you leave, which the API enforces and this surfaces as an error if attempted.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { changeEmployeeStatus, employeeKeys } from '@/api/employees';
import type { EmployeeResponse, EmployeeStatus } from '@/api/types';
import { toError } from '@/lib/apiError';

const STATUSES: EmployeeStatus[] = ['active', 'on_leave', 'inactive', 'terminated'];

export function EmployeeStatusControl({
  employee,
  onDone,
}: {
  employee: EmployeeResponse;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<EmployeeStatus>(employee.status);
  const [reason, setReason] = useState('');
  const [errorKey, setErrorKey] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => changeEmployeeStatus(employee.id, status, reason || undefined),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: employeeKeys.all });
      setOpen(false);
      onDone();
    },
    onError: (error) => setErrorKey(toError(error).key),
  });

  if (!open) {
    return (
      <button type="button" className="button button--small" onClick={() => setOpen(true)}>
        {t('employee.changeStatus')}
      </button>
    );
  }

  return (
    <div className="drawer__section" style={{ inlineSize: '100%' }}>
      <h4 className="drawer__section-title">{t('employee.changeStatus')}</h4>
      <label className="field">
        <span className="field__label">{t('common.status')}</span>
        <select
          className="select"
          value={status}
          onChange={(event) => setStatus(event.target.value as EmployeeStatus)}
        >
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              {t(`employeeStatus.${value}`)}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span className="field__label">{t('employee.statusReason')}</span>
        <input className="input" value={reason} onChange={(event) => setReason(event.target.value)} />
      </label>
      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey)}
        </p>
      ) : null}
      <div className="form__actions">
        <button type="button" className="button button--small" onClick={() => setOpen(false)}>
          {t('common.cancel')}
        </button>
        <button
          type="button"
          className="button button--small button--primary"
          disabled={mutation.isPending || status === employee.status}
          onClick={() => {
            setErrorKey(null);
            mutation.mutate();
          }}
        >
          {mutation.isPending ? t('common.saving') : t('common.save')}
        </button>
      </div>
    </div>
  );
}
