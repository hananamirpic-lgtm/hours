// The one <h1> of a portal screen (design "Page structure"). Every screen renders exactly one of these as
// the first element of its content. It sets the document title for `pageKey` and receives focus when the
// route changes, which is why it has `tabIndex={-1}` (focusable by script, not a tab stop).
//
//   <PageHeading pageKey="detail" params={{ number }}>{t('portal.pageTitle.detail', { number })}</PageHeading>
//
// `pageKey` is one of `PAGE_KEYS` in `pageStructure.ts`; `params` fills the title template (the ticket
// number on the detail page).
import type { ReactNode } from 'react';
import type { PageTitleParams } from './pageStructure';
import { usePageChrome } from './usePageChrome';

export interface PageHeadingProps {
  pageKey: string;
  params?: PageTitleParams | undefined;
  children: ReactNode;
}

export function PageHeading({ pageKey, params, children }: PageHeadingProps) {
  const ref = usePageChrome(pageKey, params);
  return (
    <h1 ref={ref} tabIndex={-1} className="portal-page-heading">
      {children}
    </h1>
  );
}
