// Focus handling of the two credential forms (R23.13, R23.14).
//
// After a failed attempt:
//  - a failure with a failing field moves focus to that field (the first one in reading order);
//  - any other failure moves focus nowhere. The message is announced through the alert live region.
//    The one exception: the submit button is disabled while the request is pending, and a browser may
//    drop focus from a button that becomes disabled, so when the button had focus at submit time it gets
//    focus back once the request has ended. That keeps a keyboard user where they were.
//
// The effect runs after the render in which the failure arrived, which is also the render in which the
// button is enabled again (the mutation sets its error and leaves "pending" in the same update).
import { useEffect, useRef } from 'react';
import type { AuthFeedback } from './authFeedback';

export function useAuthFocus(
  fieldIds: Readonly<Record<string, string>>,
  submitId: string,
  /** The failure as the API returned it (before the customer edits anything), or null. */
  failure: AuthFeedback | null,
): { noteSubmit: () => void } {
  const submitHadFocus = useRef(false);

  useEffect(() => {
    if (failure === null) return;
    const fieldId = failure.focus === null ? undefined : fieldIds[failure.focus];
    const target = fieldId ?? (submitHadFocus.current ? submitId : undefined);
    submitHadFocus.current = false;
    if (target !== undefined) document.getElementById(target)?.focus();
  }, [failure, fieldIds, submitId]);

  return {
    noteSubmit: () => {
      submitHadFocus.current = document.activeElement?.id === submitId;
    },
  };
}
