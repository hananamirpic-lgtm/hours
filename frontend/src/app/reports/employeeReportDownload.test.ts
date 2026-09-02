import { describe, expect, it } from 'vitest';

import type { EmployeeReport, EmployeeReportRow } from '@/api/types';
import {
  employeeReportFilename,
  toEmployeeReportCsv,
  type EmployeeReportLabels,
  type EmployeeReportRenderOptions,
} from '@/app/reports/employeeReportDownload';

const labels: EmployeeReportLabels = {
  employee: 'Employee',
  regular: 'Regular',
  overtime: 'Overtime',
  shabbat: 'Shabbat',
  holiday: 'Holiday',
  total: 'Total',
  cost: 'Cost',
  totalsRow: 'Total',
};

const aRow = (over: Partial<EmployeeReportRow> = {}): EmployeeReportRow => ({
  employee_id: 'emp-1',
  employee_name: 'דנה כהן',
  employee_name_en: 'Dana Cohen',
  regular_minutes: 480,
  overtime_minutes: 90,
  shabbat_minutes: 0,
  holiday_minutes: 60,
  total_minutes: 630,
  cost: '240.00',
  ...over,
});

const aReport = (over: Partial<EmployeeReport> = {}): EmployeeReport => ({
  year: 2025,
  month: 8,
  filters: { year: 2025, month: 8, employee_id: null, site_id: null, client_id: null, project: null },
  currency: 'ILS',
  rows: [aRow()],
  total_minutes: 630,
  total_cost: '240.00',
  ...over,
});

const options = (over: Partial<EmployeeReportRenderOptions> = {}): EmployeeReportRenderOptions => ({
  includeCost: true,
  language: 'en',
  labels,
  ...over,
});

const lines = (csv: string): string[] => {
  expect(csv.startsWith('\uFEFF')).toBe(true); // Excel reads Hebrew as UTF-8
  return csv.slice(1).split('\r\n');
};

describe('toEmployeeReportCsv — the finance reader (cost present)', () => {
  it('writes a header, a row and a totals row with the cost column', () => {
    const rows = lines(toEmployeeReportCsv(aReport(), options()));

    expect(rows).toHaveLength(3);
    expect(rows[0]).toBe('Employee,Regular,Overtime,Shabbat,Holiday,Total,Cost');
    // 480 → 8:00, 90 → 1:30, 60 → 1:00, 630 → 10:30; cost through formatCurrency.
    expect(rows[1]).toBe('Dana Cohen (דנה כהן),8:00,1:30,0:00,1:00,10:30,₪240.00');
    expect(rows[2]).toBe('Total,,,,,10:30,₪240.00');
  });
});

describe('toEmployeeReportCsv — the site manager (cost null, redaction)', () => {
  it('omits the cost column from header, rows and totals', () => {
    const report = aReport({
      rows: [aRow({ cost: null })],
      total_cost: null,
    });
    const rows = lines(toEmployeeReportCsv(report, options({ includeCost: false })));

    expect(rows[0]).toBe('Employee,Regular,Overtime,Shabbat,Holiday,Total');
    expect(rows[1]).toBe('Dana Cohen (דנה כהן),8:00,1:30,0:00,1:00,10:30');
    expect(rows[2]).toBe('Total,,,,,10:30');
    // No cost value — not a zero, not an empty trailing cell — appears anywhere.
    expect(rows.some((line) => line.includes('₪') || line.includes('240'))).toBe(false);
  });
});

describe('toEmployeeReportCsv — CSV escaping (RFC 4180)', () => {
  it('quotes a name with a comma and doubles an embedded quote', () => {
    const report = aReport({
      rows: [
        aRow({ employee_name_en: 'Cohen, Dana', employee_name: 'Cohen, Dana' }),
        aRow({ employee_name_en: 'Dana "Dee" Cohen', employee_name: 'Dana "Dee" Cohen' }),
      ],
    });
    const rows = lines(toEmployeeReportCsv(report, options()));

    expect(rows[1]).toBe('"Cohen, Dana",8:00,1:30,0:00,1:00,10:30,₪240.00');
    expect(rows[2]).toBe('"Dana ""Dee"" Cohen",8:00,1:30,0:00,1:00,10:30,₪240.00');
  });
});

describe('employeeReportFilename — period in the name', () => {
  it('zero-pads the month', () => {
    expect(employeeReportFilename(aReport({ month: 8 }), 'csv')).toBe('employees_2025_08.csv');
    expect(employeeReportFilename(aReport({ month: 12 }), 'pdf')).toBe('employees_2025_12.pdf');
  });
});
