import { describe, expect, it } from 'vitest';

import {
  endOfWeek,
  groupByWeek,
  rangeForPreset,
  startOfWeek,
  totalMinutes,
  type HoursDay,
} from '@/app/mobile/myHoursView';

/** A day built at local midnight, so the tests do not depend on the runner's time of day. */
const localDay = (iso: string): Date => new Date(`${iso}T00:00:00`);

const aDay = (work_date: string, total_minutes: number): HoursDay => ({ work_date, total_minutes });

describe('startOfWeek / endOfWeek — Sunday..Saturday (Israel convention)', () => {
  it('anchors a mid-week day to its surrounding Sunday and Saturday', () => {
    // 2025-03-12 is a Wednesday; its week runs Sun 2025-03-09 .. Sat 2025-03-15.
    const wednesday = localDay('2025-03-12');
    expect(startOfWeek(wednesday).getDay()).toBe(0);
    expect(endOfWeek(wednesday).getDay()).toBe(6);
    expect(startOfWeek(wednesday).getDate()).toBe(9);
    expect(endOfWeek(wednesday).getDate()).toBe(15);
  });

  it('keeps a Sunday as its own week start and a Saturday as its own week end', () => {
    const sunday = localDay('2025-03-09');
    const saturday = localDay('2025-03-15');
    expect(startOfWeek(sunday).getDate()).toBe(9);
    expect(endOfWeek(saturday).getDate()).toBe(15);
  });
});

describe('rangeForPreset — preset date math in local time', () => {
  const today = localDay('2025-03-12'); // a Wednesday in March, a 31-day month

  it('day is the single picked date, both bounds equal', () => {
    expect(rangeForPreset('day', today, localDay('2025-03-05'))).toEqual({
      from: '2025-03-05',
      to: '2025-03-05',
    });
  });

  it('day defaults to today when no explicit day is given', () => {
    expect(rangeForPreset('day', today)).toEqual({ from: '2025-03-12', to: '2025-03-12' });
  });

  it('week is the Sun..Sat window containing today', () => {
    expect(rangeForPreset('week', today)).toEqual({ from: '2025-03-09', to: '2025-03-15' });
  });

  it('month is the first to the last day of the calendar month', () => {
    expect(rangeForPreset('month', today)).toEqual({ from: '2025-03-01', to: '2025-03-31' });
  });

  it('month spans February correctly in a non-leap year', () => {
    expect(rangeForPreset('month', localDay('2025-02-14'))).toEqual({
      from: '2025-02-01',
      to: '2025-02-28',
    });
  });

  it('custom seeds the current month as its starting window', () => {
    expect(rangeForPreset('custom', today)).toEqual({ from: '2025-03-01', to: '2025-03-31' });
  });
});

describe('totalMinutes — the range total', () => {
  it('sums every day, and is zero for an empty range', () => {
    expect(totalMinutes([aDay('2025-03-10', 120), aDay('2025-03-12', 90)])).toBe(210);
    expect(totalMinutes([])).toBe(0);
  });
});

describe('groupByWeek — month view weekly subtotals', () => {
  it('groups days into Sun..Sat weeks with subtotals that reconcile to the range total', () => {
    // Two weeks: 2025-03-09..15 (Sun) holds the 10th and 12th; 2025-03-16..22 holds the 17th.
    const days = [aDay('2025-03-17', 60), aDay('2025-03-12', 90), aDay('2025-03-10', 120)];

    const groups = groupByWeek(days);

    expect(groups).toHaveLength(2);
    // Newest week first, matching the newest-first day list.
    expect(groups.map((group) => group.weekStart)).toEqual(['2025-03-16', '2025-03-09']);
    expect(groups[0].subtotalMinutes).toBe(60);
    expect(groups[1].subtotalMinutes).toBe(210);
    expect(groups[1].days.map((day) => day.work_date)).toEqual(['2025-03-12', '2025-03-10']);
    // The subtotals reconcile to the whole-range total.
    const summed = groups.reduce((acc, group) => acc + group.subtotalMinutes, 0);
    expect(summed).toBe(totalMinutes(days));
  });

  it('yields no groups for an empty list', () => {
    expect(groupByWeek([])).toEqual([]);
  });

  it('places a Saturday and the next Sunday into different weeks', () => {
    // 2025-03-15 is a Saturday, 2025-03-16 the next Sunday — adjacent days, different weeks.
    const groups = groupByWeek([aDay('2025-03-16', 30), aDay('2025-03-15', 45)]);
    expect(groups.map((group) => group.weekStart)).toEqual(['2025-03-16', '2025-03-09']);
  });
});
