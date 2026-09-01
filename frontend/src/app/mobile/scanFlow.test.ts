import { describe, expect, it } from 'vitest';

import { ApiError } from '@/api/client';
import type { ScanResult } from '@/api/types';
import { confirming, failureToState } from '@/app/mobile/scanFlow';

const anEntry = (over: Partial<ScanResult> = {}): ScanResult => ({
  time_entry_id: 'e1',
  site_id: 's1',
  action: 'check_in',
  at: '2025-08-30T07:00:00Z',
  work_date: '2025-08-30',
  is_open: true,
  flags: [],
  ...over,
});

describe('confirming', () => {
  it('carries the scan result to show', () => {
    const result = anEntry();
    expect(confirming(result)).toEqual({ kind: 'confirming', result });
  });
});

describe('failureToState — the honesty invariant (Requirement 23.6)', () => {
  it('treats a lost request with no ApiError as offline, never a success', () => {
    // A dropped connection surfaces as a plain Error. From the employee's view nothing was recorded,
    // so it must be the offline state — the one thing this task must never render as a success.
    expect(failureToState(new Error('network down'), 'TOKEN')).toEqual({ kind: 'offline' });
    expect(failureToState(new TypeError('Failed to fetch'), 'TOKEN')).toEqual({ kind: 'offline' });
    expect(failureToState(undefined, 'TOKEN')).toEqual({ kind: 'offline' });
  });
});

describe('failureToState — the open-shift-elsewhere conflict (Requirement 11.4)', () => {
  it('maps a 409 open_shift_elsewhere to the conflict, reading site and since from the envelope', () => {
    const error = new ApiError(
      409,
      'open_shift_elsewhere',
      'conflict',
      { site_id: 's2', site_name: 'Site B', since: '2025-08-30T06:00:00Z' },
      ['transition', 'cancel'],
    );
    expect(failureToState(error, 'TOKEN-B')).toEqual({
      kind: 'conflict',
      conflict: { siteName: 'Site B', since: '2025-08-30T06:00:00Z', pendingToken: 'TOKEN-B' },
    });
  });

  it('carries the pending token so a confirmed transition can re-send it', () => {
    const error = new ApiError(409, 'open_shift_elsewhere', 'conflict', { site_name: 'Site B' });
    const state = failureToState(error, 'TOKEN-B');
    expect(state.kind).toBe('conflict');
    if (state.kind === 'conflict') {
      expect(state.conflict.pendingToken).toBe('TOKEN-B');
      // A missing `since` is tolerated: null, not the string "undefined".
      expect(state.conflict.since).toBeNull();
    }
  });
});

describe('failureToState — other refusals', () => {
  it('maps a non-conflict ApiError to an error carrying its code for translation', () => {
    const error = new ApiError(409, 'site_not_active', 'conflict');
    expect(failureToState(error, 'TOKEN')).toEqual({ kind: 'error', code: 'site_not_active' });
  });

  it('keeps a null code rather than inventing one', () => {
    const error = new ApiError(500, null, 'server error');
    expect(failureToState(error, 'TOKEN')).toEqual({ kind: 'error', code: null });
  });
});
