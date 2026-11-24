/**
 * The presentational parts of the management ticket detail page (task 24.4; R16.1, R16.4, R16.6, R16.9, R9.13,
 * R15.9, R15.10, R23.12). They take everything as props and fetch nothing, so a markup test can render every
 * state; `TicketDetailPage` owns the queries and the requests.
 *
 * Every input has a visible label tied to it with `htmlFor` (R23.12). A field error is tied to its field with
 * `aria-describedby`. Status and urgency are text, never colour alone. Ticket text is rendered as text only.
 */
import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import type { StaffHistoryEntry, StaffTicketDetail, TicketStatus } from '../api/staffTypes';
import { SERVICE_NAMESPACE } from './namespace';
import {
  ENTRY_FIELDS,
  ENTRY_TECHNICIAN_FIELD,
  ENTRY_TEXT_FIELD,
  SOS_ICON,
  actionKey,
  addFormValues,
  allowedTargets,
  buildDetailRows,
  editFormValues,
  entryTimes,
  errorKeyOf,
  isApproval,
  ltrIsolate,
  openedByStaff,
  type DetailLanguage,
  type DetailRow,
  type EntryFailure,
  type EntryField,
  type EntryValues,
  type PhotoView,
  type StatusFailure,
} from './ticketDetailModel';
import './ticketDetail.css';

// --- the fields (R16.1) ----------------------------------------------------------------------------

function RowValue({ row }: { row: DetailRow }) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  switch (row.kind) {
    case 'ltr':
      return <bdi dir="ltr">{row.text}</bdi>;
    case 'status':
      return <span className="pill">{t(`status.${row.text}`)}</span>;
    case 'sos':
      return (
        <span className="pill pill--danger ticket-detail-sos">
          <span aria-hidden="true">{SOS_ICON}</span>
          <span>{t('management.tickets.sos')}</span>
        </span>
      );
    case 'description':
      // Text only, never markup: React escapes it and `white-space: pre-wrap` keeps the line breaks (R9.13).
      return row.text.trim() === '' ? (
        <>{t('management.ticket.noDescription')}</>
      ) : (
        <span className="ticket-detail-description" dir="auto">
          {row.text}
        </span>
      );
    case 'text':
      return <bdi>{row.text}</bdi>;
  }
}

export function TicketFields({ ticket, language }: { ticket: StaffTicketDetail; language: DetailLanguage }) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const rows = buildDetailRows(ticket, language);
  const openedBy = openedByStaff(ticket);
  return (
    <section className="card ticket-detail-section" aria-labelledby="ticket-detail-fields-heading">
      <h2 id="ticket-detail-fields-heading" className="card__title">
        {t('management.ticket.details')}
      </h2>
      <dl className="ticket-detail-fields">
        {rows.map((row) => (
          <div className="ticket-detail-row" key={row.key}>
            <dt>{t(row.labelKey)}</dt>
            <dd>
              <RowValue row={row} />
            </dd>
          </div>
        ))}
      </dl>
      {openedBy !== null && (
        // Plain text (React escapes it): the username of the staff member who opened the ticket (R26.7).
        <p className="ticket-detail-opened-by">
          <bdi>{t('management.ticket.openedByStaff', { name: openedBy })}</bdi>
        </p>
      )}
    </section>
  );
}

// --- the photo (R16.1) -----------------------------------------------------------------------------

export interface PhotoSectionProps {
  ticketNumber: number;
  /** The category name in the current language, for the text alternative. */
  categoryLabel: string;
  view: PhotoView;
  onRetry: () => void;
}

/** A preview that opens the stored photo, or nothing at all when the ticket has none: no placeholder. */
export function PhotoSection({ ticketNumber, categoryLabel, view, onRetry }: PhotoSectionProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  if (view.kind === 'none') return null;
  return (
    <section className="card ticket-detail-section" aria-labelledby="ticket-detail-photo-heading">
      <h2 id="ticket-detail-photo-heading" className="card__title">
        {t('management.ticket.photo.heading')}
      </h2>
      {view.kind === 'loading' && <p>{t('management.ticket.photo.loading')}</p>}
      {view.kind === 'error' && (
        <div className="feedback feedback--error" role="alert">
          <p>{t('management.ticket.photo.error')}</p>
          {view.retryable && (
            <button type="button" className="button" onClick={onRetry}>
              {t('management.common.retry')}
            </button>
          )}
        </div>
      )}
      {view.kind === 'ready' && (
        <a className="ticket-detail-photo-link" href={view.url} target="_blank" rel="noopener noreferrer">
          <img
            className="ticket-detail-photo"
            src={view.url}
            alt={t('management.ticket.photo.alt', { number: String(ticketNumber), category: categoryLabel })}
            decoding="async"
          />
          <span>{t('management.ticket.photo.open')}</span>
        </a>
      )}
    </section>
  );
}

