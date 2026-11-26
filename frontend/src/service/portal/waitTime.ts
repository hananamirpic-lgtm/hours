// Wait-time message for a rate-limit response (R5.10, R22.8; design Property 33).
// Pure: no React, no DOM, no i18n. The caller turns the result into text in the current language.

export type WaitTime =
  | { readonly unit: 'seconds'; readonly value: number }
  | { readonly unit: 'minutes'; readonly value: number }
  | { readonly unit: 'none'; readonly value: 0 };

const NONE: WaitTime = { unit: 'none', value: 0 };

// Only a plain run of ASCII digits is a valid `Retry-After` value here. The HTTP-date form, decimals,
// signs, exponents and hex are all "not a positive integer" under R22.8 and give no wait time. Spaces
// and tabs around the digits are tolerated, because header values reach us unnormalised.
const DIGITS = /^[ \t\r\n]*([0-9]+)[ \t\r\n]*$/;

/**
 * @param retryAfter the raw `Retry-After` header value (or a number already parsed by the caller).
 *
 * - 1 to 60 seconds: `{unit: 'seconds', value}`.
 * - More than 60 seconds: whole minutes, rounded up (61 -> 2, 120 -> 2, 121 -> 3).
 * - Absent, empty, non-numeric, zero, negative, fractional, or not a safe integer: `{unit: 'none', value: 0}`.
 *
 * There is no upper cap. The backend never sends more than the longest window (one hour), but any value
 * up to Number.MAX_SAFE_INTEGER seconds is converted exactly. A longer digit run (for example
 * '9999999999999999999999') cannot be held exactly in a number, so it gives `none` rather than a wrong
 * figure.
 */
export function waitTimeMessage(retryAfter: string | number | null | undefined): WaitTime {
  let seconds: number;
  if (typeof retryAfter === 'number') {
    seconds = retryAfter;
  } else if (typeof retryAfter === 'string') {
    const match = DIGITS.exec(retryAfter);
    if (match === null) return NONE;
    seconds = Number(match[1]);
  } else {
    return NONE;
  }
  if (!Number.isSafeInteger(seconds) || seconds < 1) return NONE;
  if (seconds <= 60) return { unit: 'seconds', value: seconds };
  return { unit: 'minutes', value: Math.ceil(seconds / 60) };
}
