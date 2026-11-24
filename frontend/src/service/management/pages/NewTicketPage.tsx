/**
 * Route `/tickets/list/new` (task 29.5; R26.1, R26.2, R26.7, R23.12): Ticket_Staff open a ticket for a customer.
 *
 *  1. The picker searches active customers by name or phone (`GET /staff/customers?active=true&search=`).
 *  2. The form (labels, categories and options from `GET /staff/form-config`) opens prefilled from the chosen
 *     customer, and goes to `POST /staff/tickets` as multipart with `customer_id`.
 *
 * The container holds the state and the requests; the markup is in `newTicketView.tsx` and the decisions are in
 * `newTicketModel.ts` and the portal's pure form model. A failure never clears a value or the photo: the server's
 * field codes are shown beside the fields and focus moves to the first failing one (R23.12). On 201 the page
 * opens the new ticket's detail.
 */
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate } from 'react-router-dom';
import { FORM_LABEL_KEYS } from '../../api/formTypes';
import {
  getStaffFormConfig,
  listCustomers,
  openTicketForCustomer,
  staffKeys,
  type StaffCustomer,
} from '../../api/staff';
import type { FormConfig } from '../../api/formTypes';
import type { FieldErrors } from '../../portal/formErrors';
import {
  SUMMARY_ID,
  categoryOptions,
  errorFieldOf,
  fieldId,
  focusTarget,
  initialValues,
  resolveLabels,
  shownErrors,
  withoutFieldError,
  type FormValues,
} from '../../portal/screens/ticketFormModel';
import { TICKET_LIST_PATH } from '../dashboardModel';
import { SERVICE_NAMESPACE } from '../namespace';
import {
  buildStaffFormData,
  createdTicketPath,
  failureFromError,
  pickerQuery,
  pickerState,
  type NewTicketNotice,
} from '../newTicketModel';
import { ChosenCustomerView, CustomerPickerView, NEW_TICKET_IDS, NewTicketFormView } from '../newTicketView';
import { codeOf, errorKeyOf, languageOf } from '../ticketDetailModel';
import '../i18n';

export function NewTicketPage() {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const [customer, setCustomer] = useState<StaffCustomer | null>(null);
  // True after "Change customer": the picker then takes focus, because the control that was pressed is gone.
  const [changed, setChanged] = useState(false);
  return (
    <section className="page-body new-ticket">
      <div className="page-header">
        <h1>{t('management.newTicket.title')}</h1>
        <Link to={TICKET_LIST_PATH} className="button">
          {t('management.newTicket.back')}
        </Link>
      </div>
      {customer === null ? (
        <CustomerPicker focusSearch={changed} onPick={setCustomer} />
      ) : (
        <NewTicketLoader
          key={customer.id}
          customer={customer}
          onChangeCustomer={() => {
            setChanged(true);
            setCustomer(null);
          }}
        />
      )}
    </section>
  );
}

// --- step 1 ----------------------------------------------------------------------------------------

function CustomerPicker({ focusSearch, onPick }: { focusSearch: boolean; onPick: (customer: StaffCustomer) => void }) {
  const [draft, setDraft] = useState('');
  const [applied, setApplied] = useState('');
  const request = pickerQuery(applied);
  const list = useQuery({
    queryKey: staffKeys.customerList(request),
    queryFn: ({ signal }) => listCustomers(request, signal),
    placeholderData: keepPreviousData,
    retry: false,
  });

  useEffect(() => {
    if (focusSearch) document.getElementById(NEW_TICKET_IDS.search)?.focus();
  }, [focusSearch]);

  const state = pickerState({
    data: list.data,
    errorCode: list.isError ? codeOf(list.error) : null,
  });

  return (
    <CustomerPickerView
      searchDraft={draft}
      state={state}
      busy={list.isFetching}
      onSearchDraftChange={setDraft}
      onSearchSubmit={() => {
        const next = draft.trim();
        if (next === applied) void list.refetch();
        else setApplied(next);
      }}
      onPick={onPick}
      onRetry={() => void list.refetch()}
    />
  );
}

// --- step 2 ----------------------------------------------------------------------------------------

interface LoaderProps {
  readonly customer: StaffCustomer;
  readonly onChangeCustomer: () => void;
}

