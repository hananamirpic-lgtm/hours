import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import type { UserRole } from '@/api/types';
import { CONSOLE_NAV, navFor } from '@/app/navigation';
import type { FlagGateState } from '@/service/api/features';
import { NOT_AVAILABLE_TEXT, SIGN_OUT_TEXT } from '@/service/gates/flagGateStrings';
import { consoleNavItems, technicianRouting, TICKETS_NAV_ITEM } from '@/service/gates/technicianRouting';
import { TicketsUnavailableView } from '@/service/gates/TicketsUnavailableView';

const ROLES: UserRole[] = [
  'admin',
  'operations_admin',
  'accounting',
  'site_manager',
  'employee',
  'technician',
];
const FLAGS: FlagGateState[] = ['on', 'off', 'unknown', 'loading'];

describe('technicianRouting', () => {
  it.each([
    ['on', 'redirect'],
    ['off', 'notice'],
    ['unknown', 'notice'],
    ['loading', 'loading'],
  ] as const)('technician with the flag %s -> %s', (flag, expected) => {
    expect(technicianRouting('technician', flag)).toBe(expected);
  });

  it.each(ROLES.filter((role) => role !== 'technician'))('%s passes through for every flag state', (role) => {
    for (const flag of FLAGS) {
      expect(technicianRouting(role, flag)).toBe('pass');
    }
  });

  it('a missing role passes through', () => {
    expect(technicianRouting(undefined, 'loading')).toBe('pass');
  });
});

describe('consoleNavItems', () => {
  it('uses the one tickets entry, a path and a label key', () => {
    expect(TICKETS_NAV_ITEM).toEqual({ to: '/tickets', labelKey: 'nav.tickets' });
  });

  it.each(['admin', 'operations_admin'] as const)('%s gets exactly one extra entry at the end only when on', (role) => {
    const base = navFor(role);
    const on = consoleNavItems(role, 'on');
    expect(on).toHaveLength(base.length + 1);
    expect(on.slice(0, -1)).toEqual(base);
    expect(on.at(-1)).toEqual({ to: '/tickets', labelKey: 'nav.tickets' });
    expect(on.filter((item) => item.to === '/tickets')).toHaveLength(1);
  });

  it.each(['admin', 'operations_admin'] as const)('%s: off, unknown and loading give the unchanged entries', (role) => {
    for (const flag of ['off', 'unknown', 'loading'] as const) {
      expect(consoleNavItems(role, flag)).toEqual(navFor(role));
    }
  });

  it.each(['accounting', 'site_manager', 'employee', 'technician'] as const)(
    '%s never gets the tickets entry and keeps navFor unchanged',
    (role) => {
      for (const flag of FLAGS) {
        const items = consoleNavItems(role, flag);
        expect(items).toEqual(navFor(role));
        expect(items.some((item) => item.to === '/tickets')).toBe(false);
      }
    },
  );

  it('a technician has no navigation entry at all, whatever the flag', () => {
    for (const flag of FLAGS) {
      expect(consoleNavItems('technician', flag)).toEqual([]);
    }
  });

  it('does not change the console list or what navFor returns', () => {
    const before = CONSOLE_NAV.map((item) => item.to);
    for (const role of ROLES) {
      for (const flag of FLAGS) {
        consoleNavItems(role, flag);
      }
    }
    expect(CONSOLE_NAV.map((item) => item.to)).toEqual(before);
    expect(CONSOLE_NAV.some((item) => item.to === '/tickets')).toBe(false);
    expect(navFor('admin')).toBe(CONSOLE_NAV);
  });
});

describe('TicketsUnavailableView', () => {
  const html = renderToStaticMarkup(createElement(TicketsUnavailableView, { onSignOut: () => undefined }));

  it('shows the message in Hebrew and English', () => {
    expect(html).toContain(NOT_AVAILABLE_TEXT.he);
    expect(html).toContain(NOT_AVAILABLE_TEXT.en);
  });

  it('has exactly one button, the sign-out control', () => {
    expect(html.match(/<button[\s>]/g)).toHaveLength(1);
    const button = html.slice(html.indexOf('<button'), html.indexOf('</button>'));
    expect(button).toContain(SIGN_OUT_TEXT.he);
    expect(button).toContain(SIGN_OUT_TEXT.en);
  });

  it('has no link, input, form, select or textarea', () => {
    expect(html).not.toMatch(/<(a|input|form|select|textarea)[\s>]/);
    expect(html).not.toContain('href');
  });
});