// --- status actions (R15.9, R15.10) ----------------------------------------------------------------

export interface StatusActionsProps {
  ticketNumber: number;
  /** The status the page displays. */
  status: TicketStatus;
  /** The target of a request in flight, or null. */
  pending: TicketStatus | null;
  /** The optional technician name that goes with the approval step. */
  technicianName: string;
  onTechnicianNameChange: (value: string) => void;
  onChange: (target: TicketStatus) => void;
  failure: StatusFailure | null;
}

export function StatusActions({
  ticketNumber,
  status,
  pending,
  technicianName,
  onTechnicianNameChange,
  onChange,
  failure,
}: StatusActionsProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const id = useId();
  const targets = allowedTargets(status);
  const asksName = targets.some((target) => isApproval(status, target));
  return (
    <section className="card ticket-detail-section" aria-labelledby={`${id}-heading`}>
      <h2 id={`${id}-heading`} className="card__title">
        {t('management.ticket.statusSection.title')}
      </h2>
      {targets.length === 0 ? (
        // A Cancelled ticket has no action that changes the status (R15.11).
        <p>{t('management.tickets.actions.none')}</p>
      ) : (
        <div className="form">
          {asksName && (
            <div className="field">
              <label className="field__label" htmlFor={`${id}-name`}>
                {t('management.ticket.statusSection.technicianOnApproval')}
              </label>
              <input
                id={`${id}-name`}
                className="input"
                type="text"
                value={technicianName}
                aria-describedby={`${id}-name-hint`}
                disabled={pending !== null}
                onChange={(event) => onTechnicianNameChange(event.target.value)}
              />
              <span id={`${id}-name-hint`} className="subtitle">
                {t('management.ticket.statusSection.technicianOnApprovalHint')}
              </span>
            </div>
          )}
          <div className="inline-actions">
            {targets.map((target) => {
              const key = actionKey(status, target);
              if (key === null) return null;
              return (
                <button
                  key={target}
                  type="button"
                  className="button button--primary"
                  disabled={pending !== null}
                  onClick={() => onChange(target)}
                >
                  {t(key)}
                </button>
              );
            })}
          </div>
        </div>
      )}
      {failure !== null && (
        <div className="feedback feedback--error" role="alert">
          <p>
            {t('management.tickets.statusChangeFailed', {
              action: t(failure.actionKey ?? 'management.common.actions'),
              number: ltrIsolate(String(ticketNumber)),
            })}
          </p>
          <p>{t(errorKeyOf(failure.code))}</p>
        </div>
      )}
    </section>
  );
}

// --- the assigned technician (R16.2, R16.7) --------------------------------------------------------

export interface AssignedEditorProps {
  value: string;
  saving: boolean;
  /** The code of the last failed save, or null. */
  errorCode: string | null;
  /** True after a save, until the next edit. */
  saved: boolean;
  onChange: (value: string) => void;
  onSave: () => void;
}

export function AssignedTechnicianEditor({ value, saving, errorCode, saved, onChange, onSave }: AssignedEditorProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const id = useId();
  const describedBy = errorCode === null ? `${id}-hint` : `${id}-hint ${id}-error`;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSave();
  };
  return (
    <section className="card ticket-detail-section" aria-labelledby={`${id}-heading`}>
      <h2 id={`${id}-heading`} className="card__title">
        {t('management.ticket.assigned.label')}
      </h2>
      <form className="form" onSubmit={submit} noValidate>
        <div className="field">
          <label className="field__label" htmlFor={`${id}-input`}>
            {t('management.ticket.assigned.label')}
          </label>
          <input
            id={`${id}-input`}
            className="input"
            type="text"
            value={value}
            placeholder={t('management.ticket.assigned.none')}
            aria-invalid={errorCode !== null}
            aria-describedby={describedBy}
            disabled={saving}
            onChange={(event) => onChange(event.target.value)}
          />
          <span id={`${id}-hint`} className="subtitle">
            {t('management.ticket.assigned.hint')}
          </span>
          {errorCode !== null && (
            <p id={`${id}-error`} className="form__error" role="alert">
              {t(errorKeyOf(errorCode))}
            </p>
          )}
        </div>
        <div className="inline-actions">
          <button type="submit" className="button button--primary" disabled={saving}>
            {saving ? t('management.common.saving') : t('management.ticket.assigned.save')}
          </button>
          {saved && errorCode === null && (
            <span className="feedback feedback--success" role="status">
              {t('management.ticket.assigned.saved')}
            </span>
          )}
        </div>
      </form>
    </section>
  );
}

