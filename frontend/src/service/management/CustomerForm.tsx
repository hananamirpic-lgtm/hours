// The create and edit form of a customer (task 24.5; R3.2, R3.3, R3.4, R3.5, R3.6, R3.12, R23.12).
//
// `CustomerFormView` is the presentational form: every input has a visible label tied to it, the required
// ones say so in text and in `aria-required`, and a refused field carries `aria-invalid` and a message that
// `aria-describedby` points at (R23.12). `CustomerForm` owns the values, the request and the dialog. After a
// refusal, focus moves to the first failing field in form order.
//
// Every rule stays on the server. The form does not check the phone or the lengths itself; it shows the
// field codes the Ticket_API answers with: `invalid_phone` and `phone_already_registered` on the phone,
// `invalid_full_name`, `invalid_city` and `invalid_apartment` on their fields. No password, hash or initial
// password value is read from any answer (R3.12); a new customer's initial password is named by the text only.
//
// Settlements (R30.6). While the active settlement list from `GET /staff/form-config` is not empty, the City
// field is a select of the active settlements ("No settlement" is the empty choice), the customer's current
// settlement stays selectable when it was deactivated, and a text city with no settlement is shown as read-only
// help. With an empty list the field is the free-text input.
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { createCustomer, isStaffApiError, staffKeys, updateCustomer, type StaffCustomer } from '../api/staff';
import { useActiveSettlements } from '../api/staffFormConfig';
import { CustomerDialog } from './CustomerDialog';
import {
  EMPTY_FORM_VALUES,
  NO_FORM_ERRORS,
  currentCityText,
  customerFormErrors,
  firstFailingField,
  formValuesFromCustomer,
  isCustomerGone,
  settlementOptions,
  toCustomerBody,
  type CustomerFieldName,
  type CustomerFormErrors,
  type CustomerFormValues,
  type SettlementOption,
} from './customersModel';
import { SERVICE_NAMESPACE } from './namespace';
import { listLanguage } from './ticketListModel';
import './customers.css';

export const CUSTOMER_FORM_IDS = {
  title: 'customer-form-title',
  form: 'customer-form',
  initialPasswordNote: 'customer-form-initial-password',
  formError: 'customer-form-error',
} as const;

/** The element id of a field's input, also the target of its label. */
export function fieldInputId(field: CustomerFieldName): string {
  return `customer-form-${field}`;
}

function hintId(field: CustomerFieldName): string {
  return `${fieldInputId(field)}-hint`;
}

function errorId(field: CustomerFieldName): string {
  return `${fieldInputId(field)}-error`;
}

type ValueKey = keyof CustomerFormValues;

interface FieldSpec {
  readonly field: CustomerFieldName;
  readonly valueKey: ValueKey;
  readonly labelKey: string;
  readonly hintKey: string;
  readonly required: boolean;
  /** Attributes for a phone number: a telephone keypad, left to right, no stored suggestions. */
  readonly phone: boolean;
}

/** The fields in form order: the order the server reports failures in. */
const FIELD_SPECS: readonly FieldSpec[] = [
  {
    field: 'phone',
    valueKey: 'phone',
    labelKey: 'management.customers.form.phone',
    hintKey: 'management.customers.form.phoneHint',
    required: true,
    phone: true,
  },
  {
    field: 'full_name',
    valueKey: 'fullName',
    labelKey: 'management.customers.form.fullName',
    hintKey: 'management.customers.form.fullNameHint',
    required: true,
    phone: false,
  },
  {
    field: 'city',
    valueKey: 'city',
    labelKey: 'management.customers.form.city',
    hintKey: 'management.customers.form.cityHint',
    required: false,
    phone: false,
  },
  {
    field: 'apartment',
    valueKey: 'apartment',
    labelKey: 'management.customers.form.apartment',
    hintKey: 'management.customers.form.apartmentHint',
    required: false,
    phone: false,
  },
];

/** The order of `FIELD_SPECS` is the order of the model; a test keeps the two together. */
export const FORM_FIELD_ORDER: readonly CustomerFieldName[] = FIELD_SPECS.map((spec) => spec.field);

