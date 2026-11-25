// The create and rename form of a settlement or a replaceable part (task 30.7; R30.1, R29.6, R23.12). The same as
// the category form.
//
// `NamedListFormView` is the presentational form: both names have a visible label tied to the input, both say
// "required", and a refused name carries `aria-invalid` and a message that `aria-describedby` points at. The
// Hebrew name is typed right to left and the English name left to right. `NamedListForm` owns the values, the
// request and the dialog; after a refusal, focus moves to the first failing name.
//
// Every rule stays on the server: trimming, the length range and the duplicate check. The form shows the codes
// the Ticket_API answers with: `invalid_settlement_name_he/en` or `invalid_part_name_he/en` (422) and
// `duplicate_settlement` or `duplicate_part` on the clashing language(s) (409). A refused request changes
// nothing in either language.
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { isStaffApiError } from '../api/staff';
import { namedListClient, namedListKey } from '../api/staffLists';
import type { NamedItemBody, NamedListKind, StaffNamedItem } from '../api/staffListsTypes';
import { ConfigDialog } from './configDialog';
import {
  EMPTY_NAMED_VALUES,
  NAMED_LISTS,
  NO_NAMED_ERRORS,
  firstFailingNameField,
  isNamedItemGone,
  listText,
  namedFormErrors,
  namedFormValues,
  toNamedBody,
  withoutNamedError,
  type NameFieldName,
  type NamedFormErrors,
  type NamedFormValues,
} from './namedListModel';
import { SERVICE_NAMESPACE } from './namespace';
import './config.css';

/** The ids of the form's elements for a list. */
export function namedFormIds(kind: NamedListKind): { readonly title: string; readonly form: string; readonly formError: string } {
  const prefix = NAMED_LISTS[kind].idPrefix;
  return { title: `${prefix}-title`, form: `${prefix}-form`, formError: `${prefix}-form-error` };
}

/** The element id of a name's input, also the target of its label. */
export function namedInputId(kind: NamedListKind, field: NameFieldName): string {
  return `${NAMED_LISTS[kind].idPrefix}-${field === 'name_he' ? 'name-he' : 'name-en'}`;
}

function hintId(kind: NamedListKind, field: NameFieldName): string {
  return `${namedInputId(kind, field)}-hint`;
}

function errorId(kind: NamedListKind, field: NameFieldName): string {
  return `${namedInputId(kind, field)}-error`;
}

interface NameSpec {
  readonly field: NameFieldName;
  readonly valueKey: keyof NamedFormValues;
  readonly labelName: 'nameHe' | 'nameEn';
  readonly language: 'he' | 'en';
}

/** The names in form order: the order the server reports failures in. */
const NAME_SPECS: readonly NameSpec[] = [
  { field: 'name_he', valueKey: 'nameHe', labelName: 'nameHe', language: 'he' },
  { field: 'name_en', valueKey: 'nameEn', labelName: 'nameEn', language: 'en' },
];

/** The order of `NAME_SPECS` is the order of the model; a test keeps the two together. */
export const NAMED_FORM_FIELD_ORDER: readonly NameFieldName[] = NAME_SPECS.map((spec) => spec.field);

export interface NamedListFormViewProps {
  readonly kind: NamedListKind;
  readonly values: NamedFormValues;
  readonly errors: NamedFormErrors;
  readonly pending: boolean;
  readonly onChange: (valueKey: keyof NamedFormValues, value: string) => void;
  readonly onSubmit: (event: FormEvent) => void;
  readonly onCancel: () => void;
}

