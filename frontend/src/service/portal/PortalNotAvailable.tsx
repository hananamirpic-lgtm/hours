// The portal's "service unavailable" page, shown by `FlagGate` when the flag is off (R8.2, R21.5).
//
// Same text and layout as the shared `NotAvailable` page (Hebrew and English together, no control of any
// kind), with the one difference that its `<h1>` is the portal `PageHeading`, so the page has a document
// title and takes focus like every other portal screen. The shared page, used by the staff app, is not
// changed.
import { BILINGUAL_SEPARATOR, NOT_AVAILABLE_TEXT } from '../gates/flagGateStrings';
import { pageStyle } from '../gates/NotAvailable';
import { PageHeading } from './PageHeading';

export function PortalNotAvailable() {
  return (
    <main style={pageStyle}>
      <PageHeading pageKey="unavailable">
        <span lang="he" dir="rtl">
          {NOT_AVAILABLE_TEXT.he}
        </span>{' '}
        <span aria-hidden="true">{BILINGUAL_SEPARATOR}</span>{' '}
        <span lang="en" dir="ltr">
          {NOT_AVAILABLE_TEXT.en}
        </span>
      </PageHeading>
    </main>
  );
}
