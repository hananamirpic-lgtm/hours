import { describe, expect, it } from 'vitest';

import type { TimeEntryListItem } from '@/api/types';
import { groupEntries } from '@/app/hours/hoursView';

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

describe('groupEntries — per-employee day across sites (Requirement 18.1)', () => {
  it('folds the brief scenario into one day with per-site subtotals and a daily total', () => {
    // 07:00–11:30 at Site A (270 min) plus 12:00–17:00 at Site B (300 min): 570 min = 9:30 total,
    // split across two sites (Requirement 11.2 — the day total is the sum of the per-site entries).
    const siteA = anEntry({ id: 'a', site_id: 'site-a', site_name: 'Site A', total_minutes: 270 });
    const siteB = anEntry({
      id: 'b',
      site_id: 'site-b',
      site_name: 'Site B',
      check_in_at: '2025-08-30T12:00:00Z',
      check_out_at: '2025-08-30T17:00:00Z',
      total_minutes: 300,
    });

    const days = groupEntries([siteA, siteB]);

    expect(days).toHaveLength(1);
    const [day] = days;
    expect(day.employeeId).toBe('emp-1');
    expect(day.workDate).toBe('2025-08-30');
    expect(day.siteCount).toBe(2);
    expect(day.totalMinutes).toBe(570);
    expect(day.siteGroups.map((group) => group.siteId)).toEqual(['site-a', 'site-b']);
    expect(day.siteGroups[0].subtotalMinutes).toBe(270);
    expect(day.siteGroups[1].subtotalMinutes).toBe(300);
    // The per-site subtotals reconcile to the daily total.
    const summed = day.siteGroups.reduce((acc, group) => acc + group.subtotalMinutes, 0);
    expect(summed).toBe(day.totalMinutes);
  });

  it('separates two employees and two days into their own groups, preserving the input order', () => {
    const entries = [
      anEntry({ id: 'a1', employee_id: 'emp-1', work_date: '2025-08-30' }),
      anEntry({ id: 'a2', employee_id: 'emp-1', work_date: '2025-08-31' }),
      anEntry({ id: 'b1', employee_id: 'emp-2', employee_name: 'Bella', work_date: '2025-08-30' }),
    ];

    const days = groupEntries(entries);

    expect(days.map((day) => day.key)).toEqual([
      'emp-1::2025-08-30',
      'emp-1::2025-08-31',
      'emp-2::2025-08-30',
    ]);
  });

  it('keeps several entries at the same site in one group and sums them', () => {
    const morning = anEntry({ id: 'm', total_minutes: 120 });
    const afternoon = anEntry({
      id: 'a',
      check_in_at: '2025-08-30T13:00:00Z',
      check_out_at: '2025-08-30T15:00:00Z',
      total_minutes: 120,
    });

    const [day] = groupEntries([morning, afternoon]);
    expect(day.siteGroups).toHaveLength(1);
    expect(day.siteGroups[0].entries).toHaveLength(2);
    expect(day.siteGroups[0].subtotalMinutes).toBe(240);
    expect(day.totalMinutes).toBe(240);
  });
});

describe('groupEntries — open shifts (Requirement 11.2)', () => {
  it('adds no minutes for an open shift but marks the day as having one', () => {
    const open = anEntry({ id: 'open', check_out_at: null, total_minutes: null });
    const [day] = groupEntries([open]);
    expect(day.totalMinutes).toBe(0);
    expect(day.hasOpenShift).toBe(true);
    expect(day.siteGroups[0].subtotalMinutes).toBe(0);
  });

  it('totals only the completed work when a day mixes an open and a closed shift', () => {
    const closed = anEntry({ id: 'closed', total_minutes: 180 });
    const open = anEntry({
      id: 'open',
      site_id: 'site-b',
      site_name: 'Site B',
      check_out_at: null,
      total_minutes: null,
    });
    const [day] = groupEntries([closed, open]);
    expect(day.totalMinutes).toBe(180);
    expect(day.hasOpenShift).toBe(true);
  });
});

describe('groupEntries — markers (Requirement 12.4, 7.3, 10.5)', () => {
  it('flags a day as manual when any entry is manual', () => {
    const [day] = groupEntries([anEntry({ is_manual: true, source: 'manual' })]);
    expect(day.hasManual).toBe(true);
    expect(day.hasAnomaly).toBe(false);
  });

  it('flags a day as anomalous when any entry carries a flag', () => {
    const [day] = groupEntries([anEntry({ flags: ['implausible_duration'] })]);
    expect(day.hasAnomaly).toBe(true);
  });
});

describe('groupEntries — empty', () => {
  it('returns no days for an empty page', () => {
    expect(groupEntries([])).toEqual([]);
  });
});
