import { describe, expect, it } from 'vitest';

import type { TimeEntryListItem, TimeEntryStatus } from '@/api/types';
import { advanceableIds, groupByEmployee, groupBySite } from '@/app/approvals/approvalView';

const anEntry = (over: Partial<TimeEntryListItem> = {}): TimeEntryListItem => ({
  id: 'e1',
  employee_id: 'emp-1',
  employee_name: 'Aaron',
  employee_name_en: 'Aaron',
  employee_number: '2000',
  site_id: 'site-a',
  site_name: 'Site A',
  work_date: '2025-08-30',
  check_in_at: '2025-08-30T07:00:00Z',
  check_out_at: '2025-08-30T11:30:00Z',
  total_minutes: 270,
  source: 'qr_scan',
  is_manual: false,
  status: 'draft',
  flags: [],
  ...over,
});

describe('groupBySite — site then employee (Requirement 15.3)', () => {
  it('groups entries under their site, sub-grouped by employee, with a tally', () => {
    const a = anEntry({ id: 'a', employee_id: 'emp-1', site_id: 'site-a', status: 'draft' });
    const b = anEntry({ id: 'b', employee_id: 'emp-2', site_id: 'site-a', status: 'review' });
    const c = anEntry({ id: 'c', employee_id: 'emp-1', site_id: 'site-b', status: 'approved' });

    const sites = groupBySite([a, b, c]);

    expect(sites.map((s) => s.siteId)).toEqual(['site-a', 'site-b']);
    const [siteA, siteB] = sites;
    expect(siteA.entryIds).toEqual(['a', 'b']);
    expect(siteA.tally.total).toBe(2);
    expect(siteA.tally.draft).toBe(1);
    expect(siteA.tally.review).toBe(1);
    expect(siteA.employees.map((e) => e.employeeId)).toEqual(['emp-1', 'emp-2']);
    expect(siteB.entryIds).toEqual(['c']);
    expect(siteB.tally.approved).toBe(1);
  });
});

describe('groupByEmployee — across sites (Requirement 15.3)', () => {
  it('groups one employee’s entries wherever they worked', () => {
    const a = anEntry({ id: 'a', employee_id: 'emp-1', site_id: 'site-a' });
    const b = anEntry({ id: 'b', employee_id: 'emp-1', site_id: 'site-b' });
    const c = anEntry({ id: 'c', employee_id: 'emp-2', site_id: 'site-a' });

    const employees = groupByEmployee([a, b, c]);

    expect(employees.map((e) => e.employeeId)).toEqual(['emp-1', 'emp-2']);
    expect(employees[0].entryIds).toEqual(['a', 'b']);
    expect(employees[1].entryIds).toEqual(['c']);
  });
});

describe('advanceableIds — a single forward step (Requirement 15.2)', () => {
  const cases: Array<[TimeEntryStatus, TimeEntryStatus, string[]]> = [
    ['draft', 'review', ['draft-1']],
    ['review', 'approved', ['review-1']],
  ];

  it.each(cases)('to %s ← from the rung below', (from, target, expected) => {
    const entries = [
      anEntry({ id: 'draft-1', status: 'draft' }),
      anEntry({ id: 'review-1', status: 'review' }),
      anEntry({ id: 'approved-1', status: 'approved' }),
    ];
    void from;
    expect(advanceableIds(entries, target)).toEqual(expected);
  });

  it('excludes entries already at or past the target', () => {
    const entries = [
      anEntry({ id: 'approved-1', status: 'approved' }),
      anEntry({ id: 'locked-1', status: 'locked' }),
    ];
    expect(advanceableIds(entries, 'approved')).toEqual([]);
  });

  it('excludes entries more than one rung below the target', () => {
    const entries = [anEntry({ id: 'draft-1', status: 'draft' })];
    // Draft cannot forward-step to approved in one move.
    expect(advanceableIds(entries, 'approved')).toEqual([]);
  });
});
