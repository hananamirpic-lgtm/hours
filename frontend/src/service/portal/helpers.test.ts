import { describe, expect, it } from 'vitest';
import { ERROR_CODES } from '../api/errorCodes';
import type { TicketStatusEntry } from '../types';
import { diffStatuses } from './announcements';
import { errorTextKey, fieldsToMessages, firstFailingField } from './formErrors';
import { waitTimeMessage } from './waitTime';

describe('waitTimeMessage', () => {
  const none = { unit: 'none', value: 0 };

  it('states whole seconds from 1 to 60', () => {
    expect(waitTimeMessage('1')).toEqual({ unit: 'seconds', value: 1 });
    expect(waitTimeMessage('45')).toEqual({ unit: 'seconds', value: 45 });
    expect(waitTimeMessage('60')).toEqual({ unit: 'seconds', value: 60 });
    expect(waitTimeMessage(60)).toEqual({ unit: 'seconds', value: 60 });
  });

  it('states whole minutes rounded up above 60', () => {
    expect(waitTimeMessage('61')).toEqual({ unit: 'minutes', value: 2 });
    expect(waitTimeMessage('120')).toEqual({ unit: 'minutes', value: 2 });
    expect(waitTimeMessage('121')).toEqual({ unit: 'minutes', value: 3 });
    expect(waitTimeMessage(900)).toEqual({ unit: 'minutes', value: 15 });
    expect(waitTimeMessage('3600')).toEqual({ unit: 'minutes', value: 60 });
  });

  it('trims surrounding whitespace', () => {
    expect(waitTimeMessage('  45 ')).toEqual({ unit: 'seconds', value: 45 });
    expect(waitTimeMessage('\t121\r\n')).toEqual({ unit: 'minutes', value: 3 });
  });

  it('gives no time for zero and negative values', () => {
    expect(waitTimeMessage(0)).toEqual(none);
    expect(waitTimeMessage('0')).toEqual(none);
    expect(waitTimeMessage('000')).toEqual(none);
    expect(waitTimeMessage('-3')).toEqual(none);
    expect(waitTimeMessage(-3)).toEqual(none);
  });

  it('gives no time for absent, empty and non-numeric values', () => {
    expect(waitTimeMessage(null)).toEqual(none);
    expect(waitTimeMessage(undefined)).toEqual(none);
    expect(waitTimeMessage('')).toEqual(none);
    expect(waitTimeMessage('   ')).toEqual(none);
    expect(waitTimeMessage('abc')).toEqual(none);
    expect(waitTimeMessage('12abc')).toEqual(none);
    expect(waitTimeMessage('1 2')).toEqual(none);
  });

  it('rejects decimals, signs, exponents, hex and HTTP dates', () => {
    for (const raw of ['1.5', '+5', '1e3', '0x10', '٣', 'Wed, 21 Oct 2026 07:28:00 GMT']) {
      expect(waitTimeMessage(raw)).toEqual(none);
    }
    expect(waitTimeMessage(1.5)).toEqual(none);
    expect(waitTimeMessage(Number.NaN)).toEqual(none);
    expect(waitTimeMessage(Number.POSITIVE_INFINITY)).toEqual(none);
  });

  it('does not overflow on absurdly large values', () => {
    expect(waitTimeMessage('9999999999999999999999')).toEqual(none);
    expect(waitTimeMessage(1e22)).toEqual(none);
    expect(waitTimeMessage(String(Number.MAX_SAFE_INTEGER))).toEqual({
      unit: 'minutes',
      value: Math.ceil(Number.MAX_SAFE_INTEGER / 60),
    });
    expect(waitTimeMessage(String(Number.MAX_SAFE_INTEGER + 2))).toEqual(none);
  });
});

