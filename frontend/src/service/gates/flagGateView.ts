import type { FlagGateState } from '../api/features';

/** Which of the four views the gate renders. */
export type FlagGateViewName = 'content' | 'not-available' | 'retry' | 'loading';

/**
 * Pure decision for the gate: flag on shows the wrapped screens, off shows the not-available page,
 * unknown (could not find out) shows the retry panel, and the first answer in flight shows a
 * neutral loading state. Nothing else is ever rendered, so no sign-in screen can appear while the
 * flag is off, unknown or still loading.
 */
export function flagGateView(state: FlagGateState): FlagGateViewName {
  switch (state) {
    case 'on':
      return 'content';
    case 'off':
      return 'not-available';
    case 'unknown':
      return 'retry';
    case 'loading':
      return 'loading';
  }
}