/** Loads the form configuration. The chosen customer and the way back stay on screen while it loads or fails. */
function NewTicketLoader({ customer, onChangeCustomer }: LoaderProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const config = useQuery({
    queryKey: staffKeys.formConfig,
    queryFn: ({ signal }) => getStaffFormConfig(signal),
    retry: false,
  });

  // The picker is gone, so the chosen customer takes focus (the form is not rendered yet).
  useEffect(() => {
    document.getElementById(NEW_TICKET_IDS.customerHeading)?.focus();
  }, []);

  if (config.data !== undefined) {
    return <NewTicketContainer customer={customer} config={config.data} onChangeCustomer={onChangeCustomer} />;
  }
  return (
    <>
      <ChosenCustomerView customer={customer} pending={false} onChange={onChangeCustomer} />
      {config.isError ? (
        <div className="feedback feedback--error" role="alert">
          <p>{t('management.newTicket.form.loadError')}</p>
          <p>{t(errorKeyOf(codeOf(config.error)))}</p>
          <button type="button" className="button" onClick={() => void config.refetch()}>
            {t('management.common.retry')}
          </button>
        </div>
      ) : (
        <p className="state" aria-busy="true">
          {t('management.common.loading')}
        </p>
      )}
    </>
  );
}

interface ContainerProps extends LoaderProps {
  readonly config: FormConfig;
}

function NewTicketContainer({ customer, config, onChangeCustomer }: ContainerProps) {
  const { t, i18n } = useTranslation(SERVICE_NAMESPACE);
  const language = languageOf(i18n.language);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Prefilled once, when the form opens, from the customer (R26.2). Nothing here is reset by a failure.
  const [values, setValues] = useState<FormValues>(() => initialValues(customer));
  const [photo, setPhoto] = useState<File | null>(null);
  const [photoInputKey, setPhotoInputKey] = useState(0);
  const [serverErrors, setServerErrors] = useState<FieldErrors>({});
  const [notice, setNotice] = useState<NewTicketNotice | null>(null);
  const [pending, setPending] = useState(false);
  const [focusRequest, setFocusRequest] = useState(0);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  const failedFields = useRef<FieldErrors>({});
  const photoEnabled = useRef(config.settings.photo_upload_enabled);
  photoEnabled.current = config.settings.photo_upload_enabled;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // After a validation failure: focus the first failing field in reading order, or the summary when the
  // failing fields are not on this form.
  useEffect(() => {
    if (focusRequest === 0) return;
    const errors = failedFields.current;
    const target = focusTarget(errors, photoEnabled.current);
    const id = target !== null ? fieldId(target) : Object.keys(errors).length > 0 ? SUMMARY_ID : null;
    if (id !== null) document.getElementById(id)?.focus();
  }, [focusRequest]);

  const fallbacks = Object.fromEntries(FORM_LABEL_KEYS.map((key) => [key, String(t(`fields.${key}`))])) as Record<
    (typeof FORM_LABEL_KEYS)[number],
    string
  >;
  const labels = resolveLabels(config.labels, language, fallbacks);
  const categories = categoryOptions(config.categories, language);

  const onChange = (patch: Partial<FormValues>): void => {
    setValues((previous) => ({ ...previous, ...patch }));
    setServerErrors((previous) => {
      let next = previous;
      for (const key of Object.keys(patch) as (keyof FormValues)[]) next = withoutFieldError(next, errorFieldOf(key));
      return next;
    });
  };

  const onPhotoChange = (file: File | null): void => {
    setPhoto(file);
    if (file === null) setPhotoInputKey((key) => key + 1);
    setServerErrors((previous) => withoutFieldError(previous, 'photo'));
  };

  const onFocusField = (field: string): void => {
    document.getElementById(fieldId(field))?.focus();
  };

  const onSubmit = (): void => {
    // One press sends one request, also when a second press lands before the re-render.
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    setNotice(null);
    setServerErrors({});
    void (async () => {
      try {
        const created = await openTicketForCustomer(buildStaffFormData(customer.id, values, photo, config.settings));
        queryClient.setQueryData(staffKeys.ticket(created.ticket_number), created);
        // The list, the dashboard and the reports changed; they reload when next shown.
        void queryClient.invalidateQueries({ queryKey: staffKeys.all, refetchType: 'none' });
        if (mounted.current) navigate(createdTicketPath(created), { replace: true });
      } catch (error) {
        if (!mounted.current) return;
        const failure = failureFromError(error);
        setServerErrors(failure.fieldErrors);
        setNotice(failure.notice);
        if (Object.keys(failure.fieldErrors).length > 0) {
          failedFields.current = failure.fieldErrors;
          setFocusRequest((request) => request + 1);
        }
      } finally {
        inFlight.current = false;
        if (mounted.current) setPending(false);
      }
    })();
  };

  return (
    <>
      <ChosenCustomerView customer={customer} pending={pending} onChange={onChangeCustomer} />
      <NewTicketFormView
        values={values}
        fieldErrors={shownErrors(serverErrors, photo, config.settings.photo_upload_enabled)}
        notice={notice}
        pending={pending}
        categories={categories}
        labels={labels}
        settings={config.settings}
        photo={photo === null ? null : { name: photo.name }}
        photoInputKey={photoInputKey}
        onChange={onChange}
        onPhotoChange={onPhotoChange}
        onSubmit={onSubmit}
        onFocusField={onFocusField}
      />
    </>
  );
}
