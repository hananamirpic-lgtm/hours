/**
 * A stand-in for the management interface at `/tickets/*`, until task 24.1 adds its route tree.
 *
 * Without a route at the Management_Path, the technician redirect would go to `/`, which is the
 * console tree, whose gate redirects again: a loop. This shows only a heading, no Hours data and no
 * ticket data, and it sits behind the flag gate like the real interface will. Task 24.1 replaces it.
 */

import { Bilingual } from './FlagGate';
import { TICKETS_HEADING_TEXT } from './flagGateStrings';
import { pageStyle } from './NotAvailable';

export function TicketsRoutePlaceholder() {
  return (
    <main style={pageStyle}>
      <h1 style={{ fontSize: '1.25rem', fontWeight: 600 }}>
        <Bilingual text={TICKETS_HEADING_TEXT} />
      </h1>
    </main>
  );
}
