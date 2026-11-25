/**
 * The presentational parts of the Resolve flow on the management ticket detail page (task 30.9; R29.4, R29.7,
 * R23.12): the Resolve form and the parts selection dialog. They take everything as props and fetch nothing, so
 * a markup test can render every state; `TicketDetailPage` owns the queries and the request.
 *
 * Every input has a visible label tied to it with `htmlFor`. A field error is tied to its field with
 * `aria-describedby`. The dialog is `ConfigDialog` (role dialog, aria-modal, labelled, focus trap, Escape).
 * The dialog sits beside the `<form>`, not inside it, so a key press in the dialog never submits the form.
 */
import { useEffect, useId, useRef, useState, type FormEvent, type Ref } from 'react';
import { useTranslation } from 'react-i18next';
import { SERVICE_NAMESPACE } from './namespace';
import { ConfigDialog } from './configDialog';
import {
  RESOLVE_PARTS_MAX,
  canChoosePart,
  chosenPartsText,
  firstFailingField,
  localFailure,
  orderedChosenIds,
  partName,
  resolveControlId,
  togglePart,
  validateResolve,
  withPartsReplaced,
  type PartsView,
  type ResolveFailure,
  type ResolveField,
  type ResolveValues,
} from './resolveModel';
import { errorKeyOf, type DetailLanguage } from './ticketDetailModel';
import './resolve.css';

// --- the parts selection dialog (R29.4) ------------------------------------------------------------

export interface ResolvePartsDialogProps {
  /** Prefix of the ids of the dialog and its checkboxes. */
  idPrefix: string;
  language: DetailLanguage;
  view: PartsView;
  /** The parts chosen when the dialog opens. The dialog edits a copy: Cancel keeps these. */
  initialIds: readonly string[];
  /** Done: the chosen ids, in the order of the list. */
  onDone: (ids: string[]) => void;
  onCancel: () => void;
  onRetry: () => void;
}

export function ResolvePartsDialog({ idPrefix, language, view, initialIds, onDone, onCancel, onRetry }: ResolvePartsDialogProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const [draft, setDraft] = useState<readonly string[]>(initialIds);
  const titleId = `${idPrefix}-parts-dialog-title`;
  const done = (): void => onDone(view.kind === 'ready' ? orderedChosenIds(view.parts, draft) : [...draft]);
  return (
    <ConfigDialog titleId={titleId} title={t('management.ticket.resolve.parts.title')} busy={false} onClose={onCancel}>
      {view.kind === 'loading' && <p role="status">{t('management.ticket.resolve.parts.loading')}</p>}
      {view.kind === 'error' && (
        <div className="feedback feedback--error" role="alert">
          <p>{t('management.ticket.resolve.parts.loadError')}</p>
          <p>{t(errorKeyOf(view.code))}</p>
          <button type="button" className="button" onClick={onRetry}>
            {t('management.common.retry')}
          </button>
        </div>
      )}
      {view.kind === 'ready' &&
        (view.parts.length === 0 ? (
          <p>{t('management.ticket.resolve.parts.empty')}</p>
        ) : (
          <fieldset className="resolve-parts-list">
            <legend className="field__label">{t('management.ticket.resolve.parts.legend')}</legend>
            {view.parts.map((part) => {
              const checked = draft.includes(part.id);
              return (
                <label className="checkbox-field" key={part.id}>
                  <input
                    type="checkbox"
                    id={`${idPrefix}-part-${part.id}`}
                    checked={checked}
                    disabled={!canChoosePart(draft, part.id)}
                    onChange={() => setDraft((current) => togglePart(current, part.id))}
                  />
                  <span>
                    <bdi>{partName(part, language)}</bdi>
                  </span>
                </label>
              );
            })}
          </fieldset>
        ))}
      <p className="subtitle" role="status" aria-live="polite">
        {t('management.ticket.resolve.parts.count', { selected: draft.length, max: RESOLVE_PARTS_MAX })}
      </p>
      <div className="form__actions">
        <button type="button" className="button" onClick={onCancel}>
          {t('management.common.cancel')}
        </button>
        <button type="button" className="button button--primary" onClick={done}>
          {t('management.ticket.resolve.parts.done')}
        </button>
      </div>
    </ConfigDialog>
  );
}

// --- the Resolve form (R29.4) ----------------------------------------------------------------------

export interface ResolvePanelViewProps {
  /** Distinguishes the ids of this form on the page. */
  idPrefix: string;
  language: DetailLanguage;
  values: ResolveValues;
  failure: ResolveFailure | null;
  pending: boolean;
  partsView: PartsView;
  dialogOpen: boolean;
  /** Ref of the "Choose parts…" button, so the container can return focus to it when the dialog closes. */
  chooseRef?: Ref<HTMLButtonElement>;
  onText: (field: 'entry_text' | 'technician_name', value: string) => void;
  onPartsReplaced: (checked: boolean) => void;
  onOpenParts: () => void;
  onPartsDone: (ids: string[]) => void;
  onPartsCancel: () => void;
  onRetryParts: () => void;
  onSubmit: () => void;
  onCancel: () => void;
}

