// Thin hook over `focusTrap.ts`: traps Tab inside a modal dialog, closes it on Escape, moves focus into it
// when it mounts and pulls focus back if something outside it receives focus. Returning focus to the
// control that opened the dialog is the caller's job, because only the caller knows whether that control
// still exists (after a successful cancellation it does not).
import { useEffect, useRef, type RefObject } from 'react';
import { FOCUSABLE_SELECTOR, nextFocusId } from './focusTrap';

export interface FocusTrapOptions {
  /** Called on Escape. */
  onEscape: () => void;
  /** The `id` of the element that takes focus when the dialog mounts. The first focusable one when absent. */
  initialFocusId?: string;
}

function focusableIn(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
}

/** Mount the hook inside the dialog component, so it runs for exactly as long as the dialog is open. */
export function useFocusTrap(containerRef: RefObject<HTMLElement | null>, options: FocusTrapOptions): void {
  // The latest callback without re-subscribing the listeners on every render.
  const onEscape = useRef(options.onEscape);
  onEscape.current = options.onEscape;
  const { initialFocusId } = options;

  useEffect(() => {
    const container = containerRef.current;
    if (container === null) return undefined;

    const byId = initialFocusId === undefined ? null : document.getElementById(initialFocusId);
    const initial = byId !== null && container.contains(byId) ? byId : null;
    (initial ?? focusableIn(container)[0] ?? container).focus();

    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        onEscape.current();
        return;
      }
      if (event.key !== 'Tab' || event.altKey || event.ctrlKey || event.metaKey) return;
      const elements = focusableIn(container);
      const ids = elements.map((_, index) => String(index));
      const active = document.activeElement;
      const at = active instanceof HTMLElement ? elements.indexOf(active) : -1;
      const next = nextFocusId(ids, at < 0 ? null : String(at), event.shiftKey);
      event.preventDefault();
      (next === null ? container : elements[Number(next)] ?? container).focus();
    };

    const onFocusIn = (event: FocusEvent): void => {
      if (event.target instanceof Node && !container.contains(event.target)) {
        (focusableIn(container)[0] ?? container).focus();
      }
    };

    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('focusin', onFocusIn);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('focusin', onFocusIn);
    };
  }, [containerRef, initialFocusId]);
}
