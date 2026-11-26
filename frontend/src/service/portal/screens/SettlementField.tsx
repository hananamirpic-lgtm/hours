// The settlement choice of the ticket form (task 30.8; R30.2, R30.5, R30.8, R23.3, R23.5, R23.6).
//
//   SettlementControl  the City field while the settlement list is active: a button that shows the chosen
//                      settlement as text (or a placeholder) and opens the dialog.
//   SettlementDialog   the selection window: modal dialog, labelled search box, list of options as buttons,
//                      a live region with the number of matches, Cancel.
//
// Keyboard: focus moves into the search box when the dialog opens, Tab and Shift+Tab stay inside
// (`useFocusTrap`), Escape closes it, and focus returns to the control that opened it. Arrow keys move
// between the options. Choosing an option closes the dialog and sets the field; the chosen settlement is
// kept by id, so the language switch only re-labels it.
//
// The dialog is rendered where the control is, inside the form. Its buttons are `type="button"` and Enter
// in the search box is stopped, so nothing in it submits the form.
import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import type { FormLocality } from '../../api/formTypes';
import { usePortalTranslation } from '../portalI18n';
import { useFocusTrap } from '../useFocusTrap';
import { fieldId } from './ticketFormModel';
import { filterSettlements, findSettlement, matchesText, settlementName, type SettlementLanguage } from './settlementModel';
import './ticketDetail.css';

export const SETTLEMENT_DIALOG_IDS = {
  title: 'settlement-dialog-title',
  search: 'settlement-dialog-search',
  status: 'settlement-dialog-status',
  list: 'settlement-dialog-list',
  cancel: 'settlement-dialog-cancel',
} as const;

/** The id of the element that holds the chosen settlement's text; the control is named by its label and this. */
export const SETTLEMENT_VALUE_ID = 'ticket-form-city-value';

export interface SettlementDialogProps {
  readonly localities: readonly FormLocality[];
  readonly language: SettlementLanguage;
  /** The id of the current choice, or '' for none. */
  readonly selectedId: string;
  readonly onChoose: (id: string) => void;
  readonly onCancel: () => void;
  /** The search text the dialog opens with. Only tests set it. */
  readonly initialQuery?: string;
}

