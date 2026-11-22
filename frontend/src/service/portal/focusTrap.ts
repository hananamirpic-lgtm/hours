// The focus-trap decision of a modal dialog (R23.3: no keyboard trap outside the dialog's own Escape and
// dismiss, focus stays inside while the dialog is open). Pure: no React, no DOM.
//
// `useFocusTrap` reads the focusable elements of the dialog in document order, gives each an id (its
// index), and asks `nextFocusId` where Tab or Shift+Tab goes. The browser's own Tab order is never relied
// on, so the result is the same in every browser and in both text directions (document order is the
// reading order in right-to-left and left-to-right alike).

/**
 * The id that receives focus when Tab (or Shift+Tab with `shiftKey`) is pressed inside the dialog.
 *
 * - `ids` empty: `null`. Nothing in the dialog can take focus; the caller keeps focus on the dialog itself.
 * - `currentId` is not in `ids` (focus is on the dialog container, or somewhere outside): Tab goes to the
 *   first id and Shift+Tab to the last.
 * - Otherwise the next id in `ids`, wrapping from the last to the first; Shift+Tab the previous id,
 *   wrapping from the first to the last.
 * - One id: that id, forward or backward.
 */
export function nextFocusId<T extends string>(
  ids: readonly T[],
  currentId: string | null,
  shiftKey: boolean,
): T | null {
  const last = ids.length - 1;
  if (last < 0) return null;
  const index = currentId === null ? -1 : ids.indexOf(currentId as T);
  if (index < 0) return (shiftKey ? ids[last] : ids[0]) as T;
  if (shiftKey) return (index === 0 ? ids[last] : ids[index - 1]) as T;
  return (index === last ? ids[0] : ids[index + 1]) as T;
}

/** Elements a Tab can reach inside a dialog. Disabled controls and `tabindex="-1"` are left out. */
export const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');
