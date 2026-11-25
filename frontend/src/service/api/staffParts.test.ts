// Tests of the active-parts client (task 30.9; R29.4): the pure reading of the answer body.
import { describe, expect, it } from 'vitest';
import { StaffApiError } from './staff';
import { ACTIVE_PARTS_PATH, activePartsFromBody, activePartsKey } from './staffParts';

describe('activePartsFromBody', () => {
  it('reads the items in the order given', () => {
    const parts = activePartsFromBody({
      items: [
        { id: 'a', name_he: 'אטם', name_en: 'Washer', is_active: true },
        { id: 'b', name_he: 'ברז', name_en: 'Tap', is_active: true },
      ],
    });
    expect(parts.map((part) => part.id)).toEqual(['a', 'b']);
    expect(parts[0]).toEqual({ id: 'a', name_he: 'אטם', name_en: 'Washer', is_active: true });
  });

  it('drops an item marked inactive, and accepts one with no flag', () => {
    const parts = activePartsFromBody({
      items: [
        { id: 'a', name_he: 'א', name_en: 'A', is_active: false },
        { id: 'b', name_he: 'ב', name_en: 'B' },
      ],
    });
    expect(parts.map((part) => part.id)).toEqual(['b']);
  });

  it('answers an empty list for no items', () => {
    expect(activePartsFromBody({ items: [] })).toEqual([]);
  });

  it('treats an unusable body as the generic failure', () => {
    for (const body of [null, undefined, [], 'x', {}, { items: 'x' }, { items: [{ id: 'a' }] }, { items: [null] }]) {
      expect(() => activePartsFromBody(body)).toThrow(StaffApiError);
    }
  });

  it('reads the route open to every ticket staff member, under the staff query key', () => {
    expect(ACTIVE_PARTS_PATH).toBe('/parts/active');
    expect(activePartsKey[0]).toBe('service-staff');
  });
});
