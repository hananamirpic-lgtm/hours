// The Field labels screen of the Management_Interface (task 24.7; R19.1, R19.4, R19.5, R23.12). Config staff
// only: `RequireConfigStaff` in the route tree keeps a technician from ever mounting it.
//
// Seven fields, each with a Hebrew and an English label of 1 to 60 characters after trimming. There is no
// `GET /staff/labels`, so the current labels come from `readLabels()` (a `PUT` that names no label and so stores
// nothing). Save sends only the fields the user changed; the server checks every text before it stores any, so a
// refusal changes nothing in either language and the page keeps what was typed and marks each failing text.
// The query is not cached after the page closes and is not refetched on focus, so the editor always starts from
// the labels the server holds and a half-typed edit is never replaced under the user's hands.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { isStaffApiError, readLabels, staffKeys, updateLabels } from '../../api/staff';
import type { FormLabelKey, StaffLabels } from '../../api/staffTypes';
import { LabelsFormView, labelInputId } from '../configLabelsView';
import {
  NO_LABEL_ERRORS,
  changedLabels,
  firstFailingLabel,
  labelDrafts,
  labelFormErrors,
  withLabelText,
  withoutLabelError,
  type ConfigLanguage,
  type LabelDrafts,
  type LabelFormErrors,
} from '../configModel';
import { SERVICE_NAMESPACE } from '../namespace';
import '../config.css';

interface LabelsEditorProps {
  readonly initial: StaffLabels;
}

function LabelsEditor({ initial }: LabelsEditorProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const queryClient = useQueryClient();
  // The labels the server holds, as last read or saved. Changes are the drafts that differ from these.
  const [stored, setStored] = useState<LabelDrafts>(() => labelDrafts(initial));
  const [drafts, setDrafts] = useState<LabelDrafts>(() => labelDrafts(initial));
  const [errors, setErrors] = useState<LabelFormErrors>(NO_LABEL_ERRORS);
  const [notice, setNotice] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (changed: ReturnType<typeof changedLabels>) => updateLabels(changed),
    onSuccess: (saved) => {
      // The answer holds all seven labels, trimmed as the server stored them.
      queryClient.setQueryData(staffKeys.labels, saved);
      const next = labelDrafts(saved);
      setStored(next);
      setDrafts(next);
      setErrors(NO_LABEL_ERRORS);
      setNotice('management.settings.labels.saved');
    },
    onError: (error) => {
      setNotice(null);
      setErrors(isStaffApiError(error) ? labelFormErrors(error.code, error.fields) : labelFormErrors('generic'));
    },
  });

  // Focus moves to the first failing text after every refusal (R23.12). `errors` is a new object each time.
  useEffect(() => {
    const failing = firstFailingLabel(errors);
    if (failing !== null) document.getElementById(labelInputId(failing.key, failing.language))?.focus();
  }, [errors]);

  const onChange = (key: FormLabelKey, language: ConfigLanguage, value: string): void => {
    setDrafts((previous) => withLabelText(previous, key, language, value));
    setErrors((previous) => withoutLabelError(previous, key, language));
  };

  const onSubmit = (event: { preventDefault: () => void }): void => {
    event.preventDefault();
    if (mutation.isPending) return;
    const changed = changedLabels(drafts, stored);
    setErrors(NO_LABEL_ERRORS);
    if (Object.keys(changed).length === 0) {
      setNotice('management.settings.labels.noChanges');
      return;
    }
    setNotice(null);
    mutation.mutate(changed);
  };

  return (
    <>
      <div role="status" aria-live="polite" className="config-notice">
        {notice === null ? null : <p className="feedback feedback--success">{t(notice)}</p>}
      </div>
      <LabelsFormView drafts={drafts} errors={errors} pending={mutation.isPending} onChange={onChange} onSubmit={onSubmit} />
    </>
  );
}

export function LabelsPage() {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const query = useQuery({
    queryKey: staffKeys.labels,
    queryFn: () => readLabels(),
    gcTime: 0,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  return (
    <section className="page-body">
      <h1>{t('management.settings.labels.title')}</h1>
      <p className="subtitle">{t('management.settings.labels.subtitle')}</p>

      {query.isError ? (
        <div className="feedback feedback--error config-load-error" role="alert">
          <p>{t('management.settings.labels.loadError')}</p>
          <button type="button" className="button button--small" onClick={() => void query.refetch()}>
            {t('management.common.retry')}
          </button>
        </div>
      ) : null}

      {query.data === undefined && !query.isError ? (
        <p className="state" aria-busy="true">
          {t('management.common.loading')}
        </p>
      ) : null}

      {query.data === undefined ? null : <LabelsEditor initial={query.data} />}
    </section>
  );
}