export interface CustomerFormViewProps {
  readonly mode: 'create' | 'edit';
  readonly values: CustomerFormValues;
  readonly errors: CustomerFormErrors;
  readonly pending: boolean;
  /**
   * The settlement choices. Non-empty: the City field is a select of them. Empty (the default): the free-text
   * input.
   */
  readonly settlements?: readonly SettlementOption[];
  /** True while the settlement list is being read: the City field and Save wait for it. */
  readonly settlementsLoading?: boolean;
  readonly onChange: (valueKey: ValueKey, value: string) => void;
  readonly onSubmit: (event: FormEvent) => void;
  readonly onCancel: () => void;
}

export function CustomerFormView({
  mode,
  values,
  errors,
  pending,
  settlements = [],
  settlementsLoading = false,
  onChange,
  onSubmit,
  onCancel,
}: CustomerFormViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const settlementMode = settlements.length > 0;
  const currentCity = settlementMode ? currentCityText(values) : null;
  const formErrorText = errors.form === null ? null : `${t('a11y.errorPrefix')} ${t(errors.form)}`;
  return (
    <form id={CUSTOMER_FORM_IDS.form} className="form customers-form" noValidate onSubmit={onSubmit}>
      {FIELD_SPECS.map((spec) => {
        const errorKey = errors.fields[spec.field];
        const message = errorKey === undefined ? null : `${t('a11y.errorPrefix')} ${t(errorKey)}`;
        const settlementField = spec.field === 'city' && settlementMode;
        const hintKey = settlementField
          ? 'management.customers.form.settlementHint'
          : spec.field === 'city' && settlementsLoading
            ? 'management.customers.form.settlementLoading'
            : spec.hintKey;
        const currentId = `${fieldInputId('city')}-current`;
        const ids = [hintId(spec.field)];
        if (settlementField && currentCity !== null) ids.push(currentId);
        if (message !== null) ids.push(errorId(spec.field));
        const describedBy = ids.join(' ');
        // The marker carries its own leading space so the label reads "Phone number (required)".
        const marker = ` ${t(spec.required ? 'a11y.required' : 'a11y.optional')}`;
        return (
          <div className="field" key={spec.field}>
            <label className="field__label" htmlFor={fieldInputId(spec.field)}>
              {t(spec.labelKey)}
              <span className="customers-marker">{marker}</span>
            </label>
            {settlementField ? (
              <select
                id={fieldInputId('city')}
                name="locality_id"
                className="select"
                value={values.localityId}
                aria-required={false}
                aria-invalid={message !== null}
                aria-describedby={describedBy}
                disabled={pending}
                onChange={(event) => onChange('localityId', event.target.value)}
              >
                <option value="">{t('management.customers.form.settlementNone')}</option>
                {settlements.map((option) => (
                  <option key={option.id} value={option.id}>
                    {option.inactive ? t('management.customers.form.settlementInactive', { name: option.name }) : option.name}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={fieldInputId(spec.field)}
                name={spec.field}
                className="input"
                type={spec.phone ? 'tel' : 'text'}
                {...(spec.phone ? { inputMode: 'numeric' as const, dir: 'ltr' } : {})}
                autoComplete="off"
                value={values[spec.valueKey]}
                aria-required={spec.required}
                aria-invalid={message !== null}
                aria-describedby={describedBy}
                disabled={pending || (spec.field === 'city' && settlementsLoading)}
                onChange={(event) => onChange(spec.valueKey, event.target.value)}
              />
            )}
            <p id={hintId(spec.field)} className="field__hint">
              {t(hintKey)}
            </p>
            {settlementField && currentCity !== null ? (
              <p id={currentId} className="field__hint">
                {t('management.customers.form.settlementCurrentText', { city: currentCity })}
              </p>
            ) : null}
            {message === null ? null : (
              <p id={errorId(spec.field)} className="form__error">
                {message}
              </p>
            )}
          </div>
        );
      })}
      {mode === 'create' ? (
        <p id={CUSTOMER_FORM_IDS.initialPasswordNote} className="field__hint">
          {t('management.customers.form.initialPasswordNote')}
        </p>
      ) : null}
      {formErrorText === null ? null : (
        <p id={CUSTOMER_FORM_IDS.formError} className="feedback feedback--error" role="alert">
          {formErrorText}
        </p>
      )}
      <div className="form__actions">
        <button type="button" className="button" disabled={pending} onClick={onCancel}>
          {t('management.common.cancel')}
        </button>
        <button type="submit" className="button button--primary" disabled={pending || settlementsLoading}>
          {pending ? t('management.common.saving') : t('management.common.save')}
        </button>
      </div>
    </form>
  );
}

export interface CustomerFormProps {
  /** The customer being edited; absent for a new customer. */
  readonly customer?: StaffCustomer;
  /** Called after the Ticket_API stored the customer. The page closes the form and says what happened. */
  readonly onSaved: (saved: StaffCustomer, mode: 'created' | 'updated') => void;
  readonly onCancel: () => void;
}

export function CustomerForm({ customer, onSaved, onCancel }: CustomerFormProps) {
  const { t, i18n } = useTranslation(SERVICE_NAMESPACE);
  const queryClient = useQueryClient();
  const mode = customer === undefined ? 'create' : 'edit';
  const [values, setValues] = useState<CustomerFormValues>(
    customer === undefined ? EMPTY_FORM_VALUES : formValuesFromCustomer(customer),
  );
  const [errors, setErrors] = useState<CustomerFormErrors>(NO_FORM_ERRORS);
  const { settlements: active, loading: settlementsLoading } = useActiveSettlements();
  const options = useMemo(
    () => (active.length === 0 ? [] : settlementOptions(active, customer?.locality, listLanguage(i18n.language))),
    [active, customer?.locality, i18n.language],
  );
  const settlementList = options.length > 0;

  const mutation = useMutation({
    mutationFn: (body: ReturnType<typeof toCustomerBody>): Promise<StaffCustomer> =>
      customer === undefined ? createCustomer(body) : updateCustomer(customer.id, body),
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: staffKeys.customers });
      onSaved(saved, mode === 'create' ? 'created' : 'updated');
    },
    onError: (error) => {
      const refused = isStaffApiError(error) ? customerFormErrors(error.code, error.fields) : customerFormErrors('generic');
      setErrors(refused);
      // A customer removed in the meantime: the list on screen is out of date (R3.14).
      if (isStaffApiError(error) && isCustomerGone(error.code)) {
        void queryClient.invalidateQueries({ queryKey: staffKeys.customers });
      }
    },
  });

  // Focus moves to the first failing field after every refusal (R23.12). `errors` is a new object each time.
  useEffect(() => {
    const field = firstFailingField(errors);
    if (field !== null) document.getElementById(fieldInputId(field))?.focus();
  }, [errors]);

  const onChange = (valueKey: ValueKey, value: string): void => {
    setValues((previous) => ({ ...previous, [valueKey]: value }));
    // An edited field is no longer the refused value, so its message goes away.
    // The settlement select belongs to the City field.
    const field = valueKey === 'localityId' ? 'city' : FIELD_SPECS.find((spec) => spec.valueKey === valueKey)?.field;
    if (field !== undefined && errors.fields[field] !== undefined) {
      const rest = { ...errors.fields };
      delete rest[field];
      setErrors({ fields: rest, form: errors.form });
    }
  };

  const onSubmit = (event: FormEvent): void => {
    event.preventDefault();
    if (mutation.isPending) return;
    setErrors(NO_FORM_ERRORS);
    mutation.mutate(toCustomerBody(values, settlementList));
  };

  return (
    <CustomerDialog
      titleId={CUSTOMER_FORM_IDS.title}
      title={t(mode === 'create' ? 'management.customers.form.createTitle' : 'management.customers.form.editTitle')}
      busy={mutation.isPending}
      onClose={onCancel}
    >
      <CustomerFormView
        mode={mode}
        values={values}
        errors={errors}
        pending={mutation.isPending}
        settlements={options}
        settlementsLoading={settlementsLoading}
        onChange={onChange}
        onSubmit={onSubmit}
        onCancel={onCancel}
      />
    </CustomerDialog>
  );
}
