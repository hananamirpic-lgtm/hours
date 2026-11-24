/**
 * The markup of the management New ticket screen (task 29.5; R26.1, R26.2, R23.12). Props in, markup out: no
 * query, no router, no timers, so a static render can test every state. `NewTicketPage` owns the data.
 *
 *   CustomerPickerView   step 1: a labelled search box and a list of buttons, one per active customer.
 *   ChosenCustomerView   the chosen customer as text, with a "Change customer" button.
 *   NewTicketFormView    step 2: the ticket form with the current Field_Labels, per-field errors, optional photo.
 *
 * Accessibility: every input has a visible label tied to it; "(required)" and "(optional)" are text; a refused
 * field has `aria-invalid` and `aria-describedby` pointing at its message; targets are at least 44 px high.
 */
import type { FormEvent, ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import type { FormSettings } from '../api/formTypes';
import type { StaffCustomer } from '../api/staffTypes';
import type { FieldErrors } from '../portal/formErrors';
import {
  NOTICE_ID,
  PHOTO_ACCEPT,
  SUMMARY_ID,
  describedBy,
  descriptionState,
  errorSummaryItems,
  fieldErrorKey,
  fieldId,
  fieldOrder,
  hintId,
  errorId,
  isolateText,
  labelKeyOfField,
  type CategoryOption,
  type FormValues,
  type ResolvedLabels,
} from '../portal/screens/ticketFormModel';
import { SERVICE_NAMESPACE } from './namespace';
import { SEARCH_MAX_LENGTH, customerPlace, hasMoreMatches, noticeKey, type NewTicketNotice, type PickerState } from './newTicketModel';
import { errorKeyOf } from './ticketDetailModel';
import { TICKET_LIST_PATH } from './dashboardModel';
import './newTicket.css';

export const NEW_TICKET_IDS = {
  search: 'new-ticket-search',
  searchHint: 'new-ticket-search-hint',
  pickerHeading: 'new-ticket-picker-heading',
  customerHeading: 'new-ticket-customer-heading',
  formHeading: 'new-ticket-form-heading',
} as const;

// --- step 1: the picker -----------------------------------------------------------------------------

export interface CustomerPickerViewProps {
  /** The text in the search box. It is applied on submit (the Search button or Enter). */
  readonly searchDraft: string;
  readonly state: PickerState;
  /** True while a request runs, also when earlier results stay on screen. */
  readonly busy: boolean;
  readonly onSearchDraftChange: (value: string) => void;
  readonly onSearchSubmit: () => void;
  readonly onPick: (customer: StaffCustomer) => void;
  readonly onRetry: () => void;
}

export function CustomerPickerView({
  searchDraft,
  state,
  busy,
  onSearchDraftChange,
  onSearchSubmit,
  onPick,
  onRetry,
}: CustomerPickerViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const submit = (event: FormEvent): void => {
    event.preventDefault();
    onSearchSubmit();
  };
  return (
    <section className="card new-ticket__step" aria-labelledby={NEW_TICKET_IDS.pickerHeading}>
      <h2 id={NEW_TICKET_IDS.pickerHeading} className="card__title">
        {t('management.newTicket.picker.title')}
      </h2>
      <p className="subtitle">{t('management.newTicket.picker.intro')}</p>
      <form className="toolbar new-ticket__search-form" role="search" onSubmit={submit}>
        <div className="field new-ticket__search">
          <label className="field__label" htmlFor={NEW_TICKET_IDS.search}>
            {t('management.newTicket.picker.searchLabel')}
          </label>
          <input
            id={NEW_TICKET_IDS.search}
            className="input"
            type="search"
            autoComplete="off"
            maxLength={SEARCH_MAX_LENGTH}
            value={searchDraft}
            aria-describedby={NEW_TICKET_IDS.searchHint}
            onChange={(event) => onSearchDraftChange(event.target.value)}
          />
          <p id={NEW_TICKET_IDS.searchHint} className="field__hint">
            {t('management.newTicket.picker.searchHint')}
          </p>
        </div>
        <button type="submit" className="button button--primary">
          {t('management.common.search')}
        </button>
      </form>

      <div role="status" aria-live="polite" className="new-ticket__status">
        {state.kind === 'loading' || (busy && state.kind !== 'error') ? (
          <p>{t('management.common.loading')}</p>
        ) : state.kind === 'results' ? (
          <p>{t('management.newTicket.picker.found', { count: state.total })}</p>
        ) : null}
      </div>

      {state.kind === 'error' ? (
        <div className="feedback feedback--error" role="alert">
          <p>{t('management.newTicket.picker.loadError')}</p>
          <p>{t(errorKeyOf(state.code))}</p>
          <button type="button" className="button" onClick={onRetry}>
            {t('management.common.retry')}
          </button>
        </div>
      ) : null}

      {state.kind === 'empty' ? <p className="state">{t('management.newTicket.picker.empty')}</p> : null}

      {state.kind === 'results' ? (
        <>
          <ul className="new-ticket__results" aria-label={t('management.newTicket.picker.resultsLabel')}>
            {state.items.map((customer) => {
              const place = customerPlace(customer);
              return (
                <li key={customer.id}>
                  <button type="button" className="button new-ticket__result" onClick={() => onPick(customer)}>
                    <span className="new-ticket__result-name">
                      <bdi>{customer.full_name}</bdi>
                    </span>
                    <span className="new-ticket__result-detail">
                      <bdi dir="ltr">{customer.phone}</bdi>
                    </span>
                    {place.city !== '' ? (
                      <span className="new-ticket__result-detail">
                        <bdi>{place.city}</bdi>
                      </span>
                    ) : null}
                    {place.apartment !== '' ? (
                      <span className="new-ticket__result-detail">
                        {t('management.newTicket.picker.apartment', { apartment: place.apartment })}
                      </span>
                    ) : null}
                  </button>
                </li>
              );
            })}
          </ul>
          {hasMoreMatches(state) ? <p className="field__hint">{t('management.newTicket.picker.refine')}</p> : null}
        </>
      ) : null}
    </section>
  );
}

// --- the chosen customer ----------------------------------------------------------------------------

export interface ChosenCustomerViewProps {
  readonly customer: Pick<StaffCustomer, 'full_name' | 'phone'>;
  readonly onChange: () => void;
  /** True while the ticket is being sent: the customer cannot be changed then. */
  readonly pending: boolean;
}

export function ChosenCustomerView({ customer, onChange, pending }: ChosenCustomerViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  return (
    <section className="card new-ticket__step new-ticket__chosen" aria-labelledby={NEW_TICKET_IDS.customerHeading}>
      <h2 id={NEW_TICKET_IDS.customerHeading} className="card__title" tabIndex={-1}>
        {t('management.newTicket.customer.title')}
      </h2>
      <p className="new-ticket__chosen-text">
        <bdi>{customer.full_name}</bdi>
        <span aria-hidden="true" className="new-ticket__separator" />
        <bdi dir="ltr">{customer.phone}</bdi>
      </p>
      <button type="button" className="button" disabled={pending} onClick={onChange}>
        {t('management.newTicket.customer.change')}
      </button>
    </section>
  );
}

// --- step 2: the form -------------------------------------------------------------------------------

/** The selected photo as the form shows it. */
export interface SelectedPhoto {
  readonly name: string;
}

export interface NewTicketFormViewProps {
  readonly values: FormValues;
  /** Field name to error code: the server's map plus the immediate photo-size hint. */
  readonly fieldErrors: FieldErrors;
  /** A message that belongs to no field, or null. */
  readonly notice: NewTicketNotice | null;
  readonly pending: boolean;
  readonly categories: readonly CategoryOption[];
  /** The current Field_Labels of the language (from `GET /staff/form-config`). */
  readonly labels: ResolvedLabels;
  readonly settings: FormSettings;
  readonly photo: SelectedPhoto | null;
  /** Changes when the photo is removed, so the browser's file input starts empty again. */
  readonly photoInputKey: number;
  readonly onChange: (patch: Partial<FormValues>) => void;
  readonly onPhotoChange: (file: File | null) => void;
  readonly onSubmit: () => void;
  /** Called by a link of the error summary: move focus to that field. */
  readonly onFocusField: (field: string) => void;
}

interface AriaProps {
  'aria-invalid'?: true;
  'aria-describedby'?: string;
}

export function NewTicketFormView({
  values,
  fieldErrors,
  notice,
  pending,
  categories,
  labels,
  settings,
  photo,
  photoInputKey,
  onChange,
  onPhotoChange,
  onSubmit,
  onFocusField,
}: NewTicketFormViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const photoEnabled = settings.photo_upload_enabled;
  const descriptionRequired = descriptionState(settings) === 'required';

  const mark = (state: 'required' | 'optional'): ReactNode => (
    <span className="new-ticket__mark"> {t(state === 'required' ? 'a11y.required' : 'a11y.optional')}</span>
  );
  const errorText = (field: string): string | null => {
    const key = fieldErrorKey(fieldErrors, field);
    return key === null ? null : `${t('a11y.errorPrefix')} ${t(key)}`;
  };
  const aria = (field: string, hasHint = false): AriaProps => {
    const props: AriaProps = {};
    const invalid = fieldErrorKey(fieldErrors, field) !== null;
    if (invalid) props['aria-invalid'] = true;
    const described = describedBy(field, invalid, hasHint);
    if (described !== undefined) props['aria-describedby'] = described;
    return props;
  };
  const error = (field: string): ReactNode => {
    const text = errorText(field);
    return text === null ? null : (
      <p id={errorId(field)} className="form__error">
        {text}
      </p>
    );
  };

  const summary = errorSummaryItems(fieldErrors, photoEnabled);
  const inForm = new Set<string>(fieldOrder(photoEnabled));

  return (
    <section className="card new-ticket__step" aria-labelledby={NEW_TICKET_IDS.formHeading}>
      <h2 id={NEW_TICKET_IDS.formHeading} className="card__title">
        {t('management.newTicket.form.title')}
      </h2>
      <p className="field__hint">{t('management.newTicket.form.requiredNote')}</p>

      {summary.length > 0 ? (
        <div id={SUMMARY_ID} className="feedback feedback--error" tabIndex={-1}>
          <p>{t('management.newTicket.form.errorSummary')}</p>
          <ul className="new-ticket__summary">
            {summary.map((item) => {
              const labelKey = labelKeyOfField(item.field);
              const message = String(t(item.textKey));
              const line = labelKey === null ? message : `${labels[labelKey]}: ${message}`;
              return (
                <li key={item.field}>
                  {inForm.has(item.field) ? (
                    <a
                      href={`#${fieldId(item.field)}`}
                      onClick={(event) => {
                        event.preventDefault();
                        onFocusField(item.field);
                      }}
                    >
                      {line}
                    </a>
                  ) : (
                    line
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      <form
        className="form new-ticket__form"
        noValidate
        aria-busy={pending}
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <div className="field">
          <label className="field__label" htmlFor={fieldId('city')}>
            {labels.city}
            {mark('required')}
          </label>
          <input
            id={fieldId('city')}
            name="city"
            className="input"
            type="text"
            dir="auto"
            value={values.city}
            aria-required="true"
            {...aria('city')}
            onChange={(event) => onChange({ city: event.target.value })}
          />
          {error('city')}
        </div>

        <div className="field">
          <label className="field__label" htmlFor={fieldId('apartment')}>
            {labels.apartment}
            {mark('required')}
          </label>
          <input
            id={fieldId('apartment')}
            name="apartment"
            className="input"
            type="text"
            dir="auto"
            value={values.apartment}
            aria-required="true"
            {...aria('apartment')}
            onChange={(event) => onChange({ apartment: event.target.value })}
          />
          {error('apartment')}
        </div>

        <div className="field">
          <label className="field__label" htmlFor={fieldId('phone')}>
            {labels.phone}
            {mark('required')}
          </label>
          <input
            id={fieldId('phone')}
            name="phone"
            className="input"
            type="tel"
            inputMode="numeric"
            dir="ltr"
            autoComplete="off"
            value={values.phone}
            aria-required="true"
            {...aria('phone')}
            onChange={(event) => onChange({ phone: event.target.value })}
          />
          {error('phone')}
        </div>

        <div className="field">
          <label className="field__label" htmlFor={fieldId('category_id')}>
            {labels.category}
            {mark('required')}
          </label>
          <select
            id={fieldId('category_id')}
            name="category_id"
            className="select"
            value={values.categoryId}
            aria-required="true"
            {...aria('category_id')}
            onChange={(event) => onChange({ categoryId: event.target.value })}
          >
            <option value="">{t('management.newTicket.form.categoryPlaceholder')}</option>
            {categories.map((category) => (
              <option key={category.id} value={category.id}>
                {category.name}
              </option>
            ))}
          </select>
          {error('category_id')}
        </div>

        <div className="field">
          <label className="field__label" htmlFor={fieldId('description')}>
            {labels.description}
            {mark(descriptionRequired ? 'required' : 'optional')}
          </label>
          <textarea
            id={fieldId('description')}
            name="description"
            className="input new-ticket__textarea"
            rows={5}
            dir="auto"
            value={values.description}
            aria-required={descriptionRequired ? 'true' : 'false'}
            {...aria('description')}
            onChange={(event) => onChange({ description: event.target.value })}
          />
          {error('description')}
        </div>

        {photoEnabled ? (
          <div className="field">
            <label className="field__label" htmlFor={fieldId('photo')}>
              {labels.photo}
              {mark('optional')}
            </label>
            <input
              key={photoInputKey}
              id={fieldId('photo')}
              name="photo"
              className="input"
              type="file"
              accept={PHOTO_ACCEPT}
              {...aria('photo', true)}
              onChange={(event) => onPhotoChange(event.target.files?.[0] ?? null)}
            />
            <p id={hintId('photo')} className="field__hint">
              {t('management.newTicket.form.photoHint')}
            </p>
            {error('photo')}
            {photo !== null ? (
              <div className="new-ticket__photo">
                <p>{t('management.newTicket.form.photoSelected', { file: isolateText(photo.name) })}</p>
                <button type="button" className="button" onClick={() => onPhotoChange(null)}>
                  {t('management.newTicket.form.photoRemove')}
                </button>
              </div>
            ) : null}
          </div>
        ) : null}

        <div className="field">
          <label className="checkbox-field new-ticket__check" htmlFor={fieldId('urgent')}>
            <input
              id={fieldId('urgent')}
              name="urgent"
              type="checkbox"
              checked={values.urgent}
              {...aria('urgent', true)}
              onChange={(event) => onChange({ urgent: event.target.checked })}
            />
            <span>{labels.urgent}</span>
          </label>
          <p id={hintId('urgent')} className="field__hint">
            {t('management.newTicket.form.urgentHelp')}
          </p>
          {error('urgent')}
        </div>

        {notice !== null ? (
          <p id={NOTICE_ID} className="feedback feedback--error" role="alert">
            {t(noticeKey(notice))}
          </p>
        ) : null}

        <div className="form__actions">
          <Link to={TICKET_LIST_PATH} className="button">
            {t('management.newTicket.form.cancel')}
          </Link>
          <button type="submit" className="button button--primary" disabled={pending}>
            {pending ? t('management.newTicket.form.submitting') : t('management.newTicket.form.submit')}
          </button>
        </div>
      </form>
    </section>
  );
}
