// The Branding screen of the Management_Interface (task 30.7; R28.1, R28.2, R23.12). Config staff only:
// `RequireConfigStaff` in the route tree keeps a technician from ever mounting it.
//
// The company name (1 to 100 characters) and the logo (JPEG, PNG or WebP, up to 1 MB) that residents see above the
// title of the ticket form. One Save sends only what changed, as multipart: the name, the file, and a removal of
// either. The server checks every part before it stores any, so a refusal changes nothing; the page keeps what
// was typed and chosen, marks the failing field and moves focus to the first one.
//
// The stored logo is fetched as a Blob with the staff bearer header (an `<img src>` could not send it) and shown
// through an object URL; the chosen file is shown the same way before it is sent. Both URLs are revoked when the
// blob changes and on unmount (`useObjectUrl`). The editor starts from what the server holds and the query is not
// cached after the page closes, so a half-typed edit is never replaced under the user's hands.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { isStaffApiError } from '../../api/staff';
import { fetchBrandingLogo, getBranding, staffListKeys, updateBranding } from '../../api/staffLists';
import type { BrandingChange, StaffBranding } from '../../api/staffListsTypes';
import {
  NO_BRANDING_ERRORS,
  brandingChange,
  brandingFormErrors,
  draftFromStored,
  draftWithLogo,
  draftWithLogoRemovalToggled,
  draftWithName,
  draftWithNameRemoved,
  firstFailingBrandingField,
  hasBrandingChange,
  isPreviewable,
  withoutBrandingError,
  type BrandingDraft,
  type BrandingFormErrors,
} from '../brandingModel';
import { BrandingFormView, brandingInputId } from '../brandingView';
import { SERVICE_NAMESPACE } from '../namespace';
import { useObjectUrl } from '../objectUrl';
import '../config.css';

interface BrandingEditorProps {
  readonly initial: StaffBranding;
}

function BrandingEditor({ initial }: BrandingEditorProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const queryClient = useQueryClient();
  // The branding the server holds, as last read or saved. A save sends what the draft changes in it.
  const [stored, setStored] = useState<StaffBranding>(initial);
  const [draft, setDraft] = useState<BrandingDraft>(() => draftFromStored(initial));
  const [errors, setErrors] = useState<BrandingFormErrors>(NO_BRANDING_ERRORS);
  const [notice, setNotice] = useState<string | null>(null);
  const [inputKey, setInputKey] = useState(0);

  // The stored logo, fetched only while one exists. A save invalidates it, so a replaced logo is fetched again.
  const logo = useQuery({
    queryKey: staffListKeys.brandingLogo,
    queryFn: ({ signal }) => fetchBrandingLogo(signal),
    enabled: stored.has_logo,
    gcTime: 0,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
  const storedLogoUrl = useObjectUrl(stored.has_logo ? (logo.data ?? null) : null);
  const chosenFile = draft.logo !== null && isPreviewable(draft.logo) ? draft.logo : null;
  const chosenLogoUrl = useObjectUrl(chosenFile);

  const mutation = useMutation({
    mutationFn: (change: BrandingChange) => updateBranding(change),
    onSuccess: async (saved) => {
      queryClient.setQueryData(staffListKeys.branding, saved);
      setStored(saved);
      setDraft(draftFromStored(saved));
      setInputKey((key) => key + 1);
      setErrors(NO_BRANDING_ERRORS);
      setNotice('management.settings.branding.saved');
      await queryClient.invalidateQueries({ queryKey: staffListKeys.brandingLogo });
    },
    onError: (error) => {
      setNotice(null);
      setErrors(isStaffApiError(error) ? brandingFormErrors(error.code, error.fields) : brandingFormErrors('generic'));
    },
  });

  // Focus moves to the first failing field after every refusal (R23.12). `errors` is a new object each time.
  useEffect(() => {
    const field = firstFailingBrandingField(errors);
    if (field !== null) document.getElementById(brandingInputId(field))?.focus();
  }, [errors]);

  const onSubmit = (event: { preventDefault: () => void }): void => {
    event.preventDefault();
    if (mutation.isPending) return;
    const change = brandingChange(draft, stored);
    setErrors(NO_BRANDING_ERRORS);
    if (!hasBrandingChange(change)) {
      setNotice('management.settings.branding.noChanges');
      return;
    }
    setNotice(null);
    mutation.mutate(change);
  };

  return (
    <>
      <div role="status" aria-live="polite" className="config-notice">
        {notice === null ? null : <p className="feedback feedback--success">{t(notice)}</p>}
      </div>
      <BrandingFormView
        stored={stored}
        draft={draft}
        errors={errors}
        pending={mutation.isPending}
        storedLogoUrl={storedLogoUrl}
        storedLogoFailed={logo.isError || (logo.isSuccess && logo.data === null)}
        chosenLogoUrl={chosenLogoUrl}
        inputKey={inputKey}
        onNameChange={(value) => {
          setDraft((previous) => draftWithName(previous, value));
          setErrors((previous) => withoutBrandingError(previous, 'company_name'));
        }}
        onRemoveName={() => {
          setDraft((previous) => draftWithNameRemoved(previous));
          setErrors((previous) => withoutBrandingError(previous, 'company_name'));
        }}
        onFileChange={(file) => {
          setDraft((previous) => draftWithLogo(previous, file));
          setErrors((previous) => withoutBrandingError(previous, 'logo'));
        }}
        onClearFile={() => {
          setDraft((previous) => draftWithLogo(previous, null));
          setInputKey((key) => key + 1);
          setErrors((previous) => withoutBrandingError(previous, 'logo'));
        }}
        onToggleLogoRemoval={() => {
          setDraft((previous) => draftWithLogoRemovalToggled(previous));
          setInputKey((key) => key + 1);
          setErrors((previous) => withoutBrandingError(previous, 'logo'));
        }}
        onSubmit={onSubmit}
      />
    </>
  );
}

export function BrandingPage() {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const query = useQuery({
    queryKey: staffListKeys.branding,
    queryFn: ({ signal }) => getBranding(signal),
    gcTime: 0,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  return (
    <section className="page-body">
      <h1>{t('management.settings.branding.title')}</h1>
      <p className="subtitle">{t('management.settings.branding.subtitle')}</p>

      {query.isError ? (
        <div className="feedback feedback--error config-load-error" role="alert">
          <p>{t('management.settings.branding.loadError')}</p>
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

      {query.data === undefined ? null : <BrandingEditor initial={query.data} />}
    </section>
  );
}
