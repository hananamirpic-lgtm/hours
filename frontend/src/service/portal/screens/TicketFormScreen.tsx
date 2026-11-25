// The ticket form (task 23.3; R9.1-R9.3, R9.5, R9.7, R9.11, R9.12, R10.8, R11.1, R22.8, R22.10, R23.1,
// R23.2, R23.6, R23.13, R23.14).
//
//   TicketFormScreen   route `/new`: loads the form configuration and the customer's record, keeps the
//                      page heading in one place so focus stays put while the data loads.
//   TicketFormContainer holds the state (values, photo, errors, pending) and does the submission.
//   TicketForm         presentational: props in, markup out. No query, no router state, no timers.
//
// Failure handling (all of it in `ticketFormModel.ts`): the container never clears a value or the photo
// on a failure. It sets the error state, re-enables the submit control in a `finally`, moves focus to the
// first failing field for a validation error, and announces a message that belongs to no field.
//
// The photo control is one native file input with `accept` and no `capture`, so a phone offers the camera
// and the gallery in one chooser (R11.1). The preview is an object URL, revoked when the photo changes
// and on unmount.
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { FORM_LABEL_KEYS, type FormConfig, type FormSettings } from '../../api/formTypes';
import { brandingLogoKey, createTicket, formConfigKey, getBrandingLogo, getFormConfig, getMe } from '../../api/ticketCreate';
import { ticketKeys } from '../../api/tickets';
import { useAnnouncer } from '../Announcer';
import type { FieldErrors } from '../formErrors';
import { PageHeading } from '../PageHeading';
import { directionOf, getLanguage, usePortalTranslation } from '../portalI18n';
import { portalKeys } from '../session';
import {
  CONFIRMATION_PATH,
  NOTICE_ID,
  PHOTO_ACCEPT,
  SUMMARY_ID,
  buildFormData,
  categoryOptions,
  confirmationState,
  describedBy,
  descriptionState,
  errorFieldOf,
  errorId,
  errorSummaryItems,
  failureFromError,
  fieldErrorKey,
  fieldId,
  fieldOrder,
  focusTarget,
  hintId,
  initialValues,
  isolateText,
  labelKeyOfField,
  noticeTexts,
  resolveLabels,
  shownErrors,
  withoutFieldError,
  type CategoryOption,
  type FormNotice,
  type FormValues,
  type PrefillSource,
  type ResolvedLabels,
  type TextRef,
} from './ticketFormModel';
import { BrandingHeader } from './BrandingHeader';
import { SettlementControl } from './SettlementField';
import { brandingView, settlementChoice, type BrandingView, type SettlementChoice } from './settlementModel';
import './ticketForm.css';

// --- presentational --------------------------------------------------------------------------------

/** The selected photo as the form shows it. */
export interface PhotoView {
  readonly name: string;
  /** The object URL of the preview; null while it is not ready. */
  readonly previewUrl: string | null;
}

