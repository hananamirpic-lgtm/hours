/**
 * The pure state of the employee scan flow, separated from the screen so its transitions can be
 * unit-tested without a camera or a network (Requirement 23.5, 23.6, 11.4).
 *
 * The flow is a small state machine. It starts `idle`, opens the `scanning` camera view, and on a
 * decoded token goes `submitting`. A success lands on `confirming` with the outcome to show
 * (Requirement 23.5). Two failures are distinct and must never be confused with each other or with
 * success:
 *
 *  - `offline`: the scan was refused before it left the device, or a network fault dropped it. The
 *    screen states plainly that nothing was recorded (Requirement 23.6). This is the invariant the
 *    task guards: an offline scan is a visible non-event, never a false success.
 *  - `conflict`: the server said there is an open shift at another site (409 open_shift_elsewhere).
 *    The screen offers the transition or cancel, and the "move to another site" action
 *    (Requirement 11.4, 11.6). The other site's name and the pending token are carried so the
 *    confirmed transition can re-present the token.
 *  - `error`: any other refusal, shown by its translated code.
 */

import type { ScanResult } from '@/api/types';
import { ApiError } from '@/api/client';

/** The site an open-shift-elsewhere conflict names, and the token that provoked it. */
export interface ConflictState {
  /** The other site's name, straight from the conflict envelope's `site_name` param. */
  siteName: string;
  /** When the other shift opened, ISO 8601, from the envelope's `since` param — may be absent. */
  since: string | null;
  /** The token the employee presented, so a confirmed transition can re-send it. */
  pendingToken: string;
}

export type ScanFlowState =
  | { kind: 'idle' }
  | { kind: 'scanning' }
  | { kind: 'submitting' }
  | { kind: 'confirming'; result: ScanResult }
  | { kind: 'conflict'; conflict: ConflictState }
  | { kind: 'offline' }
  | { kind: 'error'; code: string | null };

export const idle: ScanFlowState = { kind: 'idle' };
export const scanning: ScanFlowState = { kind: 'scanning' };
export const submitting: ScanFlowState = { kind: 'submitting' };
export const offline: ScanFlowState = { kind: 'offline' };

export const confirming = (result: ScanResult): ScanFlowState => ({ kind: 'confirming', result });

/**
 * Classify a scan failure into the state that names it honestly.
 *
 * A 409 `open_shift_elsewhere` becomes the conflict, reading the other site's name and the pending
 * token from the envelope. Anything else is a plain error carrying its code for translation. A caller
 * that already knows the device was offline should not reach here — it routes to `offline` directly —
 * but a request that failed with no `ApiError` (a dropped connection) is treated as offline rather
 * than a mysterious error, because from the employee's point of view the scan simply did not land.
 */
export const failureToState = (error: unknown, pendingToken: string): ScanFlowState => {
  if (error instanceof ApiError) {
    if (error.code === 'open_shift_elsewhere') {
      return {
        kind: 'conflict',
        conflict: {
          siteName: error.params.site_name ?? '',
          since: error.params.since ?? null,
          pendingToken,
        },
      };
    }
    return { kind: 'error', code: error.code };
  }
  // No structured error: the request never reached the server or its reply was lost. Treat it as a
  // non-recording, not a success — the whole point of Requirement 23.6.
  return { kind: 'offline' };
};