describe('diffStatuses', () => {
  const t = (ticketNumber: number, status: TicketStatusEntry['status']): TicketStatusEntry => ({
    ticketNumber,
    status,
  });

  it('announces nothing on the first load', () => {
    expect(diffStatuses(null, [t(1, 'received'), t(2, 'closed')])).toEqual([]);
    expect(diffStatuses(null, [])).toEqual([]);
  });

  it('announces nothing when nothing changed', () => {
    const list = [t(1, 'received'), t(2, 'in_progress')];
    expect(diffStatuses(list, [...list])).toEqual([]);
    expect(diffStatuses([], [])).toEqual([]);
  });

  it('announces one changed ticket with its new status', () => {
    expect(diffStatuses([t(1, 'received'), t(2, 'received')], [t(1, 'received'), t(2, 'in_progress')])).toEqual([
      { ticketNumber: 2, status: 'in_progress' },
    ]);
  });

  it('orders several changes by ticket number ascending', () => {
    const previous = [t(30, 'received'), t(10, 'received'), t(20, 'received'), t(40, 'received')];
    const next = [t(40, 'received'), t(30, 'closed'), t(20, 'in_progress'), t(10, 'cancelled')];
    expect(diffStatuses(previous, next)).toEqual([
      { ticketNumber: 10, status: 'cancelled' },
      { ticketNumber: 20, status: 'in_progress' },
      { ticketNumber: 30, status: 'closed' },
    ]);
  });

  it('announces a status that goes back and forth on each change, from the previous load only', () => {
    const a = [t(5, 'received')];
    const b = [t(5, 'in_progress')];
    expect(diffStatuses(a, b)).toEqual([{ ticketNumber: 5, status: 'in_progress' }]);
    expect(diffStatuses(b, a)).toEqual([{ ticketNumber: 5, status: 'received' }]);
    expect(diffStatuses(b, b)).toEqual([]);
  });

  it('ignores tickets that are new in the next load and tickets that were removed', () => {
    const previous = [t(1, 'received'), t(2, 'received')];
    const next = [t(2, 'closed'), t(3, 'received')];
    expect(diffStatuses(previous, next)).toEqual([{ ticketNumber: 2, status: 'closed' }]);
    expect(diffStatuses(previous, [t(3, 'received')])).toEqual([]);
    expect(diffStatuses([], [t(3, 'closed')])).toEqual([]);
  });

  it('does not change its inputs', () => {
    const previous = Object.freeze([Object.freeze(t(1, 'received'))]);
    const next = Object.freeze([Object.freeze(t(1, 'closed'))]);
    expect(diffStatuses(previous, next)).toHaveLength(1);
  });
});

describe('formErrors', () => {
  const domOrder = ['category', 'description', 'photo', 'urgent'];

  describe('firstFailingField', () => {
    it('returns the first field in DOM order, whatever the order of the map', () => {
      expect(firstFailingField({ urgent: 'invalid_urgent', description: 'invalid_description' }, domOrder)).toBe(
        'description',
      );
      expect(firstFailingField({ photo: 'invalid_photo_type', category: 'invalid_category' }, domOrder)).toBe(
        'category',
      );
    });

    it('ignores fields that are not in the DOM order', () => {
      expect(firstFailingField({ other: 'unknown_field' }, domOrder)).toBeNull();
      expect(firstFailingField({ other: 'unknown_field', photo: 'photo_too_large' }, domOrder)).toBe('photo');
    });

    it('returns null for empty, null and undefined fields or an empty DOM order', () => {
      expect(firstFailingField({}, domOrder)).toBeNull();
      expect(firstFailingField(null, domOrder)).toBeNull();
      expect(firstFailingField(undefined, domOrder)).toBeNull();
      expect(firstFailingField({ category: 'invalid_category' }, [])).toBeNull();
    });

    it('does not treat inherited object keys as failing fields', () => {
      expect(firstFailingField({}, ['toString', 'constructor'])).toBeNull();
    });
  });

  describe('errorTextKey', () => {
    it('maps every known code to errors.<code>', () => {
      for (const code of ERROR_CODES) expect(errorTextKey(code)).toBe(`errors.${code}`);
    });

    it('falls back to errors.generic for unknown, empty and absent codes', () => {
      expect(errorTextKey('something_new')).toBe('errors.generic');
      expect(errorTextKey('')).toBe('errors.generic');
      expect(errorTextKey('INVALID_PHONE')).toBe('errors.generic');
      expect(errorTextKey('constructor')).toBe('errors.generic');
      expect(errorTextKey(null)).toBe('errors.generic');
      expect(errorTextKey(undefined)).toBe('errors.generic');
    });
  });

  describe('fieldsToMessages', () => {
    it('follows DOM order, with unknown fields last in server order', () => {
      const fields = {
        zeta: 'unknown_field',
        urgent: 'invalid_urgent',
        alpha: 'brand_new_code',
        category: 'invalid_category',
      };
      expect(fieldsToMessages(fields, domOrder)).toEqual([
        { field: 'category', textKey: 'errors.invalid_category' },
        { field: 'urgent', textKey: 'errors.invalid_urgent' },
        { field: 'zeta', textKey: 'errors.unknown_field' },
        { field: 'alpha', textKey: 'errors.generic' },
      ]);
    });

    it('lists a field once even when the DOM order repeats it', () => {
      expect(fieldsToMessages({ photo: 'photo_too_large' }, ['photo', 'photo'])).toEqual([
        { field: 'photo', textKey: 'errors.photo_too_large' },
      ]);
    });

    it('returns an empty list for empty, null and undefined fields', () => {
      expect(fieldsToMessages({}, domOrder)).toEqual([]);
      expect(fieldsToMessages(null, domOrder)).toEqual([]);
      expect(fieldsToMessages(undefined, domOrder)).toEqual([]);
    });

    it('first message matches the field that takes focus', () => {
      const fields = { description: 'invalid_description', category: 'invalid_category' };
      expect(fieldsToMessages(fields, domOrder)[0]?.field).toBe(firstFailingField(fields, domOrder));
    });
  });
});
