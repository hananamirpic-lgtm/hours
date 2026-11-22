// The create and rename form of an issue category (task 24.7; R19.5, R19.6, R19.7, R19.8, R23.12).
//
// `CategoryFormView` is the presentational form: both names have a visible label tied to the input, both say
// "required", and a refused name carries `aria-invalid` and a message that `aria-describedby` points at. The
// Hebrew name is typed right to left and the English name left to right. `CategoryForm` owns the values, the
// request and the dialog; after a refusal, focus moves to the first failing name.
//
// Every rule stays on the server: trimming, 1 to 60 characters, and the duplicate check. The form shows the
// codes the Ticket_API answers with: `invalid_name_he` and `invalid_name_en` (422), `duplicate_category` on the
// clashing language(s) (409). A refused request changes nothing in either language (R19.5, R19.7).
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { createCategory, isStaffApiError, renameCategory, staffKeys, type StaffCategory } from '../api/staff';
import { ConfigDialog } from './configDialog';
import {
  EMPTY_CATEGORY_VALUES,
  NO_CATEGORY_ERRORS,
  categoryFormErrors,
  categoryFormValues,
  firstFailingCategoryField,
  isCategoryGone,
  toCategoryBody,
  type CategoryFieldName,
  type CategoryFormErrors,
  type CategoryFormValues,
} from './configModel';
import { SERVICE_NAMESPACE } from './namespace';
import './config.css';

export const CATEGORY_FORM_IDS = {
  title: 'config-category-title',
  form: 'config-category-form',
  formError: 'config-category-form-error',
} as const;

/** The element id of a name's input, also the target of its label. */
export function categoryInputId(field: CategoryFieldName): string {
  return `config-category-${field === 'name_he' ? 'name-he' : 'name-en'}`;
}

function hintId(field: CategoryFieldName): string {
  return `${categoryInputId(field)}-hint`;
}

function errorId(field: CategoryFieldName): string {
  return `${categoryInputId(field)}-error`;
}

interface NameSpec {
  readonly field: CategoryFieldName;
  readonly valueKey: keyof CategoryFormValues;
  readonly labelKey: string;
  readonly language: 'he' | 'en';
}

/** The names in form order: the order the server reports failures in. */
const NAME_SPECS: readonly NameSpec[] = [
  { field: 'name_he', valueKey: 'nameHe', labelKey: 'management.settings.categories.nameHe', language: 'he' },
  { field: 'name_en', valueKey: 'nameEn', labelKey: 'management.settings.categories.nameEn', language: 'en' },
];

/** The order of `NAME_SPECS` is the order of the model; a test keeps the two together. */
export const NAME_FIELD_ORDER: readonly CategoryFieldName[] = NAME_SPECS.map((spec) => spec.field);

export interface CategoryFormViewProps {
  readonly values: CategoryFormValues;
  readonly errors: CategoryFormErrors;
  readonly pending: boolean;
  readonly onChange: (valueKey: keyof CategoryFormValues, value: string) => void;
  readonly onSubmit: (event: FormEvent) => void;
  readonly onCancel: () => void;
}

export function CategoryFormView({ values, errors, pending, onChange, onSubmit, onCancel }: CategoryFormViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const formErrorText = errors.form === null ? null : `${t('a11y.errorPrefix')} ${t(errors.form)}`;
  return (
    <form id={CATEGORY_FORM_IDS.form} className="form config-form" noValidate onSubmit={onSubmit}>
      {NAME_SPECS.map((spec) => {
        const errorKey = errors.fields[spec.field];
        const message = errorKey === undefined ? null : `${t('a11y.errorPrefix')} ${t(errorKey)}`;
        const describedBy = message === null ? hintId(spec.field) : `${hintId(spec.field)} ${errorId(spec.field)}`;
        // The marker carries its own leading space so the label reads "Name in Hebrew (required)".
        const marker = ` ${t('a11y.required')}`;
        return (
          <div className="field" key={spec.field}>
            <label className="field__label" htmlFor={categoryInputId(spec.field)}>
              {t(spec.labelKey)}
              <span className="config-marker">{marker}</span>
            </label>
            <input
              id={categoryInputId(spec.field)}
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
            <p id={hintId(spec.field)} className="field__hint">
              {t('management.settings.categories.nameHint')}
            </p>
            {message === null ? null : (
              <p id={errorId(spec.field)} className="form__error">
                {message}
              </p>
            )}
          </div>
        );
      })}
      {formErrorText === null ? null : (
        <p id={CATEGORY_FORM_IDS.formError} className="feedback feedback--error" role="alert">
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

export interface CategoryFormProps {
  /** The category being renamed; absent for a new category. */
  readonly category?: StaffCategory;
  /** Called after the Ticket_API stored the category. The page closes the form and says what happened. */
  readonly onSaved: (saved: StaffCategory, mode: 'created' | 'updated') => void;
  readonly onCancel: () => void;
}

export function CategoryForm({ category, onSaved, onCancel }: CategoryFormProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const queryClient = useQueryClient();
  const mode = category === undefined ? 'create' : 'edit';
  const [values, setValues] = useState<CategoryFormValues>(
    category === undefined ? EMPTY_CATEGORY_VALUES : categoryFormValues(category),
  );
  const [errors, setErrors] = useState<CategoryFormErrors>(NO_CATEGORY_ERRORS);

  const mutation = useMutation({
    mutationFn: (body: ReturnType<typeof toCategoryBody>): Promise<StaffCategory> =>
      category === undefined ? createCategory(body) : renameCategory(category.id, body),
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: staffKeys.categories });
      onSaved(saved, mode === 'create' ? 'created' : 'updated');
    },
    onError: (error) => {
      const refused = isStaffApiError(error) ? categoryFormErrors(error.code, error.fields) : categoryFormErrors('generic');
      setErrors(refused);
      // A category removed in the meantime: the list on screen is out of date.
      if (isStaffApiError(error) && isCategoryGone(error.code)) {
        void queryClient.invalidateQueries({ queryKey: staffKeys.categories });
      }
    },
  });

  // Focus moves to the first failing name after every refusal (R23.12). `errors` is a new object each time.
  useEffect(() => {
    const field = firstFailingCategoryField(errors);
    if (field !== null) document.getElementById(categoryInputId(field))?.focus();
  }, [errors]);

  const onChange = (valueKey: keyof CategoryFormValues, value: string): void => {
    setValues((previous) => ({ ...previous, [valueKey]: value }));
    // An edited name is no longer the refused value, so its message goes away.
    const field = NAME_SPECS.find((spec) => spec.valueKey === valueKey)?.field;
    if (field !== undefined && errors.fields[field] !== undefined) {
      const rest = { ...errors.fields };
      delete rest[field];
      setErrors({ fields: rest, form: errors.form });
    }
  };

  const onSubmit = (event: FormEvent): void => {
    event.preventDefault();
    if (mutation.isPending) return;
    setErrors(NO_CATEGORY_ERRORS);
    mutation.mutate(toCategoryBody(values));
  };

  return (
    <ConfigDialog
      titleId={CATEGORY_FORM_IDS.title}
      title={t(
        mode === 'create' ? 'management.settings.categories.createTitle' : 'management.settings.categories.editTitle',
      )}
      busy={mutation.isPending}
      onClose={onCancel}
    >
      <CategoryFormView
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
