import { describe, expect, it } from 'vitest';

import type { PayrollRecord, SiteAllocation } from '@/api/types';
import {
  allocationMinutes,
  allocationsTotal,
  parseAmount,
  payBuckets,
  totalMinutes,
} from '@/app/payroll/payrollView';

const anAllocation = (over: Partial<SiteAllocation> = {}): SiteAllocation => ({
  site_id: 'site-a',
  regular_minutes: 0,
  overtime_minutes: 0,
  shabbat_minutes: 0,
  holiday_minutes: 0,
  cost: '0.00',
  ...over,
});

const aRecord = (over: Partial<PayrollRecord> = {}): PayrollRecord => ({
  id: 'p1',
  employee_id: 'emp-1',
  year: 2025,
  month: 8,
  regular_minutes: 0,
  overtime_minutes: 0,
  shabbat_minutes: 0,
  holiday_minutes: 0,
  regular_pay: '0.00',
  overtime_pay: '0.00',
  shabbat_pay: '0.00',
  holiday_pay: '0.00',
  travel: '0.00',
  bonuses: '0.00',
  deductions: '0.00',
  total_pay: '0.00',
  status: 'draft',
  calculated_at: null,
  allocations: [],
  ...over,
});

describe('parseAmount — a decimal string to a number for formatting', () => {
  it('parses a plain decimal', () => {
    expect(parseAmount('7420.00')).toBe(7420);
    expect(parseAmount('157.50')).toBe(157.5);
  });

  it('reads a missing or unparseable value as zero, never NaN', () => {
    expect(parseAmount(null)).toBe(0);
    expect(parseAmount(undefined)).toBe(0);
    expect(parseAmount('')).toBe(0);
    expect(parseAmount('not-a-number')).toBe(0);
  });
});

describe('payBuckets — the four buckets in display order (Requirement 16.6)', () => {
  it('carries each bucket’s minutes and pay', () => {
    const record = aRecord({
      regular_minutes: 480,
      overtime_minutes: 90,
      shabbat_minutes: 0,
      holiday_minutes: 60,
      regular_pay: '240.00',
      overtime_pay: '67.50',
      shabbat_pay: '0.00',
      holiday_pay: '52.50',
    });

    const buckets = payBuckets(record);

    expect(buckets.map((b) => b.key)).toEqual(['regular', 'overtime', 'shabbat', 'holiday']);
    expect(buckets[0]).toEqual({ key: 'regular', minutes: 480, pay: 240 });
    expect(buckets[1]).toEqual({ key: 'overtime', minutes: 90, pay: 67.5 });
    expect(buckets[3]).toEqual({ key: 'holiday', minutes: 60, pay: 52.5 });
  });
});

describe('totalMinutes — minutes worked across the four buckets', () => {
  it('sums the buckets', () => {
    const record = aRecord({
      regular_minutes: 480,
      overtime_minutes: 90,
      shabbat_minutes: 30,
      holiday_minutes: 60,
    });
    expect(totalMinutes(record)).toBe(660);
  });
});

describe('per-site allocation (Requirement 16.8)', () => {
  it('sums an allocation’s minutes across its buckets', () => {
    const allocation = anAllocation({
      regular_minutes: 270,
      overtime_minutes: 0,
      shabbat_minutes: 0,
      holiday_minutes: 0,
    });
    expect(allocationMinutes(allocation)).toBe(270);
  });

  it('sums the per-site costs to the record total — the brief’s split (16.8)', () => {
    // The design's example: 157.50 ₪ and 175.00 ₪ allocated across two sites, totalling 332.50 ₪.
    const record = aRecord({
      allocations: [
        anAllocation({ site_id: 'site-a', cost: '157.50' }),
        anAllocation({ site_id: 'site-b', cost: '175.00' }),
      ],
    });
    expect(allocationsTotal(record)).toBe(332.5);
  });
});