/** The id of the form-level message, which takes focus after a failure that names no field. */
export function resolveAlertId(idPrefix: string): string {
  return `${idPrefix}-alert`;
}

export function ResolvePanelView({
  idPrefix,
  language,
  values,
  failure,
  pending,
  partsView,
  dialogOpen,
  chooseRef,
  onText,
  onPartsReplaced,
  onOpenParts,
  onPartsDone,
  onPartsCancel,
  onRetryParts,
  onSubmit,
  onCancel,
}: ResolvePanelViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const controlId = (field: ResolveField): string => resolveControlId(idPrefix, field);
  const errorId = (field: ResolveField): string => `${controlId(field)}-error`;
  const codeOfField = (field: ResolveField): string | null => failure?.fields[field] ?? null;
  // The part list is only on screen while the box is ticked, so its message is too.
  const partsCode = values.parts_replaced ? codeOfField('part_ids') : null;
  const replacedCode = codeOfField('parts_replaced');
  const textCode = codeOfField('entry_text');
  const nameCode = codeOfField('technician_name');
  const titleId = `${idPrefix}-title`;
  const chosenText = chosenPartsText(partsView.kind === 'ready' ? partsView.parts : [], values.part_ids, language);
  const describedBy = (...ids: Array<string | null>): string | undefined => {
    const list = ids.filter((id): id is string => id !== null);
    return list.length === 0 ? undefined : list.join(' ');
  };
  const submit = (event: FormEvent): void => {
    event.preventDefault();
    onSubmit();
  };
  return (
    <section className="card ticket-detail-section resolve-panel" aria-labelledby={titleId}>
      <h2 id={titleId} className="card__title">
        {t('management.ticket.resolve.title')}
      </h2>
      <p className="subtitle">{t('management.ticket.resolve.intro')}</p>
      <form className="form" onSubmit={submit} noValidate aria-labelledby={titleId}>
        {failure !== null && failure.code !== null && (
          <div id={resolveAlertId(idPrefix)} className="feedback feedback--error" role="alert" tabIndex={-1}>
            <p>{t('management.ticket.resolve.failed')}</p>
            <p>{t(errorKeyOf(failure.code))}</p>
          </div>
        )}

        <div className="field">
          <label className="field__label" htmlFor={controlId('entry_text')}>
            {t('management.ticket.resolve.historyText')} {t('a11y.required')}
          </label>
          <textarea
            id={controlId('entry_text')}
            className="input ticket-detail-textarea"
            rows={5}
            value={values.entry_text}
            required
            aria-required="true"
            aria-invalid={textCode !== null}
            aria-describedby={describedBy(`${idPrefix}-entry_text-hint`, textCode === null ? null : errorId('entry_text'))}
            disabled={pending}
            onChange={(event) => onText('entry_text', event.target.value)}
          />
          <span id={`${idPrefix}-entry_text-hint`} className="subtitle">
            {t('management.ticket.resolve.historyHint')}
          </span>
          {textCode !== null && (
            <p id={errorId('entry_text')} className="form__error">
              {t(errorKeyOf(textCode))}
            </p>
          )}
        </div>

        <div className="field">
          <label className="field__label" htmlFor={controlId('technician_name')}>
            {t('management.ticket.resolve.technicianName')} {t('a11y.required')}
          </label>
          <input
            id={controlId('technician_name')}
            className="input"
            type="text"
            value={values.technician_name}
            required
            aria-required="true"
            aria-invalid={nameCode !== null}
            aria-describedby={describedBy(nameCode === null ? null : errorId('technician_name'))}
            disabled={pending}
            onChange={(event) => onText('technician_name', event.target.value)}
          />
          {nameCode !== null && (
            <p id={errorId('technician_name')} className="form__error">
              {t(errorKeyOf(nameCode))}
            </p>
          )}
        </div>

        <div className="field">
          <label className="checkbox-field" htmlFor={controlId('parts_replaced')}>
            <input
              id={controlId('parts_replaced')}
              type="checkbox"
              checked={values.parts_replaced}
              aria-invalid={replacedCode !== null}
              aria-describedby={describedBy(replacedCode === null ? null : errorId('parts_replaced'))}
              disabled={pending}
              onChange={(event) => onPartsReplaced(event.target.checked)}
            />
            <span>{t('management.ticket.resolve.partsReplaced')}</span>
          </label>
          {replacedCode !== null && (
            <p id={errorId('parts_replaced')} className="form__error">
              {t(errorKeyOf(replacedCode))}
            </p>
          )}
          {values.parts_replaced && (
            <div className="resolve-parts">
              <button
                ref={chooseRef}
                id={controlId('part_ids')}
                type="button"
                className="button"
                aria-haspopup="dialog"
                aria-invalid={partsCode !== null}
                aria-describedby={describedBy(`${idPrefix}-parts-text`, partsCode === null ? null : errorId('part_ids'))}
                disabled={pending}
                onClick={onOpenParts}
              >
                {values.part_ids.length === 0
                  ? t('management.ticket.resolve.chooseParts')
                  : t('management.ticket.resolve.changeParts')}
              </button>
              <span id={`${idPrefix}-parts-text`} className="resolve-parts-text">
                {values.part_ids.length === 0 || chosenText === '' ? (
                  t('management.ticket.resolve.noPartsChosen')
                ) : (
                  <>
                    {t('management.ticket.resolve.chosenParts')} <bdi>{chosenText}</bdi>
                  </>
                )}
              </span>
              {partsCode !== null && (
                <p id={errorId('part_ids')} className="form__error">
                  {t(errorKeyOf(partsCode))}
                </p>
              )}
            </div>
          )}
        </div>

        <div className="form__actions">
          <button type="button" className="button" onClick={onCancel} disabled={pending}>
            {t('management.common.cancel')}
          </button>
          <button type="submit" className="button button--primary" disabled={pending}>
            {pending ? t('management.common.saving') : t('management.ticket.resolve.submit')}
          </button>
        </div>
      </form>
      {dialogOpen && (
        <ResolvePartsDialog
          idPrefix={idPrefix}
          language={language}
          view={partsView}
          initialIds={values.part_ids}
          onDone={onPartsDone}
          onCancel={onPartsCancel}
          onRetry={onRetryParts}
        />
      )}
    </section>
  );
}