export function NamedListFormView({ kind, values, errors, pending, onChange, onSubmit, onCancel }: NamedListFormViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const ids = namedFormIds(kind);
  const formErrorText = errors.form === null ? null : `${t('a11y.errorPrefix')} ${t(errors.form)}`;
  return (
    <form id={ids.form} className="form config-form" noValidate onSubmit={onSubmit}>
      {NAME_SPECS.map((spec) => {
        const errorKey = errors.fields[spec.field];
        const message = errorKey === undefined ? null : `${t('a11y.errorPrefix')} ${t(errorKey)}`;
        const describedBy = message === null ? hintId(kind, spec.field) : `${hintId(kind, spec.field)} ${errorId(kind, spec.field)}`;
        // The marker carries its own leading space so the label reads "Name in Hebrew (required)".
        const marker = ` ${t('a11y.required')}`;
        return (
          <div className="field" key={spec.field}>
            <label className="field__label" htmlFor={namedInputId(kind, spec.field)}>
              {t(listText(kind, spec.labelName))}
              <span className="config-marker">{marker}</span>
            </label>
            <input
              id={namedInputId(kind, spec.field)}
              name={spec.field}
              className="input"
              type="text"
              lang={spec.language}
              dir={spec.language === 'he' ? 'rtl' : 'ltr'}
              autoComplete="off"
              value={values[spec.valueKey]}
              aria-required={true}
              aria-invalid={message !== null}
              aria-describedby={describedBy}
              disabled={pending}
              onChange={(event) => onChange(spec.valueKey, event.target.value)}
            />
            <p id={hintId(kind, spec.field)} className="field__hint">
              {t(listText(kind, 'nameHint'))}
            </p>
            {message === null ? null : (
              <p id={errorId(kind, spec.field)} className="form__error">
                {message}
              </p>
            )}
          </div>
        );
      })}
      {formErrorText === null ? null : (
        <p id={ids.formError} className="feedback feedback--error" role="alert">
          {formErrorText}
        </p>
      )}
      <div className="form__actions">
        <button type="button" className="button" disabled={pending} onClick={onCancel}>
          {t('management.common.cancel')}
        </button>
        <button type="submit" className="button button--primary" disabled={pending}>
          {pending ? t('management.common.saving') : t('management.common.save')}
        </button>
      </div>
    </form>
  );
}

export interface NamedListFormProps {
  readonly kind: NamedListKind;
  /** The item being renamed; absent for a new item. */
  readonly item?: StaffNamedItem;
  /** Called after the Ticket_API stored the item. The page closes the form and says what happened. */
  readonly onSaved: (saved: StaffNamedItem, mode: 'created' | 'updated') => void;
  readonly onCancel: () => void;
}

export function NamedListForm({ kind, item, onSaved, onCancel }: NamedListFormProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const queryClient = useQueryClient();
  const client = namedListClient(kind);
  const mode = item === undefined ? 'create' : 'edit';
  const [values, setValues] = useState<NamedFormValues>(item === undefined ? EMPTY_NAMED_VALUES : namedFormValues(item));
  const [errors, setErrors] = useState<NamedFormErrors>(NO_NAMED_ERRORS);

  const mutation = useMutation({
    mutationFn: (body: NamedItemBody): Promise<StaffNamedItem> =>
      item === undefined ? client.create(body) : client.rename(item.id, body),
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: namedListKey(kind) });
      onSaved(saved, mode === 'create' ? 'created' : 'updated');
    },
    onError: (error) => {
      setErrors(isStaffApiError(error) ? namedFormErrors(error.code, error.fields) : namedFormErrors('generic'));
      // An item removed in the meantime: the list on screen is out of date.
      if (isStaffApiError(error) && isNamedItemGone(kind, error.code)) {
        void queryClient.invalidateQueries({ queryKey: namedListKey(kind) });
      }
    },
  });

  // Focus moves to the first failing name after every refusal (R23.12). `errors` is a new object each time.
  useEffect(() => {
    const field = firstFailingNameField(errors);
    if (field !== null) document.getElementById(namedInputId(kind, field))?.focus();
  }, [errors, kind]);

  const onChange = (valueKey: keyof NamedFormValues, value: string): void => {
    setValues((previous) => ({ ...previous, [valueKey]: value }));
    const field = NAME_SPECS.find((spec) => spec.valueKey === valueKey)?.field;
    if (field !== undefined) setErrors((previous) => withoutNamedError(previous, field));
  };

  const onSubmit = (event: FormEvent): void => {
    event.preventDefault();
    if (mutation.isPending) return;
    setErrors(NO_NAMED_ERRORS);
    mutation.mutate(toNamedBody(values));
  };

  return (
    <ConfigDialog
      titleId={namedFormIds(kind).title}
      title={t(listText(kind, mode === 'create' ? 'createTitle' : 'editTitle'))}
      busy={mutation.isPending}
      onClose={onCancel}
    >
      <NamedListFormView
        kind={kind}
        values={values}
        errors={errors}
        pending={mutation.isPending}
        onChange={onChange}
        onSubmit={onSubmit}
        onCancel={onCancel}
      />
    </ConfigDialog>
  );
}
