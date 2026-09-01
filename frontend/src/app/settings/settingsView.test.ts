import { describe, expect, it } from 'vitest';

import type { SettingItem } from '@/api/types';
import {
  WORKING_DAY_KEY,
  inputKindFor,
  isWorkingDayLength,
  toDisplayValue,
  toStoredValue,
} from '@/app/settings/settingsView';

const setting = (over: Partial<SettingItem> = {}): SettingItem => ({
  key: 'a_key',
  value: '1',
  value_type: 'integer',
  description: null,
  ...over,
});

describe('inputKindFor', () => {
  it('maps integer and decimal to a number input', () => {
    expect(inputKindFor(setting({ value_type: 'integer' }))).toBe('number');
    expect(inputKindFor(setting({ value_type: 'decimal' }))).toBe('number');
  });

  it('maps time, boolean and string to their inputs', () => {
    expect(inputKindFor(setting({ value_type: 'time' }))).toBe('time');
    expect(inputKindFor(setting({ value_type: 'boolean' }))).toBe('checkbox');
    expect(inputKindFor(setting({ value_type: 'string' }))).toBe('text');
    expect(inputKindFor(setting({ value_type: 'json' }))).toBe('text');
  });
});

describe('the working-day length, presented in hours', () => {
  it('recognises the working-day key', () => {
    expect(isWorkingDayLength(WORKING_DAY_KEY)).toBe(true);
    expect(isWorkingDayLength('document_expiry_warning_days')).toBe(false);
  });

  it('shows stored minutes as hours', () => {
    expect(toDisplayValue(setting({ key: WORKING_DAY_KEY, value: '480' }))).toBe('8');
    expect(toDisplayValue(setting({ key: WORKING_DAY_KEY, value: '450' }))).toBe('7.5');
  });

  it('converts entered hours back to whole minutes, rounding cleanly', () => {
    expect(toStoredValue(WORKING_DAY_KEY, '8')).toBe('480');
    expect(toStoredValue(WORKING_DAY_KEY, '7.5')).toBe('450');
    // A fractional hour that is not a whole number of minutes rounds to the nearest minute.
    expect(toStoredValue(WORKING_DAY_KEY, '7.51')).toBe('451');
  });

  it('passes a non-numeric entry through so the server reports it', () => {
    expect(toStoredValue(WORKING_DAY_KEY, 'abc')).toBe('abc');
    expect(toStoredValue(WORKING_DAY_KEY, '')).toBe('');
  });

  it('falls back to the raw text when the stored value does not parse', () => {
    expect(toDisplayValue(setting({ key: WORKING_DAY_KEY, value: 'oops' }))).toBe('oops');
  });
});

describe('every other setting round-trips verbatim', () => {
  it('shows and stores the value unchanged', () => {
    expect(toDisplayValue(setting({ key: 'no_checkout_cutoff_time', value: '23:59' }))).toBe('23:59');
    expect(toStoredValue('no_checkout_cutoff_time', '22:00')).toBe('22:00');
    expect(toStoredValue('document_expiry_warning_days', '30')).toBe('30');
  });
});
