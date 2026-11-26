import { describe, expect, it } from 'vitest';

import en from './locales/en/service.json';
import he from './locales/he/service.json';

// Service-tickets task 22.1: the `service` namespace has exactly the same keys in Hebrew and English, no
// empty text, and the same {{placeholders}} for each key, so no screen falls back silently or loses a value.

type Tree = { [key: string]: string | Tree };

const flatten = (tree: Tree, prefix = ''): Record<string, string> =>
  Object.entries(tree).reduce<Record<string, string>>((flat, [key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === 'string') {
      flat[path] = value;
    } else {
      Object.assign(flat, flatten(value, path));
    }
    return flat;
  }, {});

const placeholders = (text: string): string[] =>
  Array.from(new Set(Array.from(text.matchAll(/\{\{\s*([^}\s]+)\s*\}\}/g), (match) => match[1] as string))).sort();

const flatHe = flatten(he as Tree);
const flatEn = flatten(en as Tree);

describe('service locale catalogues', () => {
  it('have identical key sets in Hebrew and English', () => {
    expect(Object.keys(flatHe).sort()).toEqual(Object.keys(flatEn).sort());
  });

  it.each([
    ['he', flatHe],
    ['en', flatEn],
  ] as const)('%s has no empty text', (_language, flat) => {
    const empty = Object.entries(flat)
      .filter(([, value]) => value.trim() === '')
      .map(([key]) => key);
    expect(empty).toEqual([]);
  });

  it('use the same {{placeholders}} for every key in both languages', () => {
    const mismatched = Object.keys(flatHe)
      .filter((key) => key in flatEn)
      .filter((key) => placeholders(flatHe[key] as string).join(',') !== placeholders(flatEn[key] as string).join(','));
    expect(mismatched).toEqual([]);
  });

  it('has the four ticket statuses and the generic error in both languages', () => {
    for (const flat of [flatHe, flatEn]) {
      for (const key of [
        'status.received',
        'status.in_progress',
        'status.closed',
        'status.cancelled',
        'errors.generic',
      ]) {
        expect(flat[key]?.trim()).toBeTruthy();
      }
    }
  });
});
