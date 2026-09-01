import { describe, expect, it } from 'vitest';

import { ApiError } from '@/api/client';
import { hasAction, toError } from '@/lib/apiError';

describe('toError', () => {
  it('maps an ApiError code to its translation key and carries params', () => {
    const error = new ApiError(409, 'duplicate_passport', 'conflict', { full_name: 'Dana Levi' });
    expect(toError(error)).toEqual({
      key: 'apiError.duplicate_passport',
      params: { full_name: 'Dana Levi' },
    });
  });

  it('falls back to the unknown key for a non-API error', () => {
    expect(toError(new Error('boom'))).toEqual({ key: 'apiError.unknown', params: {} });
  });

  it('falls back to unknown when an ApiError carries no code', () => {
    expect(toError(new ApiError(500, null, 'server error'))).toEqual({
      key: 'apiError.unknown',
      params: {},
    });
  });
});

describe('hasAction', () => {
  it('is true when the envelope offered the named recorded action', () => {
    const error = new ApiError(409, 'client_has_time_entries', 'conflict', {}, ['archive']);
    expect(hasAction(error, 'archive')).toBe(true);
  });

  it('is false for a different action or a plain error', () => {
    const error = new ApiError(409, 'client_has_time_entries', 'conflict', {}, ['archive']);
    expect(hasAction(error, 'delete')).toBe(false);
    expect(hasAction(new Error('boom'), 'archive')).toBe(false);
  });
});