// --- the history (R16.3 to R16.6, R16.9) -----------------------------------------------------------

export interface EntryFormViewProps {
  /** Distinguishes the add form from each edit form on the page. */
  idPrefix: string;
  /** The heading: `management.ticket.history.add` or `.edit`. */
  titleKey: string;
  values: EntryValues;
  failure: EntryFailure | null;
  pending: boolean;
  onChange: (field: EntryField, value: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
}

/** The add and edit form. The typed values come in as props, so an error never clears them (R16.6). */
export function HistoryEntryFormView({
  idPrefix,
  titleKey,
  values,
  failure,
  pending,
  onChange,
  onSubmit,
  onCancel,
}: EntryFormViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const fieldId = (field: EntryField): string => `${idPrefix}-${field}`;
  const errorId = (field: EntryField): string => `${idPrefix}-${field}-error`;
  const codeOfField = (field: EntryField): string | null => failure?.fields[field] ?? null;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit();
  };
  return (
    <form className="form ticket-detail-entry-form" onSubmit={submit} noValidate aria-labelledby={`${idPrefix}-title`}>
      <h3 id={`${idPrefix}-title`} className="card__title">
        {t(titleKey)}
      </h3>
      {failure !== null && failure.code !== null && (
        <p className="feedback feedback--error" role="alert">
          {t(errorKeyOf(failure.code))}
        </p>
      )}
      <div className="field">
        <label className="field__label" htmlFor={fieldId(ENTRY_TEXT_FIELD)}>
          {t('management.ticket.history.text')} {t('a11y.required')}
        </label>
        <textarea
          id={fieldId(ENTRY_TEXT_FIELD)}
          className="input ticket-detail-textarea"
          rows={4}
          value={values.entry_text}
          required
          aria-required="true"
          aria-invalid={codeOfField(ENTRY_TEXT_FIELD) !== null}
          aria-describedby={codeOfField(ENTRY_TEXT_FIELD) === null ? undefined : errorId(ENTRY_TEXT_FIELD)}
          disabled={pending}
          onChange={(event) => onChange(ENTRY_TEXT_FIELD, event.target.value)}
        />
        {codeOfField(ENTRY_TEXT_FIELD) !== null && (
          <p id={errorId(ENTRY_TEXT_FIELD)} className="form__error">
            {t(errorKeyOf(codeOfField(ENTRY_TEXT_FIELD) ?? ''))}
          </p>
        )}
      </div>
      <div className="field">
        <label className="field__label" htmlFor={fieldId(ENTRY_TECHNICIAN_FIELD)}>
          {t('management.ticket.history.technicianName')} {t('a11y.required')}
        </label>
        <input
          id={fieldId(ENTRY_TECHNICIAN_FIELD)}
          className="input"
          type="text"
          value={values.technician_name}
          required
          aria-required="true"
          aria-invalid={codeOfField(ENTRY_TECHNICIAN_FIELD) !== null}
          aria-describedby={codeOfField(ENTRY_TECHNICIAN_FIELD) === null ? undefined : errorId(ENTRY_TECHNICIAN_FIELD)}
          disabled={pending}
          onChange={(event) => onChange(ENTRY_TECHNICIAN_FIELD, event.target.value)}
        />
        {codeOfField(ENTRY_TECHNICIAN_FIELD) !== null && (
          <p id={errorId(ENTRY_TECHNICIAN_FIELD)} className="form__error">
            {t(errorKeyOf(codeOfField(ENTRY_TECHNICIAN_FIELD) ?? ''))}
          </p>
        )}
      </div>
      <div className="form__actions">
        <button type="button" className="button" onClick={onCancel} disabled={pending}>
          {t('management.common.cancel')}
        </button>
        <button type="submit" className="button button--primary" disabled={pending}>
          {pending ? t('management.common.saving') : t('management.ticket.history.save')}
        </button>
      </div>
    </form>
  );
}

export interface HistoryEntryFormProps {
  idPrefix: string;
  titleKey: string;
  initial: EntryValues;
  /** Saves the values. Resolves to null on success (the parent then closes the form) or to the failure. */
  onSubmit: (values: EntryValues) => Promise<EntryFailure | null>;
  onCancel: () => void;
}

