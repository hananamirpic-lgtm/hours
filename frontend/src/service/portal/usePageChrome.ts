// The per-screen page chrome (design "Accessibility measures", row "Page structure"):
//
//   - `document.title` is set for the screen and set again when the language changes;
//   - keyboard focus moves to the screen's <h1> when the route changes.
//
// Focus rules, so it never gets in the way:
//   - not on the first view after the page loads (the skip link stays the first tab stop);
//   - only when the screen or the path changes, never on a background query reload, a language change or
//     any other re-render (the effect depends on the view key, not on data);
//   - never while a dialog is open, because the dialog owns focus;
//   - `preventScroll` plus a scroll to the top, so the new screen starts at the top like a page load.
//
// `PageHeading` (PageHeading.tsx) is the one place that uses this hook, so a screen only renders it.
import { useEffect, useRef, type RefObject } from 'react';
import { useLocation } from 'react-router-dom';
import { usePortalTranslation } from './portalI18n';
import { documentTitle, isDialogOpen, pageViewKey, shouldMoveFocus, type PageTitleParams } from './pageStructure';

/** The view key of the previous screen view in this page load. `null` until the first view. */
let previousViewKey: string | null = null;

/** Forget the previous view, as after a fresh page load. For tests. */
export function resetPageChromeForTests(): void {
  previousViewKey = null;
}

/**
 * Sets the document title for `pageKey` and moves focus to the returned heading ref on a route change.
 * Attach the ref to an `<h1 tabIndex={-1}>`.
 */
export function usePageChrome(pageKey: string, params?: PageTitleParams | undefined): RefObject<HTMLHeadingElement> {
  const { t, i18n } = usePortalTranslation();
  const { pathname } = useLocation();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const language = i18n.language;
  const paramsKey = JSON.stringify(params ?? {});

  useEffect(() => {
    document.title = documentTitle(pageKey, JSON.parse(paramsKey) as PageTitleParams, (key, options) =>
      String(options === undefined ? t(key) : t(key, options)),
    );
    // `t` changes with the language; `language` makes the dependency explicit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageKey, paramsKey, language]);

  const viewKey = pageViewKey(pathname, pageKey);
  useEffect(() => {
    const previous = previousViewKey;
    previousViewKey = viewKey;
    if (!shouldMoveFocus(previous, viewKey, isDialogOpen(document))) return;
    headingRef.current?.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }, [viewKey]);

  return headingRef;
}
