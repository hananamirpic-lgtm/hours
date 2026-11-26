/**
 * The markup of the technician notice (Requirement 21.7): the unavailable message, in Hebrew and
 * English together, and one sign-out button. No link, no form, no other control.
 *
 * Kept apart from `TicketsUnavailable` so it imports no auth provider (which loads the staff i18n
 * instance) and renders in a plain test.
 */

import { Bilingual } from './FlagGate';
import { NOT_AVAILABLE_TEXT, SIGN_OUT_TEXT } from './flagGateStrings';
import { pageStyle } from './NotAvailable';

export function TicketsUnavailableView({ onSignOut }: { onSignOut: () => void }) {
  return (
    <main style={pageStyle}>
      <section>
        <p>
          <Bilingual text={NOT_AVAILABLE_TEXT} />
        </p>
        <button type="button" onClick={onSignOut}>
          <Bilingual text={SIGN_OUT_TEXT} />
        </button>
      </section>
    </main>
  );
}
