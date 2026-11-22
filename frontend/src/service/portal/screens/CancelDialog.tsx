// The cancellation confirmation (R13.2, R23.3). Presentational: it holds no ticket data and sends no
// request. The container owns its open state and the request, so a 30-second reload of the ticket changes
// nothing here (R12.5): the dialog is a sibling of the ticket view, mounted by `phase`, never by data.
//
// Keyboard: focus moves to the dismiss control when it opens (the safe choice for a destructive action),
// Tab and Shift+Tab stay inside, Escape dismisses. While the request is pending both controls are
// disabled; focus then sits on the dialog itself, so a disabled button never drops it to the page.
import { useEffect, useRef } from 'react';
import { usePortalTranslation } from '../portalI18n';
import { useFocusTrap } from '../useFocusTrap';
import { ltrIsolate } from './ticketDetailModel';

export const CANCEL_DIALOG_IDS = {
  title: 'portal-cancel-dialog-title',
  description: 'portal-cancel-dialog-description',
  confirm: 'portal-cancel-dialog-confirm',
  dismiss: 'portal-cancel-dialog-dismiss',
} as const;

export interface CancelDialogProps {
  ticketNumber: number;
  /** True while the cancellation request awaits its answer. */
  pending: boolean;
  onConfirm: () => void;
  onDismiss: () => void;
}

export function CancelDialog({ ticketNumber, pending, onConfirm, onDismiss }: CancelDialogProps) {
  const { t } = usePortalTranslation();
  const dialogRef = useRef<HTMLDivElement>(null);

  useFocusTrap(dialogRef, {
    onEscape: () => {
      if (!pending) onDismiss();
    },
    initialFocusId: CANCEL_DIALOG_IDS.dismiss,
  });

  useEffect(() => {
    if (pending) dialogRef.current?.focus();
  }, [pending]);

  return (
    <div className="ticket-dialog-backdrop">
      <div
        ref={dialogRef}
        className="ticket-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={CANCEL_DIALOG_IDS.title}
        aria-describedby={CANCEL_DIALOG_IDS.description}
        aria-busy={pending}
        tabIndex={-1}
      >
        <h2 id={CANCEL_DIALOG_IDS.title}>{t('portal.detail.cancelDialog.title')}</h2>
        <p id={CANCEL_DIALOG_IDS.description}>
          {t('portal.detail.cancelDialog.message', { number: ltrIsolate(String(ticketNumber)) })}
        </p>
        <div className="ticket-dialog-actions">
          <button
            type="button"
            id={CANCEL_DIALOG_IDS.confirm}
            className="portal-button portal-button--danger"
            disabled={pending}
            onClick={onConfirm}
          >
            {pending ? t('portal.detail.cancelDialog.pending') : t('portal.detail.cancelDialog.confirm')}
          </button>
          <button
            type="button"
            id={CANCEL_DIALOG_IDS.dismiss}
            className="portal-button portal-button--quiet"
            disabled={pending}
            onClick={onDismiss}
          >
            {t('portal.detail.cancelDialog.dismiss')}
          </button>
        </div>
      </div>
    </div>
  );
}