export function SettlementDialog({ localities, language, selectedId, onChoose, onCancel, initialQuery = '' }: SettlementDialogProps) {
  const { t } = usePortalTranslation();
  const dialogRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const [query, setQuery] = useState(initialQuery);

  useFocusTrap(dialogRef, { onEscape: onCancel, initialFocusId: SETTLEMENT_DIALOG_IDS.search });

  const matches = filterSettlements(localities, query);
  const message = matchesText(matches.length);
  const messageText = String(message.count === undefined ? t(message.key) : t(message.key, { count: message.count }));

  const options = (): HTMLElement[] => Array.from(listRef.current?.querySelectorAll<HTMLElement>('[role="option"]') ?? []);

  const onSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>): void => {
    if (event.key === 'Enter') {
      // The dialog sits inside the form: Enter must not submit it.
      event.preventDefault();
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      options()[0]?.focus();
    }
  };

  const onListKeyDown = (event: KeyboardEvent<HTMLUListElement>): void => {
    const items = options();
    const at = items.findIndex((item) => item === document.activeElement);
    let next: number | null = null;
    if (event.key === 'ArrowDown') next = Math.min(at + 1, items.length - 1);
    else if (event.key === 'ArrowUp') next = at <= 0 ? -1 : at - 1;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = items.length - 1;
    if (next === null) return;
    event.preventDefault();
    if (next < 0) document.getElementById(SETTLEMENT_DIALOG_IDS.search)?.focus();
    else items[next]?.focus();
  };

  return (
    <div className="ticket-dialog-backdrop">
      <div
        ref={dialogRef}
        className="ticket-dialog settlement-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={SETTLEMENT_DIALOG_IDS.title}
        tabIndex={-1}
      >
        <h2 id={SETTLEMENT_DIALOG_IDS.title}>{t('portal.settlementDialog.title')}</h2>
        <div className="portal-field">
          <label htmlFor={SETTLEMENT_DIALOG_IDS.search}>{t('portal.settlementDialog.searchLabel')}</label>
          <input
            id={SETTLEMENT_DIALOG_IDS.search}
            type="search"
            dir="auto"
            autoComplete="off"
            value={query}
            aria-describedby={SETTLEMENT_DIALOG_IDS.status}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onSearchKeyDown}
          />
        </div>
        <p id={SETTLEMENT_DIALOG_IDS.status} className="settlement-status" role="status" aria-live="polite" aria-atomic="true">
          {messageText}
        </p>
        {matches.length > 0 && (
          <ul
            ref={listRef}
            id={SETTLEMENT_DIALOG_IDS.list}
            className="settlement-options"
            role="listbox"
            aria-labelledby={SETTLEMENT_DIALOG_IDS.title}
            onKeyDown={onListKeyDown}
          >
            {matches.map((locality) => (
              <li key={locality.id} role="presentation">
                <button
                  type="button"
                  role="option"
                  className="portal-button portal-button--quiet settlement-option"
                  aria-selected={locality.id === selectedId}
                  dir="auto"
                  onClick={() => onChoose(locality.id)}
                >
                  {settlementName(locality, language)}
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="ticket-dialog-actions">
          <button
            type="button"
            id={SETTLEMENT_DIALOG_IDS.cancel}
            className="portal-button portal-button--quiet"
            onClick={onCancel}
          >
            {t('portal.settlementDialog.cancel')}
          </button>
        </div>
      </div>
    </div>
  );
}

export interface SettlementControlProps {
  readonly localities: readonly FormLocality[];
  readonly language: SettlementLanguage;
  /** The id of the chosen settlement, or '' for none. */
  readonly value: string;
  /** The `id` of the visible `<label>`: the control is named by it and by the chosen text. */
  readonly labelId: string;
  readonly invalid: boolean;
  readonly describedBy: string | undefined;
  readonly onChange: (id: string) => void;
  /** Opens with the dialog showing. Only tests set it. */
  readonly initialOpen?: boolean;
  /** The search text the dialog opens with. Only tests set it. */
  readonly initialQuery?: string;
}

export function SettlementControl({
  localities,
  language,
  value,
  labelId,
  invalid,
  describedBy,
  onChange,
  initialOpen = false,
  initialQuery,
}: SettlementControlProps) {
  const { t } = usePortalTranslation();
  const [open, setOpen] = useState(initialOpen);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);

  // Focus goes back to the control that opened the dialog, whichever way the dialog closed.
  useEffect(() => {
    if (wasOpen.current && !open) buttonRef.current?.focus();
    wasOpen.current = open;
  }, [open]);

  const chosen = findSettlement(localities, value);

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        id={fieldId('city')}
        className="portal-button portal-button--quiet settlement-control"
        aria-haspopup="dialog"
        aria-labelledby={`${labelId} ${SETTLEMENT_VALUE_ID}`}
        {...(invalid ? { 'aria-invalid': true as const } : {})}
        {...(describedBy !== undefined ? { 'aria-describedby': describedBy } : {})}
        onClick={() => setOpen(true)}
      >
        <span
          id={SETTLEMENT_VALUE_ID}
          dir="auto"
          className={chosen === null ? 'settlement-control-placeholder' : 'settlement-control-value'}
        >
          {chosen === null ? t('portal.form.settlementPlaceholder') : settlementName(chosen, language)}
        </span>
      </button>
      {open && (
        <SettlementDialog
          localities={localities}
          language={language}
          selectedId={chosen?.id ?? ''}
          {...(initialQuery !== undefined ? { initialQuery } : {})}
          onChoose={(id) => {
            onChange(id);
            setOpen(false);
          }}
          onCancel={() => setOpen(false)}
        />
      )}
    </>
  );
}
