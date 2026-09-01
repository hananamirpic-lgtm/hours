/**
 * A coloured status pill for an employee or site status. The label comes through `t()` so it is
 * bilingual; the colour is chosen by the status so the state reads at a glance.
 */

import { useTranslation } from 'react-i18next';

import type { CalculationStatus, EmployeeStatus, SiteStatus, TimeEntryStatus } from '@/api/types';

const EMPLOYEE_TONE: Record<EmployeeStatus, string> = {
  active: 'pill--active',
  on_leave: 'pill--warn',
  inactive: 'pill--muted',
  terminated: 'pill--danger',
};

const SITE_TONE: Record<SiteStatus, string> = {
  active: 'pill--active',
  completed: 'pill--muted',
  on_hold: 'pill--warn',
};

export function EmployeeStatusPill({ status }: { status: EmployeeStatus }) {
  const { t } = useTranslation();
  return <span className={`pill ${EMPLOYEE_TONE[status]}`}>{t(`employeeStatus.${status}`)}</span>;
}

export function SiteStatusPill({ status }: { status: SiteStatus }) {
  const { t } = useTranslation();
  return <span className={`pill ${SITE_TONE[status]}`}>{t(`siteStatus.${status}`)}</span>;
}

const TIME_ENTRY_TONE: Record<TimeEntryStatus, string> = {
  draft: 'pill--muted',
  review: 'pill--warn',
  approved: 'pill--active',
  locked: 'pill--muted',
};

export function TimeEntryStatusPill({ status }: { status: TimeEntryStatus }) {
  const { t } = useTranslation();
  return <span className={`pill ${TIME_ENTRY_TONE[status]}`}>{t(`timeEntryStatus.${status}`)}</span>;
}

// A payroll record is a working draft while its month is open, or final once the month is locked and
// the figures are frozen (Requirement 16.10). Final reads as settled; draft reads as still movable.
const PAYROLL_TONE: Record<CalculationStatus, string> = {
  draft: 'pill--warn',
  final: 'pill--active',
};

export function PayrollStatusPill({ status }: { status: CalculationStatus }) {
  const { t } = useTranslation();
  return <span className={`pill ${PAYROLL_TONE[status]}`}>{t(`payrollStatus.${status}`)}</span>;
}
