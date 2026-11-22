import { describe, expect, it } from 'vitest';

import en from './locales/en/common.json';
import he from './locales/he/common.json';

// Service-tickets Requirement 2.8: the technician role has a display name in both languages, and the
// Hebrew and English common catalogues carry exactly the same keys, so no screen falls back silently.

type Tree = { [key: string]: string | Tree };

const flattenKeys = (tree: Tree, prefix = ''): string[] =>
  Object.entries(tree).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return typeof value === 'string' ? [path] : flattenKeys(value, path);
  });

const lookup = (tree: Tree, path: string): unknown =>
  path.split('.').reduce<unknown>((node, key) => {
    if (node !== null && typeof node === 'object') {
      return (node as Record<string, unknown>)[key];
    }
    return undefined;
  }, tree);

describe('common locale catalogues', () => {
  it('have identical key sets in Hebrew and English', () => {
    expect(flattenKeys(he as Tree).sort()).toEqual(flattenKeys(en as Tree).sort());
  });

  const catalogues: Record<'he' | 'en', Tree> = { he: he as Tree, en: en as Tree };

  it.each([
    ['he', 'role.technician', 'טכנאי'],
    ['en', 'role.technician', 'Technician'],
    ['he', 'nav.tickets', 'קריאות שירות'],
    ['en', 'nav.tickets', 'Service tickets'],
  ] as const)('%s has a non-empty %s', (language, key, expected) => {
    const value = lookup(catalogues[language], key);
    expect(typeof value).toBe('string');
    expect((value as string).trim()).not.toBe('');
    expect(value).toBe(expected);
  });
});
