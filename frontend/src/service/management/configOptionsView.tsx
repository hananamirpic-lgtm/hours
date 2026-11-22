// The markup of the form options (task 24.7; R19.2, R19.3, R23.12). Props in, markup out: no query, no timers,
// so a static render can test it. `OptionsPage` owns the data and the handlers.
//
// Two checkboxes, each with a visible label: "Description required" and "Photo upload enabled". The options are
// saved together (the Ticket_API takes both as strict booleans), and a change applies to tickets sent after it.
import { useTranslation } from 'react-i18next';
import type { FormSettings } from '../api/staffTypes';
import type { ErrorTextKey, OptionName } from './configModel';
import { SERVICE_NAMESPACE } from './namespace';
import './config.css';

export const OPTIONS_FORM_ID = 'config-options-form';
export const OPTIONS_FORM_ERROR_ID = 'config-options-form-error';

/** The element id of an option's checkbox, also the target of its label. */
export function optionInputId(name: OptionName): string {
  return `config-option-${name === 'description_required' ? 'description-required' : 'photo-upload-enabled'}`;
}

function helpId(name: OptionName): string {
  return `${optionInputId(name)}-help`;
}

interface OptionSpec {
  readonly name: OptionName;
  readonly labelKey: string;
  /** The help text under the option, when it has one. */
  readonly helpKey: string | null;
}

/** The options in the order they are shown. */
const OPTION_SPECS: readonly OptionSpec[] = [
  { name: 'description_required', labelKey: 'management.settings.options.descriptionRequired', helpKey: null },
  {
    name: 'photo_upload_enabled',
    labelKey: 'management.settings.options.photoUploadEnabled',
    helpKey: 'management.settings.options.photoUploadHelp',
  },
];

/** The order of `OPTION_SPECS` is the order of the model; a test keeps the two together. */
export const OPTION_ORDER: readonly OptionName[] = OPTION_SPECS.map((spec) => spec.name);

export interface OptionsFormViewProps {
  readonly settings: FormSettings;
  readonly pending: boolean;
  /** The text key of the refusal of the last save, or null. */
  readonly error: ErrorTextKey | null;
  readonly onChange: (name: OptionName, value: boolean) => void;
  readonly onSubmit: (event: { preventDefault: () => void }) => void;
}

export function OptionsFormView({ settings, pending, error, onChange, onSubmit }: OptionsFormViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const errorText = error === null ? null : `${t('a11y.errorPrefix')} ${t(error)}`;
  return (
    <form id={OPTIONS_FORM_ID} className="form config-form config-options" noValidate onSubmit={onSubmit}>
      {OPTION_SPECS.map((spec) => (
        <div key={spec.name}>
          <div className="checkbox-field config-options__row">
            <input
              id={optionInputId(spec.name)}
              name={spec.name}
              type="checkbox"
              checked={settings[spec.name]}
              disabled={pending}
              {...(spec.helpKey === null ? {} : { 'aria-describedby': helpId(spec.name) })}
              onChange={(event) => onChange(spec.name, event.target.checked)}
            />
            <label htmlFor={optionInputId(spec.name)}>{t(spec.labelKey)}</label>
          </div>
          {spec.helpKey === null ? null : (
            <p id={helpId(spec.name)} className="field__hint">
              {t(spec.helpKey)}
            </p>
          )}
        </div>
      ))}
      {errorText === null ? null : (
        <p id={OPTIONS_FORM_ERROR_ID} className="feedback feedback--error" role="alert">
          {errorText}
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
