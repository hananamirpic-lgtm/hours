import { describe, expect, it } from 'vitest';

import type { BillingSummary, ClientBilling, SiteBilling } from '@/api/types';
import {
  clientById,
  hasExcluded,
  hasProfit,
  parseAmount,
  siteMinutes,
  sitesForClient,
} from '@/app/billing/billingView';

const aSite = (over: Partial<SiteBilling> = {}): SiteBilling => ({
  site_id: 'site-a',
  client_id: 'client-1',
  regular_minutes: 0,
  overtime_minutes: 0,
  billing_rate_applied: '60.00',
  overtime_rate_applied: null,
  billing: '0.00',
  cost: null,
  profit: null,
  status: 'draft',
  calculated_at: null,
  ...over,
});

const aClient = (over: Partial<ClientBilling> = {}): ClientBilling => ({
  client_id: 'client-1',
  billing: '0.00',
  cost: '0.00',
  profit: '0.00',
  ...over,
});

const aSummary = (over: Partial<BillingSummary> = {}): BillingSummary => ({
  year: 2025,
  month: 8,
  sites: [],
  clients: [],
  total_billing: '0.00',
  total_cost: '0.00',
  total_profit: '0.00',
  excluded_entry_count: 0,
  excluded_minutes: 0,
  ...over,
});

describe('parseAmount — a decimal string to a number for formatting', () => {
  it('parses a plain decimal', () => {
    expect(parseAmount('645.00')).toBe(645);
    expect(parseAmount('312.50')).toBe(312.5);
  });

  it('reads a missing, null or unparseable value as zero, never NaN', () => {
    expect(parseAmount(null)).toBe(0);
    expect(parseAmount(undefined)).toBe(0);
    expect(parseAmount('')).toBe(0);
    expect(parseAmount('not-a-number')).toBe(0);
  });
});

describe('siteMinutes — a site’s billable minutes (Requirement 17.2)', () => {
  it('sums the regular and overtime minutes', () => {
    // The brief's example: 4.5 h regular + 5.0 h overtime = 9.5 h = 570 minutes.
    expect(siteMinutes(aSite({ regular_minutes: 270, overtime_minutes: 300 }))).toBe(570);
  });
});

describe('hasProfit — cost and profit are present only after a calculation (Requirement 17.4)', () => {
  it('is false for a stored read, where every site cost is null', () => {
    const summary = aSummary({ sites: [aSite({ cost: null }), aSite({ cost: null })] });
    expect(hasProfit(summary)).toBe(false);
  });

  it('is true once a calculation fills the cost in', () => {
    const summary = aSummary({ sites: [aSite({ cost: '332.50', profit: '312.50' })] });
    expect(hasProfit(summary)).toBe(true);
  });

  it('is false for an empty summary', () => {
    expect(hasProfit(aSummary())).toBe(false);
  });
});

describe('hasExcluded — the excluded-hours notice (Requirement 17.6)', () => {
  it('is true when unapproved entries were left out', () => {
    expect(hasExcluded(aSummary({ excluded_entry_count: 2, excluded_minutes: 240 }))).toBe(true);
  });

  it('is true when only the minutes are set', () => {
    expect(hasExcluded(aSummary({ excluded_entry_count: 0, excluded_minutes: 60 }))).toBe(true);
  });

  it('is false when nothing was excluded', () => {
    expect(hasExcluded(aSummary())).toBe(false);
  });
});

describe('per-client drill-down (Requirement 17.3)', () => {
  const summary = aSummary({
    sites: [
      aSite({ site_id: 's1', client_id: 'client-1', billing: '645.00' }),
      aSite({ site_id: 's2', client_id: 'client-2', billing: '100.00' }),
      aSite({ site_id: 's3', client_id: 'client-1', billing: '55.00' }),
    ],
    clients: [
      aClient({ client_id: 'client-1', billing: '700.00', cost: '332.50', profit: '367.50' }),
      aClient({ client_id: 'client-2', billing: '100.00', cost: '40.00', profit: '60.00' }),
    ],
  });

  it('returns exactly a client’s sites in the summary’s order', () => {
    const sites = sitesForClient(summary, 'client-1');
    expect(sites.map((site) => site.site_id)).toEqual(['s1', 's3']);
  });

  it('returns no sites for a client absent from the summary', () => {
    expect(sitesForClient(summary, 'client-x')).toEqual([]);
  });

  it('looks a client’s aggregate row up by id', () => {
    expect(clientById(summary, 'client-2')?.billing).toBe('100.00');
    expect(clientById(summary, 'client-x')).toBeNull();
  });
});
