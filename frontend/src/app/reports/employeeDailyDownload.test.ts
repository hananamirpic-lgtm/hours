import { describe, expect, it } from 'vitest';

import type { EmployeeDailyReport, EmployeeDailyRow } from '@/api/types';
import {
  employeeDailyFilename,
  employeeDailyName,
  toEmployeeDailyCsv,
  type EmployeeDailyLabels,
  type EmployeeDailyRenderOptions,
} from '@/app/reports/employeeDailyDownload';

const labels: EmployeeDailyLabels = {
  employee: 'Employee',
  total: 'Total',
  approved: 'Approved',
  notApproved: 'Not approved',
};

const aRow = (over: Partial<EmployeeDailyRow> = {}): EmployeeDailyRow => ({
  employee_id: 'emp-1',
  employee_name: 'דנה כהן',
  employee_name_en: 'Dana Cohen',
  employee_number: '2000',
  total_minutes: 630,
  approved_minutes: 480,
  not_approved_minutes: 150,
  ...over,
});

const aReport = (over: Partial<EmployeeDailyReport> = {}): EmployeeDailyReport => ({
  year: 2025,
  month: 8,
  rows: [aRow()],
  ...over,
});

const options = (over: Partial<EmployeeDailyRenderOptions> = {}): EmployeeDailyRenderOptions => ({
  language: 'en',
  labels,
  ...over,
});

const lines = (csv: string): string[] => {
  expect(csv.startsWith('\uFEFF')).toBe(true); // Excel reads Hebrew as UTF-8
  return csv.slice(1).split('\r\n');
};

describe('toEmployeeDailyCsv — header and rows', () => {
  it('writes a header and one row per employee, minutes as h:mm', () => {
    const rows = lines(toEmployeeDailyCsv(aReport(), options()));

    expect(rows).toHaveLength(2); // header + one employee, no totals row
    expect(rows[0]).toBe('Employee,Total,Approved,Not approved');
    // 630 → 10:30, 480 → 8:00, 150 → 2:30.
    expect(rows[1]).toBe('Dana Cohen (דנה כהן),10:30,8:00,2:30');
  });

  it('carries no money field anywhere', () => {
    const csv = toEmployeeDailyCsv(aReport(), options());
    expect(csv.includes('₪')).toBe(false);
  });
});

describe('employeeDailyName — fallback when employee_name_en is null', () => {
  it('falls back to the Hebrew name when the English name is null', () => {
    const row = aRow({ employee_name_en: null });
    // English is absent, so the name shows alone — never "Hebrew ()" or an empty parenthetical.
    expect(employeeDailyName('en', row)).toBe('דנה כהן');
    expect(employeeDailyName('he', row)).toBe('דנה כהן');
  });

  it('renders a null English name without a money or empty cell in the CSV row', () => {
    const rows = lines(toEmployeeDailyCsv(aReport({ rows: [aRow({ employee_name_en: null })] }), options()));
    expect(rows[1]).toBe('דנה כהן,10:30,8:00,2:30');
  });
});

describe('toEmployeeDailyCsv — CSV escaping (RFC 4180)', () => {
  it('quotes a name with a comma and doubles an embedded quote', () => {
    const report = aReport({
      rows: [
        aRow({ employee_name_en: 'Cohen, Dana', employee_name: 'Cohen, Dana' }),
        aRow({ employee_name_en: 'Dana "Dee" Cohen', employee_name: 'Dana "Dee" Cohen' }),
      ],
    });
    const rows = lines(toEmployeeDailyCsv(report, options()));

    expect(rows[1]).toBe('"Cohen, Dana",10:30,8:00,2:30');
    expect(rows[2]).toBe('"Dana ""Dee"" Cohen",10:30,8:00,2:30');
  });
});

describe('employeeDailyFilename — period in the name', () => {
  it('zero-pads the month', () => {
    expect(employeeDailyFilename(aReport({ month: 8 }))).toBe('employee_daily_2025_08.csv');
    expect(employeeDailyFilename(aReport({ month: 12 }))).toBe('employee_daily_2025_12.csv');
  });
});
