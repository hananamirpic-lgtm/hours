// The one <h1> of a portal screen (design "Page structure"). Every screen renders exactly one of these as
// the first element of its content. It sets the document title for `pageKey` and receives focus when the
// route changes, which is why it has `tabIndex={-1}` (focusable by script, not a tab stop).
//
//   <PageHeading pageKey="detail" params={{ number }}>{t('portal.pageTitle.detail', { number })}</PageHeading>
//
// `pageKey` is one of `PAGE_KEYS` in `pageStructure.ts`; `params` fills the title template (the ticket
// number on the detail page).
//
// A screen that must move focus to its own heading for another reason (the ticket list on a page change,
// the ticket detail after its dialog closes) passes `headingRef`; it receives the same element.
import { useCallback, type ReactNode, type Ref } from 'react';
import type { PageTitleParams } from './pageStructure';
import { usePageChrome } from './usePageChrome';

export interface PageHeadingProps {
  pageKey: string;
  params?: PageTitleParams | undefined;
  /** Optional: also receives the `<h1>` element. */
  headingRef?: Ref<HTMLHeadingElement> | null | undefined;
  children: ReactNode;
}

export function PageHeading({ pageKey, params, headingRef, children }: PageHeadingProps) {
  const chromeRef = usePageChrome(pageKey, params);
  const setRef = useCallback(
    (element: HTMLHeadingElement | null) => {
      (chromeRef as { current: HTMLHeadingElement | null }).current = element;
      if (typeof headingRef === 'function') headingRef(element);
      else if (headingRef !== null && headingRef !== undefined) {
        (headingRef as { current: HTMLHeadingElement | null }).current = element;
      }
    },
    [chromeRef, headingRef],
  );
  return (
    <h1 ref={setRef} tabIndex={-1} className="portal-page-heading">
      {children}
    </h1>
  );
}
