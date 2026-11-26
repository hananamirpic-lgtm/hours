// The markup of the Branding form (task 30.7; R28.1, R28.2, R23.12). Props in, markup out: no query, no timers, so
// a static render can test it. `BrandingPage` owns the data, the object URLs and the handlers.
//
// Two fields. The company name has a visible label, a hint, a "Remove the name" button and a note while its
// removal is pending. The logo is a file input (accepting JPEG, PNG and WebP) with the 1 MB hint, a warning
// when the chosen file looks wrong (advice only, the server decides), a preview of the chosen file next to the
// stored logo, a button to clear the chosen file and a toggle to remove the stored logo. Neither field is
// required. A refused field carries `aria-invalid` and a message that `aria-describedby` points at.
import type { ChangeEvent, FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import type { StaffBranding } from '../api/staffListsTypes';
import {
  LOGO_ACCEPT,
  canRemoveName,
  formatFileSize,
  logoWarnings,
  logoWillBeRemoved,
  nameWillBeRemoved,
  type BrandingDraft,
  type BrandingFieldName,
  type BrandingFormErrors,
} from './brandingModel';
import { SERVICE_NAMESPACE } from './namespace';
import './branding.css';
import './config.css';

export const BRANDING_FORM_ID = 'config-branding-form';
export const BRANDING_FORM_ERROR_ID = 'config-branding-form-error';

/** The element id of a field's input, also the target of its label. */
export function brandingInputId(field: BrandingFieldName): string {
  return field === 'company_name' ? 'config-branding-name' : 'config-branding-logo';
}

function hintId(field: BrandingFieldName): string {
  return `${brandingInputId(field)}-hint`;
}

function errorId(field: BrandingFieldName): string {
  return `${brandingInputId(field)}-error`;
}

const NAME_NOTE_ID = 'config-branding-name-note';
const LOGO_WARNING_ID = 'config-branding-logo-warning';
const LOGO_NOTE_ID = 'config-branding-logo-note';

export interface BrandingFormViewProps {
  /** The branding the server holds. */
  readonly stored: StaffBranding;
  readonly draft: BrandingDraft;
  readonly errors: BrandingFormErrors;
  readonly pending: boolean;
  /** The object URL of the stored logo once it was fetched; null before that and when there is none. */
  readonly storedLogoUrl: string | null;
  /** True when the stored logo could not be fetched. */
  readonly storedLogoFailed: boolean;
  /** The object URL of the chosen file, when the browser can preview it. */
  readonly chosenLogoUrl: string | null;
  /** Changes with every reset of the file input, which has no value a script may set. */
  readonly inputKey: number;
  readonly onNameChange: (value: string) => void;
  readonly onRemoveName: () => void;
  readonly onFileChange: (file: File | null) => void;
  readonly onClearFile: () => void;
  readonly onToggleLogoRemoval: () => void;
  readonly onSubmit: (event: FormEvent) => void;
}

export function BrandingFormView({
  stored,
  draft,
  errors,
  pending,
  storedLogoUrl,
  storedLogoFailed,
  chosenLogoUrl,
  inputKey,
  onNameChange,
  onRemoveName,
  onFileChange,
  onClearFile,
  onToggleLogoRemoval,
  onSubmit,
}: BrandingFormViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const formErrorText = errors.form === null ? null : `${t('a11y.errorPrefix')} ${t(errors.form)}`;
  const nameErrorKey = errors.fields.company_name;
  const nameMessage = nameErrorKey === undefined ? null : `${t('a11y.errorPrefix')} ${t(nameErrorKey)}`;
  const logoErrorKey = errors.fields.logo;
  const logoMessage = logoErrorKey === undefined ? null : `${t('a11y.errorPrefix')} ${t(logoErrorKey)}`;
  const removingName = nameWillBeRemoved(draft, stored);
  const removingLogo = logoWillBeRemoved(draft, stored);
  const warnings = logoWarnings(draft.logo);

  const nameDescribedBy = [hintId('company_name'), removingName ? NAME_NOTE_ID : null, nameMessage === null ? null : errorId('company_name')]
    .filter((id): id is string => id !== null)
    .join(' ');
  const logoDescribedBy = [
    hintId('logo'),
    warnings.length > 0 ? LOGO_WARNING_ID : null,
    removingLogo ? LOGO_NOTE_ID : null,
    logoMessage === null ? null : errorId('logo'),
  ]
    .filter((id): id is string => id !== null)
    .join(' ');

  const onFile = (event: ChangeEvent<HTMLInputElement>): void => {
    onFileChange(event.target.files?.[0] ?? null);
  };

  return (
    <form id={BRANDING_FORM_ID} className="form config-form config-branding" noValidate onSubmit={onSubmit}>
      <div className="field">
        <label className="field__label" htmlFor={brandingInputId('company_name')}>
          {t('management.settings.branding.companyName')}
        </label>
        <div className="config-branding__row">
          <input
            id={brandingInputId('company_name')}
            name="company_name"
            className="input"
            type="text"
            autoComplete="off"
            value={draft.name}
            aria-invalid={nameMessage !== null}
            aria-describedby={nameDescribedBy}
            disabled={pending}
            onChange={(event) => onNameChange(event.target.value)}
          />
          <button
            type="button"
            className="button"
            disabled={pending || !canRemoveName(draft, stored)}
            onClick={onRemoveName}
          >
            {t('management.settings.branding.removeName')}
          </button>
        </div>
        <p id={hintId('company_name')} className="field__hint">
          {t('management.settings.branding.companyNameHint')}
        </p>
        {removingName ? (
          <p id={NAME_NOTE_ID} className="field__hint">
            {t('management.settings.branding.nameWillBeRemoved')}
          </p>
        ) : null}
        {nameMessage === null ? null : (
          <p id={errorId('company_name')} className="form__error">
            {nameMessage}
          </p>
        )}
      </div>

      <div className="field">
        <label className="field__label" htmlFor={brandingInputId('logo')}>
          {t('management.settings.branding.logo')}
        </label>
        <input
          key={inputKey}
          id={brandingInputId('logo')}
          name="logo"
          className="input"
          type="file"
          accept={LOGO_ACCEPT}
          aria-invalid={logoMessage !== null}
          aria-describedby={logoDescribedBy}
          disabled={pending}
          onChange={onFile}
        />
        <p id={hintId('logo')} className="field__hint">
          {t('management.settings.branding.logoHint')}
        </p>
        {warnings.length > 0 ? (
          <div id={LOGO_WARNING_ID} className="config-branding__warning">
            {warnings.map((warning) => (
              <p key={warning} className="field__hint">
                {t(warning === 'too_large' ? 'management.settings.branding.logoTooLarge' : 'management.settings.branding.logoWrongType')}
              </p>
            ))}
          </div>
        ) : null}
        {removingLogo ? (
          <p id={LOGO_NOTE_ID} className="field__hint">
            {t('management.settings.branding.logoWillBeRemoved')}
          </p>
        ) : null}
        {logoMessage === null ? null : (
          <p id={errorId('logo')} className="form__error">
            {logoMessage}
          </p>
        )}

        <div className="config-branding__previews">
          {draft.logo === null ? null : (
            <figure className="config-branding__figure">
              <figcaption>
                {t('management.settings.branding.chosenLogo')}
                <span className="config-branding__file">
                  {t('management.settings.branding.fileSummary', {
                    name: draft.logo.name,
                    size: formatFileSize(draft.logo.size),
                  })}
                </span>
              </figcaption>
              {chosenLogoUrl === null ? null : (
                <img
                  className="config-branding__image"
                  src={chosenLogoUrl}
                  alt={t('management.settings.branding.chosenLogoAlt')}
                />
              )}
            </figure>
          )}
          <figure className="config-branding__figure">
            <figcaption>{t('management.settings.branding.currentLogo')}</figcaption>
            {!stored.has_logo ? (
              <p className="state">{t('management.settings.branding.noLogo')}</p>
            ) : removingLogo ? null : storedLogoUrl !== null ? (
              <img
                className="config-branding__image"
                src={storedLogoUrl}
                alt={t('management.settings.branding.currentLogoAlt')}
              />
            ) : storedLogoFailed ? (
              <p className="feedback feedback--error" role="alert">
                {t('management.settings.branding.logoLoadError')}
              </p>
            ) : (
              <p className="state" aria-busy="true">
                {t('management.settings.branding.logoLoading')}
              </p>
            )}
          </figure>
        </div>

        <div className="config-branding__buttons">
          {draft.logo === null ? null : (
            <button type="button" className="button" disabled={pending} onClick={onClearFile}>
              {t('management.settings.branding.clearFile')}
            </button>
          )}
          {stored.has_logo ? (
            <button type="button" className="button" disabled={pending} onClick={onToggleLogoRemoval}>
              {t(draft.logoRemoved ? 'management.settings.branding.keepLogo' : 'management.settings.branding.removeLogo')}
            </button>
          ) : null}
        </div>
      </div>

      {formErrorText === null ? null : (
        <p id={BRANDING_FORM_ERROR_ID} className="feedback feedback--error" role="alert">
          {formErrorText}
        </p>
      )}
      <div className="form__actions">
        <button type="submit" className="button button--primary" disabled={pending}>
          {pending ? t('management.common.saving') : t('management.common.save')}
        </button>
      </div>
    </form>
  );
}