// --- the container: values, pending flag, last failure, dialog -------------------------------------

export interface ResolvePanelProps {
  language: DetailLanguage;
  /** The values the form opens with (`resolveInitialValues`). */
  initial: ResolveValues;
  partsView: PartsView;
  onRetryParts: () => void;
  /** Sends the resolution. Resolves to null on success (the parent then closes the form) or to the failure. */
  onSubmit: (values: ResolveValues) => Promise<ResolveFailure | null>;
  onCancel: () => void;
}

/** Holds the typed values, the chosen parts, the pending flag, the last failure and the dialog of one form. */
export function ResolvePanel({ language, initial, partsView, onRetryParts, onSubmit, onCancel }: ResolvePanelProps) {
  const idPrefix = useId();
  const [values, setValues] = useState<ResolveValues>(initial);
  const [failure, setFailure] = useState<ResolveFailure | null>(null);
  const [pending, setPending] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const inFlight = useRef(false);
  const focusAfterFailure = useRef(false);
  const chooseRef = useRef<HTMLButtonElement>(null);
  const dialogWasOpen = useRef(false);

  // On open, the history text takes focus: it is the first thing to fill in.
  useEffect(() => {
    document.getElementById(resolveControlId(idPrefix, 'entry_text'))?.focus();
  }, [idPrefix]);

  // After a failure, focus the first failing field in reading order, or the message when no field failed.
  useEffect(() => {
    if (failure === null || !focusAfterFailure.current) return;
    focusAfterFailure.current = false;
    const first = firstFailingField(failure.fields);
    const target = first === null ? resolveAlertId(idPrefix) : resolveControlId(idPrefix, first);
    document.getElementById(target)?.focus();
  }, [failure, idPrefix]);

  // When the dialog closes (Done, Cancel or Escape), focus goes back to the control that opened it.
  useEffect(() => {
    if (dialogWasOpen.current && !dialogOpen) chooseRef.current?.focus();
    dialogWasOpen.current = dialogOpen;
  }, [dialogOpen]);

  const submit = async (): Promise<void> => {
    // One press of the submit control sends one request, also when a second press lands before the re-render.
    if (inFlight.current) return;
    const local = validateResolve(values);
    if (Object.keys(local).length > 0) {
      focusAfterFailure.current = true;
      setFailure(localFailure(local));
      return;
    }
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
    <ResolvePanelView
      idPrefix={idPrefix}
      language={language}
      values={values}
      failure={failure}
      pending={pending}
      partsView={partsView}
      dialogOpen={dialogOpen}
      chooseRef={chooseRef}
      onText={(field, value) => setValues((current) => ({ ...current, [field]: value }))}
      onPartsReplaced={(checked) => {
        setValues((current) => withPartsReplaced(current, checked));
        if (!checked) setDialogOpen(false);
      }}
      onOpenParts={() => setDialogOpen(true)}
      onPartsDone={(ids) => {
        setValues((current) => ({ ...current, part_ids: ids }));
        setDialogOpen(false);
      }}
      onPartsCancel={() => setDialogOpen(false)}
      onRetryParts={onRetryParts}
      onSubmit={() => {
        void submit();
      }}
      onCancel={onCancel}
    />
  );
}
