// The Form options screen of the Management_Interface (task 24.7; R19.1, R19.2, R19.3, R23.12). Config staff
// only: `RequireConfigStaff` in the route tree keeps a technician from ever mounting it.
//
// Two options: Description required and Photo upload enabled. Save sends both together as booleans; a change
// applies to tickets sent after it and existing tickets stay as they are. The query is not cached after the
// page closes and is not refetched on focus, so the checkboxes always start from the values the server holds.
// A refused save keeps the boxes as the user left them and shows the error.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { getSettings, isStaffApiError, staffKeys, updateSettings } from '../../api/staff';
import type { FormSettings } from '../../api/staffTypes';
import { OptionsFormView } from '../configOptionsView';
import { errorTextKey, withOption, type ErrorTextKey, type OptionName } from '../configModel';
import { SERVICE_NAMESPACE } from '../namespace';
import '../config.css';

interface OptionsEditorProps {
  readonly initial: FormSettings;
}

function OptionsEditor({ initial }: OptionsEditorProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<FormSettings>(initial);
  const [error, setError] = useState<ErrorTextKey | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (settings: FormSettings) => updateSettings(settings),
    onSuccess: (saved) => {
      queryClient.setQueryData(staffKeys.settings, saved);
      setDraft(saved);
      setError(null);
      setNotice('management.settings.options.saved');
    },
    onError: (caught) => {
      setNotice(null);
      setError(errorTextKey(isStaffApiError(caught) ? caught.code : 'generic'));
    },
  });

  const onChange = (name: OptionName, value: boolean): void => {
    setDraft((previous) => withOption(previous, name, value));
    setNotice(null);
  };

  const onSubmit = (event: { preventDefault: () => void }): void => {
    event.preventDefault();
    if (mutation.isPending) return;
    setError(null);
    setNotice(null);
    mutation.mutate(draft);
  };

  return (
    <>
      <div role="status" aria-live="polite" className="config-notice">
        {notice === null ? null : <p className="feedback feedback--success">{t(notice)}</p>}
      </div>
      <OptionsFormView settings={draft} pending={mutation.isPending} error={error} onChange={onChange} onSubmit={onSubmit} />
    </>
  );
}

export function OptionsPage() {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const query = useQuery({
    queryKey: staffKeys.settings,
    queryFn: ({ signal }) => getSettings(signal),
    gcTime: 0,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  return (
    <section className="page-body">
      <h1>{t('management.settings.options.title')}</h1>
      <p className="subtitle">{t('management.settings.options.subtitle')}</p>

      {query.isError ? (
        <div className="feedback feedback--error config-load-error" role="alert">
          <p>{t('management.settings.options.loadError')}</p>
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

      {query.data === undefined ? null : <OptionsEditor initial={query.data} />}
    </section>
  );
}
