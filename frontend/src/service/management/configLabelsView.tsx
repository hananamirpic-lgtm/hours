// The markup of the field label editor (task 24.7; R19.4, R19.5, R23.12). Props in, markup out: no query, no
// timers, so a static render can test it. `LabelsPage` owns the data and the handlers.
//
// One group per form field (the seven of R19.4) with the field name as its legend, holding the Hebrew and the
// English label. Every input has a visible label tied to it, says "required", and carries `aria-invalid` and a
// message that `aria-describedby` points at when the server refused it. The Hebrew label is typed right to
// left and the English one left to right. The 1 to 60 character rule is stated in the hint and enforced by the
// server, which names every failing text.
import { useTranslation } from 'react-i18next';
import type { FormLabelKey } from '../api/staffTypes';
import {
  CONFIG_LANGUAGES,
  LABEL_FIELDS,
  labelErrorKey,
  labelFieldNameKey,
  type ConfigLanguage,
  type LabelDrafts,
  type LabelFormErrors,
} from './configModel';
import { SERVICE_NAMESPACE } from './namespace';
import './config.css';

export const LABELS_FORM_ID = 'config-labels-form';
export const LABELS_FORM_ERROR_ID = 'config-labels-form-error';

/** The element id of one label input, also the target of its label. */
export function labelInputId(key: FormLabelKey, language: ConfigLanguage): string {
  return `config-label-${key}-${language}`;
}

function hintId(key: FormLabelKey): string {
  return `config-label-${key}-hint`;
}

function errorId(key: FormLabelKey, language: ConfigLanguage): string {
  return `${labelInputId(key, language)}-error`;
}

export interface LabelsFormViewProps {
  readonly drafts: LabelDrafts;
  readonly errors: LabelFormErrors;
  readonly pending: boolean;
  readonly onChange: (key: FormLabelKey, language: ConfigLanguage, value: string) => void;
  readonly onSubmit: (event: { preventDefault: () => void }) => void;
}

export function LabelsFormView({ drafts, errors, pending, onChange, onSubmit }: LabelsFormViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const formErrorText = errors.form === null ? null : `${t('a11y.errorPrefix')} ${t(errors.form)}`;
  return (
    <form id={LABELS_FORM_ID} className="form config-form config-labels" noValidate onSubmit={onSubmit}>
      {LABEL_FIELDS.map((key) => (
        <fieldset className="config-label-group" key={key}>
          <legend>{t(labelFieldNameKey(key))}</legend>
          <div className="config-label-group__row">
            {CONFIG_LANGUAGES.map((language) => {
              const errorKey = errors.fields[labelErrorKey(key, language)];
              const message = errorKey === undefined ? null : `${t('a11y.errorPrefix')} ${t(errorKey)}`;
              const describedBy = message === null ? hintId(key) : `${hintId(key)} ${errorId(key, language)}`;
              const marker = ` ${t('a11y.required')}`;
              return (
                <div className="field" key={language}>
                  <label className="field__label" htmlFor={labelInputId(key, language)}>
                    {t(language === 'he' ? 'management.settings.labels.labelHe' : 'management.settings.labels.labelEn')}
                    <span className="config-marker">{marker}</span>
                  </label>
                  <input
                    id={labelInputId(key, language)}
                    name={`${key}.${language}`}
                    className="input"
                    type="text"
                    lang={language}
                    dir={language === 'he' ? 'rtl' : 'ltr'}
                    autoComplete="off"
                    value={drafts[key][language]}
                    aria-required={true}
                    aria-invalid={message !== null}
                    aria-describedby={describedBy}
                    disabled={pending}
                    onChange={(event) => onChange(key, language, event.target.value)}
                  />
                  {message === null ? null : (
                    <p id={errorId(key, language)} className="form__error">
                      {message}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
          <p id={hintId(key)} className="field__hint">
            {t('management.settings.labels.hint')}
          </p>
        </fieldset>
      ))}
      {formErrorText === null ? null : (
        <p id={LABELS_FORM_ERROR_ID} className="feedback feedback--error" role="alert">
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
