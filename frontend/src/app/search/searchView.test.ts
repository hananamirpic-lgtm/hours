import { describe, expect, it } from 'vitest';

import type { SearchResults } from '@/api/types';
import {
  employeeLabel,
  flattenResults,
  hitPath,
  moveActive,
} from '@/app/search/searchView';

/** A results payload with the three groups; each group defaults to empty. */
const results = (over: Partial<SearchResults> = {}): SearchResults => ({
  query: 'q',
  employees: { items: [], total: 0, limit: 10, offset: 0 },
  sites: { items: [], total: 0, limit: 10, offset: 0 },
  clients: { items: [], total: 0, limit: 10, offset: 0 },
  ...over,
});

describe('employeeLabel — reader-language name with the other form (Requirement 21.5)', () => {
  it('shows the Hebrew form first in Hebrew, English in parentheses', () => {
    expect(employeeLabel('he', 'אברהם כהן', 'Abraham Cohen')).toBe('אברהם כהן (Abraham Cohen)');
  });

  it('shows the English form first in English, Hebrew in parentheses', () => {
    expect(employeeLabel('en', 'אברהם כהן', 'Abraham Cohen')).toBe('Abraham Cohen (אברהם כהן)');
  });

  it('does not double a name that is the same in both forms', () => {
    expect(employeeLabel('en', 'Sarah', 'Sarah')).toBe('Sarah');
    expect(employeeLabel('he', 'Sarah', 'Sarah')).toBe('Sarah');
  });
});

describe('hitPath — a chosen hit opens its console list carrying the id', () => {
  it('routes each kind to its list route with the id in the query', () => {
    expect(hitPath('employee', 'e1')).toBe('/employees?id=e1');
    expect(hitPath('site', 's1')).toBe('/sites?id=s1');
    expect(hitPath('client', 'c1')).toBe('/clients?id=c1');
  });

  it('encodes an id so a stray character cannot break the query', () => {
    expect(hitPath('employee', 'a b')).toBe('/employees?id=a%20b');
  });
});

describe('flattenResults — grouped results into one navigable list (Requirement 22.1, 22.5)', () => {
  it('concatenates employees, then sites, then clients, indexing across the whole list', () => {
    const flat = flattenResults(
      results({
        employees: {
          items: [
            { id: 'e1', full_name: 'אברהם', full_name_en: 'Abraham', status: 'active' },
            { id: 'e2', full_name: 'דוד', full_name_en: 'David', status: 'active' },
          ],
          total: 2,
          limit: 10,
          offset: 0,
        },
        sites: {
          items: [{ id: 's1', name: 'North Tower', site_number: 'SITE-042', client_id: 'c9', status: 'active' }],
          total: 1,
          limit: 10,
          offset: 0,
        },
        clients: {
          items: [{ id: 'c1', name: 'Globex', company: 'Globex Corp' }],
          total: 1,
          limit: 10,
          offset: 0,
        },
      }),
      'en',
    );

    expect(flat.count).toBe(4);
    expect(flat.groups.map((group) => group.kind)).toEqual(['employee', 'site', 'client']);
    // The flattened order matches the group order, and indices are the position in that flat list.
    expect(flat.items.map((item) => item.index)).toEqual([0, 1, 2, 3]);
    expect(flat.items.map((item) => item.kind)).toEqual(['employee', 'employee', 'site', 'client']);
    expect(flat.items.map((item) => item.id)).toEqual(['e1', 'e2', 's1', 'c1']);
    // Keys are stable and unique across kinds.
    expect(new Set(flat.items.map((item) => item.key)).size).toBe(4);
    // A site carries its number as the detail line; a client its company.
    expect(flat.items[2].detail).toBe('SITE-042');
    expect(flat.items[3].detail).toBe('Globex Corp');
    // Each item's path opens its list route with the id.
    expect(flat.items[0].to).toBe('/employees?id=e1');
    expect(flat.items[2].to).toBe('/sites?id=s1');
  });

  it('carries a group total that can exceed the items shown (Requirement 22.5)', () => {
    const flat = flattenResults(
      results({
        employees: {
          items: [{ id: 'e1', full_name: 'A', full_name_en: 'A', status: 'active' }],
          total: 12,
          limit: 1,
          offset: 0,
        },
      }),
      'en',
    );
    expect(flat.groups[0].total).toBe(12);
    expect(flat.groups[0].items).toHaveLength(1);
  });

  it('omits a group with no items and flattens an empty result to nothing', () => {
    const onlyClients = flattenResults(
      results({
        clients: {
          items: [{ id: 'c1', name: 'Solo', company: null }],
          total: 1,
          limit: 10,
          offset: 0,
        },
      }),
      'en',
    );
    expect(onlyClients.groups.map((group) => group.kind)).toEqual(['client']);

    expect(flattenResults(results(), 'en')).toEqual({ groups: [], items: [], count: 0 });
    expect(flattenResults(null, 'en')).toEqual({ groups: [], items: [], count: 0 });
    expect(flattenResults(undefined, 'en')).toEqual({ groups: [], items: [], count: 0 });
  });

  it('labels an employee in the reader language (Requirement 21.5)', () => {
    const payload = results({
      employees: {
        items: [{ id: 'e1', full_name: 'אברהם', full_name_en: 'Abraham', status: 'active' }],
        total: 1,
        limit: 10,
        offset: 0,
      },
    });
    expect(flattenResults(payload, 'he').items[0].label).toBe('אברהם (Abraham)');
    expect(flattenResults(payload, 'en').items[0].label).toBe('Abraham (אברהם)');
  });
});

describe('moveActive — arrow-key navigation across the flattened list (Requirement 22.5)', () => {
  it('selects nothing over an empty list', () => {
    expect(moveActive(null, 1, 0)).toBeNull();
    expect(moveActive(2, -1, 0)).toBeNull();
  });

  it('selects the first item on the first Down and the last on the first Up', () => {
    expect(moveActive(null, 1, 4)).toBe(0);
    expect(moveActive(null, -1, 4)).toBe(3);
  });

  it('steps one item at a time', () => {
    expect(moveActive(0, 1, 4)).toBe(1);
    expect(moveActive(2, -1, 4)).toBe(1);
  });

  it('wraps Down past the end to the top and Up past the start to the bottom', () => {
    expect(moveActive(3, 1, 4)).toBe(0);
    expect(moveActive(0, -1, 4)).toBe(3);
  });
});
