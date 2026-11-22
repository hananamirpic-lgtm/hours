// The modal shell of the configuration screens (R23.12): the category form and the delete confirmation open in
// it. Tab and Shift+Tab stay inside, Escape closes it unless a request is pending, and the title names it for a
// screen reader. Returning focus to the control that opened it is the page's job, because only the page knows
// whether that control still exists (a deleted category has no row left).
import { useEffect, useRef, type ReactNode } from 'react';
import { useFocusTrap } from '../portal/useFocusTrap';
import './config.css';

export interface ConfigDialogProps {
  /** The id of the title element, also named by `aria-labelledby`. */
  readonly titleId: string;
  readonly title: string;
  /** The id of the element that takes focus on open. The first focusable element when absent. */
  readonly initialFocusId?: string;
  /** The id of the element that describes the dialog (the confirmation text), when there is one. */
  readonly describedById?: string;
  /** True while a request is pending: Escape does nothing, so a half-sent change is never dismissed. */
  readonly busy: boolean;
  readonly onClose: () => void;
  readonly children: ReactNode;
}

export function ConfigDialog({ titleId, title, initialFocusId, describedById, busy, onClose, children }: ConfigDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(dialogRef, {
    onEscape: () => {
      if (!busy) onClose();
    },
    ...(initialFocusId === undefined ? {} : { initialFocusId }),
  });
  // While the request is pending the controls are disabled; focus sits on the dialog itself, so a
  // disabled button never drops it to the page behind.
  useEffect(() => {
    if (busy) dialogRef.current?.focus();
  }, [busy]);
  return (
    <div className="config-dialog-backdrop">
      <div
        ref={dialogRef}
        className="config-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        {...(describedById === undefined ? {} : { 'aria-describedby': describedById })}
        aria-busy={busy}
        tabIndex={-1}
      >
        <h2 id={titleId} className="config-dialog__title">
          {title}
        </h2>
        {children}
      </div>
    </div>
  );
}