export interface TicketFormProps {
  readonly values: FormValues;
  /** Field name to error code: the server's map plus the immediate photo-size hint. */
  readonly fieldErrors: FieldErrors;
  /** A message that belongs to no field (server error, rate limit, numbers exhausted), or null. */
  readonly notice: FormNotice | null;
  readonly pending: boolean;
  readonly categories: readonly CategoryOption[];
  readonly labels: ResolvedLabels;
  readonly settings: FormSettings;
  /** The settlement list while it is active (R30.2); absent or not required means the City field is free text (R30.3). */
  readonly settlements?: SettlementChoice;
  readonly photo: PhotoView | null;
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

/** The id of the City label while the field is the settlement control, which the control is named by. */
const CITY_LABEL_ID = 'ticket-form-city-label';

function FieldLabel({ htmlFor, text, mark, id }: { htmlFor: string; text: string; mark: string; id?: string }) {
  return (
    <label htmlFor={htmlFor} {...(id !== undefined ? { id } : {})}>
      {text}
      {mark !== '' && <span className="portal-field-mark">{mark}</span>}
    </label>
  );
}

function FieldError({ field, text }: { field: string; text: string | null }) {
  if (text === null) return null;
  return (
    <p id={errorId(field)} className="portal-error">
      {text}
    </p>
  );
}

/** The one `<h1>` of the screen and its language and direction. Stays mounted while the form loads. */
export function TicketFormFrame({ children, branding = null }: { children: ReactNode; branding?: BrandingView | null }) {
  const { t, i18n } = usePortalTranslation();
  const language = getLanguage(i18n);
  return (
    <section className="portal-ticket-form" lang={language} dir={directionOf(language)}>
      <BrandingHeader view={branding} />
      <PageHeading pageKey="newTicket">{t('portal.form.title')}</PageHeading>
      {children}
    </section>
  );
}

export function TicketForm({
  values,
  fieldErrors,
  notice,
  pending,
  categories,
  labels,
  settings,
  settlements,
  photo,
  photoInputKey,
  onChange,
  onPhotoChange,
  onSubmit,
  onFocusField,
}: TicketFormProps) {
  const { t, i18n } = usePortalTranslation();
  const language = getLanguage(i18n);
  const required = ` ${t('a11y.required')}`;
  const optional = ` ${t('a11y.optional')}`;
  const descriptionMark = descriptionState(settings) === 'required' ? required : optional;
  const photoEnabled = settings.photo_upload_enabled;

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
  const text = (ref: TextRef): string => String(ref.count === undefined ? t(ref.key) : t(ref.key, { count: ref.count }));

  const summary = errorSummaryItems(fieldErrors, photoEnabled);
  const inForm = new Set<string>(fieldOrder(photoEnabled));

  return (
    <>
      <p>{t('portal.form.intro')}</p>
      <p className="portal-hint">{t('portal.form.requiredNote')}</p>
      {summary.length > 0 && (
        <div id={SUMMARY_ID} className="portal-notice portal-notice--error portal-error-summary" tabIndex={-1}>
          <p>{t('portal.form.errorSummary')}</p>
          <ul>
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
      )}
      <form
        noValidate
        lang={language}
        dir={directionOf(language)}
        aria-busy={pending}
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <div className="portal-field">
          {settlements?.required === true ? (
            <>
              <FieldLabel htmlFor={fieldId('city')} text={labels.city} mark={required} id={CITY_LABEL_ID} />
              <SettlementControl
                localities={settlements.localities}
                language={language}
                value={values.localityId ?? ''}
                labelId={CITY_LABEL_ID}
                invalid={fieldErrorKey(fieldErrors, 'city') !== null}
                describedBy={describedBy('city', fieldErrorKey(fieldErrors, 'city') !== null, false)}
                onChange={(id) => onChange({ localityId: id })}
              />
            </>
          ) : (
            <>
              <FieldLabel htmlFor={fieldId('city')} text={labels.city} mark={required} />
              <input
                id={fieldId('city')}
                name="city"
                type="text"
                dir="auto"
                value={values.city}
                aria-required="true"
                {...aria('city')}
                onChange={(event) => onChange({ city: event.target.value })}
              />
            </>
          )}
          <FieldError field="city" text={errorText('city')} />
        </div>

        <div className="portal-field">
          <FieldLabel htmlFor={fieldId('apartment')} text={labels.apartment} mark={required} />
          <input
            id={fieldId('apartment')}
            name="apartment"
            type="text"
            dir="auto"
            value={values.apartment}
            aria-required="true"
            {...aria('apartment')}
            onChange={(event) => onChange({ apartment: event.target.value })}
          />
          <FieldError field="apartment" text={errorText('apartment')} />
        </div>

        <div className="portal-field">
          <FieldLabel htmlFor={fieldId('phone')} text={labels.phone} mark={required} />
          <input
            id={fieldId('phone')}
            name="phone"
            type="tel"
            inputMode="numeric"
            dir="ltr"
            autoComplete="tel"
            value={values.phone}
            aria-required="true"
            {...aria('phone')}
            onChange={(event) => onChange({ phone: event.target.value })}
          />
          <FieldError field="phone" text={errorText('phone')} />
        </div>

        <div className="portal-field">
          <FieldLabel htmlFor={fieldId('category_id')} text={labels.category} mark={required} />
          <select
            id={fieldId('category_id')}
            name="category_id"
            value={values.categoryId}
            aria-required="true"
            {...aria('category_id')}
            onChange={(event) => onChange({ categoryId: event.target.value })}
          >
            <option value="">{t('portal.form.categoryPlaceholder')}</option>
            {categories.map((category) => (
              <option key={category.id} value={category.id}>
                {category.name}
              </option>
            ))}
          </select>
          <FieldError field="category_id" text={errorText('category_id')} />
        </div>

        <div className="portal-field">
          <FieldLabel htmlFor={fieldId('description')} text={labels.description} mark={descriptionMark} />
          <textarea
            id={fieldId('description')}
            name="description"
            rows={5}
            dir="auto"
            value={values.description}
            aria-required={descriptionState(settings) === 'required' ? 'true' : 'false'}
            {...aria('description')}
            onChange={(event) => onChange({ description: event.target.value })}
          />
          <FieldError field="description" text={errorText('description')} />
        </div>

        {photoEnabled && (
          <div className="portal-field">
            <FieldLabel htmlFor={fieldId('photo')} text={labels.photo} mark={optional} />
            <input
              key={photoInputKey}
              id={fieldId('photo')}
              name="photo"
              type="file"
              accept={PHOTO_ACCEPT}
              {...aria('photo', true)}
              onChange={(event) => onPhotoChange(event.target.files?.[0] ?? null)}
            />
            <p id={hintId('photo')} className="portal-hint">
              {t('portal.form.photoHint')}
            </p>
            <FieldError field="photo" text={errorText('photo')} />
            {photo !== null && (
              <div className="portal-photo-preview">
                <p>{t('portal.form.photoSelected', { file: isolateText(photo.name) })}</p>
                {photo.previewUrl !== null && (
                  <img className="portal-photo-preview-img" src={photo.previewUrl} alt={t('portal.form.photoPreviewAlt')} />
                )}
                <button type="button" className="portal-button portal-button--quiet" onClick={() => onPhotoChange(null)}>
                  {t('portal.form.photoRemove')}
                </button>
              </div>
            )}
          </div>
        )}

        <div className="portal-field">
          <div className="portal-check-row">
            <input
              id={fieldId('urgent')}
              name="urgent"
              type="checkbox"
              checked={values.urgent}
              {...aria('urgent', true)}
              onChange={(event) => onChange({ urgent: event.target.checked })}
            />
            <label htmlFor={fieldId('urgent')}>{labels.urgent}</label>
          </div>
          <p id={hintId('urgent')} className="portal-hint">
            {t('portal.form.urgentHelp')}
          </p>
          <FieldError field="urgent" text={errorText('urgent')} />
        </div>

        {notice !== null && (
          <div id={NOTICE_ID} className="portal-notice portal-notice--error">
            {noticeTexts(notice).map((ref) => (
              <p key={ref.key}>{text(ref)}</p>
            ))}
          </div>
        )}

        <div className="portal-form-actions">
          <button type="submit" className="portal-button" disabled={pending}>
            {pending ? t('portal.form.submitting') : t('portal.form.submit')}
          </button>
          <Link to="/" className="portal-button portal-button--quiet">
            {t('portal.form.cancel')}
          </Link>
        </div>
      </form>
    </>
  );
}

// --- container -------------------------------------------------------------------------------------

/** An object URL for the chosen photo, revoked when the photo changes and on unmount. Null until it exists. */
function usePhotoPreview(photo: File | null): string | null {
  const [preview, setPreview] = useState<{ file: File; url: string } | null>(null);
  useEffect(() => {
    if (photo === null || typeof URL.createObjectURL !== 'function') {
      setPreview(null);
      return;
    }
    const url = URL.createObjectURL(photo);
    setPreview({ file: photo, url });
    return () => {
      URL.revokeObjectURL(url);
    };
  }, [photo]);
  return preview !== null && preview.file === photo ? preview.url : null;
}

interface ContainerProps {
  readonly config: FormConfig;
  readonly me: PrefillSource;
}

function TicketFormContainer({ config, me }: ContainerProps) {
  const { t, i18n } = usePortalTranslation();
  const language = getLanguage(i18n);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { announce, alert } = useAnnouncer();

  // Prefilled once, when the form opens. Nothing here is reset by a failure.
  const settlements = settlementChoice(config);
  const [values, setValues] = useState<FormValues>(() => initialValues(me, settlements));
  const [photo, setPhoto] = useState<File | null>(null);
  const [photoInputKey, setPhotoInputKey] = useState(0);
  const [serverErrors, setServerErrors] = useState<FieldErrors>({});
  const [notice, setNotice] = useState<FormNotice | null>(null);
  const [pending, setPending] = useState(false);
  const [focusRequest, setFocusRequest] = useState(0);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  const failedFields = useRef<FieldErrors>({});
  const photoEnabled = useRef(config.settings.photo_upload_enabled);
  photoEnabled.current = config.settings.photo_upload_enabled;
  const previewUrl = usePhotoPreview(photo);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // After a validation failure: focus the first failing field in reading order (R23.13), or the summary
  // when the failing fields are not on this form.
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
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    setNotice(null);
    setServerErrors({});
    void (async () => {
      try {
        const ticketNumber = await createTicket(buildFormData(values, photo, config.settings, settlements.required));
        void queryClient.invalidateQueries({ queryKey: ticketKeys.all });
        announce(String(i18n.t('a11y.live.ticketSubmitted', { number: ticketNumber })));
        if (mounted.current) navigate(CONFIRMATION_PATH, { replace: true, state: confirmationState(ticketNumber) });
      } catch (error) {
        const failure = failureFromError(error);
        if (!mounted.current) return;
        setServerErrors(failure.fieldErrors);
        setNotice(failure.notice);
        if (failure.notice !== null) {
          // A message that belongs to no field is announced without moving focus (R23.14).
          alert(
            noticeTexts(failure.notice)
              .map((ref) => String(ref.count === undefined ? i18n.t(ref.key) : i18n.t(ref.key, { count: ref.count })))
              .join(' '),
          );
        }
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
    <TicketForm
      values={values}
      fieldErrors={shownErrors(serverErrors, photo, config.settings.photo_upload_enabled)}
      notice={notice}
      pending={pending}
      categories={categories}
      labels={labels}
      settings={config.settings}
      settlements={settlements}
      photo={photo === null ? null : { name: photo.name, previewUrl }}
      photoInputKey={photoInputKey}
      onChange={onChange}
      onPhotoChange={onPhotoChange}
      onSubmit={onSubmit}
      onFocusField={onFocusField}
    />
  );
}

function useFormConfig() {
  return useQuery({ queryKey: formConfigKey, queryFn: ({ signal }) => getFormConfig(signal) });
}

function useMe() {
  // The key the session gate uses, so the record is usually already cached.
  return useQuery({ queryKey: portalKeys.me, queryFn: ({ signal }) => getMe(signal) });
}

function TicketFormLoader() {
  const { t } = usePortalTranslation();
  const config = useFormConfig();
  const me = useMe();
  if (config.isError || me.isError) {
    const retry = (): void => {
      if (config.isError) void config.refetch();
      if (me.isError) void me.refetch();
    };
    return (
      <div className="portal-notice portal-notice--error portal-form-load-error" role="alert">
        <p>{t('portal.form.loadError')}</p>
        <button type="button" className="portal-button" onClick={retry}>
          {t('portal.form.retry')}
        </button>
      </div>
    );
  }
  if (config.data === undefined || me.data === undefined) return <p>{t('portal.loading')}</p>;
  return <TicketFormContainer config={config.data} me={me.data} />;
}

/**
 * An object URL for the logo, fetched with the bearer header only when the configuration says there is
 * one. Revoked when the logo changes and on unmount. Null until it exists and after a failed fetch: a logo
 * that cannot be loaded is simply not shown, and the form is never blocked by it (R28.3).
 */
function useBrandingLogoUrl(hasLogo: boolean): string | null {
  const logo = useQuery({
    queryKey: brandingLogoKey,
    queryFn: ({ signal }) => getBrandingLogo(signal),
    enabled: hasLogo,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 5 * 60_000,
  });
  const blob = hasLogo && logo.data !== undefined ? logo.data : null;
  const [object, setObject] = useState<{ blob: Blob; url: string } | null>(null);
  useEffect(() => {
    if (blob === null || typeof URL.createObjectURL !== 'function') {
      setObject(null);
      return;
    }
    const url = URL.createObjectURL(blob);
    setObject({ blob, url });
    return () => {
      URL.revokeObjectURL(url);
    };
  }, [blob]);
  return object !== null && object.blob === blob ? object.url : null;
}

/** Route `/new`. */
export function TicketFormScreen() {
  // The same query as the loader below: one request, shared through the cache.
  const config = useFormConfig();
  const branding = config.data?.branding;
  const logoUrl = useBrandingLogoUrl(branding?.has_logo === true);
  return (
    <TicketFormFrame branding={brandingView(branding, logoUrl)}>
      <TicketFormLoader />
    </TicketFormFrame>
  );
}