/** Holds the typed values, the pending flag and the last failure of one form. */
export function HistoryEntryForm({ idPrefix, titleKey, initial, onSubmit, onCancel }: HistoryEntryFormProps) {
  const [values, setValues] = useState<EntryValues>(initial);
  const [failure, setFailure] = useState<EntryFailure | null>(null);
  const [pending, setPending] = useState(false);
  const inFlight = useRef(false);
  const focusAfterFailure = useRef(false);

  // After a failure, focus the first failing field, in reading order.
  useEffect(() => {
    if (failure === null || !focusAfterFailure.current) return;
    focusAfterFailure.current = false;
    const first = ENTRY_FIELDS.find((field) => failure.fields[field] !== undefined);
    if (first !== undefined) document.getElementById(`${idPrefix}-${first}`)?.focus();
  }, [failure, idPrefix]);

  const submit = async (): Promise<void> => {
    // One press of the save control sends one request, also when a second press lands before the re-render.
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    setFailure(null);
    const result = await onSubmit(values);
    inFlight.current = false;
    if (result !== null) {
      focusAfterFailure.current = true;
      setFailure(result);
      setPending(false);
    }
  };

  return (
    <HistoryEntryFormView
      idPrefix={idPrefix}
      titleKey={titleKey}
      values={values}
      failure={failure}
      pending={pending}
      onChange={(field, value) => setValues((current) => ({ ...current, [field]: value }))}
      onSubmit={() => {
        void submit();
      }}
      onCancel={onCancel}
    />
  );
}

/** Which form is open: none, the add form, or the edit form of one entry. Only one at a time. */
export type HistoryMode = { readonly kind: 'closed' } | { readonly kind: 'add' } | { readonly kind: 'edit'; readonly id: string };

export interface HistoryEntryItemProps {
  entry: StaffHistoryEntry;
  onEdit: () => void;
}

/** One entry: text, technician, creation time, and the last-edited time only when it was edited (R16.9). */
export function HistoryEntryItem({ entry, onEdit }: HistoryEntryItemProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const times = entryTimes(entry);
  return (
    <>
      <p className="ticket-detail-description" dir="auto">
        {entry.entry_text}
      </p>
      <p className="subtitle">
        <bdi>{t('management.ticket.history.by', { name: entry.technician_name })}</bdi>
      </p>
      <p className="subtitle">
        <bdi>{t('management.ticket.history.created', { when: times.created })}</bdi>
        {times.edited !== null && (
          <>
            <span aria-hidden="true" className="ticket-detail-separator" />
            <bdi>{t('management.ticket.history.edited', { when: times.edited })}</bdi>
          </>
        )}
      </p>
      <button type="button" className="button button--small" onClick={onEdit}>
        {t('management.ticket.history.edit')}
      </button>
    </>
  );
}

export interface HistorySectionProps {
  entries: readonly StaffHistoryEntry[];
  mode: HistoryMode;
  /** The acting username, prefilled as the technician name of a new entry (R16.4). */
  username: string;
  onOpenAdd: () => void;
  onOpenEdit: (id: string) => void;
  onClose: () => void;
  /** Saves an add (`mode.kind` add) or an edit of `id`. */
  onSubmit: (mode: HistoryMode, values: EntryValues) => Promise<EntryFailure | null>;
}

export function HistorySection({ entries, mode, username, onOpenAdd, onOpenEdit, onClose, onSubmit }: HistorySectionProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const id = useId();
  return (
    <section className="card ticket-detail-section" aria-labelledby={`${id}-heading`}>
      <div className="page-header ticket-detail-section-header">
        <h2 id={`${id}-heading`} className="card__title">
          {t('management.ticket.history.title')}
        </h2>
        {mode.kind !== 'add' && (
          <button type="button" className="button button--primary" onClick={onOpenAdd}>
            {t('management.ticket.history.add')}
          </button>
        )}
      </div>
      <p className="subtitle">{t('management.ticket.history.internalNote')}</p>
      {mode.kind === 'add' && (
        <HistoryEntryForm
          idPrefix={`${id}-add`}
          titleKey="management.ticket.history.add"
          initial={addFormValues(username)}
          onSubmit={(values) => onSubmit(mode, values)}
          onCancel={onClose}
        />
      )}
      {entries.length === 0 ? (
        <p>{t('management.ticket.history.empty')}</p>
      ) : (
        <ol className="ticket-detail-history">
          {entries.map((entry) => (
            <li key={entry.id} className="ticket-detail-history-item">
              {mode.kind === 'edit' && mode.id === entry.id ? (
                <HistoryEntryForm
                  idPrefix={`${id}-edit-${entry.id}`}
                  titleKey="management.ticket.history.edit"
                  initial={editFormValues(entry)}
                  onSubmit={(values) => onSubmit(mode, values)}
                  onCancel={onClose}
                />
              ) : (
                <HistoryEntryItem entry={entry} onEdit={() => onOpenEdit(entry.id)} />
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
