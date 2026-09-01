/**
 * The payroll screen (Requirement 16.6, 16.8, 2.6).
 *
 * Payroll is wage data, so the whole screen is for administrators and accounting only: the server
 * puts every payroll endpoint behind a finance-role guard (Requirement 2.6), and the console hides the
 * nav item from the other roles, so a site manager or an employee never arrives here. This screen
 * reads what the guarded endpoints return and does no redaction of its own.
 *
 * It shows, for a chosen month, the per-employee payroll records — each a row with its bucket
 * breakdown (regular, overtime, Shabbat, holiday) and the month's total (Requirement 16.6). A
 * recalculate action recomputes an employee's month from its Approved or Locked entries, replacing the
 * existing draft rather than duplicating it (Requirement 16.10); it can be run per row or from the
 * toolbar for a chosen employee with optional adjustments. Selecting a row opens a drill-down with the
 * allowances, deductions, total, and the per-site cost allocation whose parts sum to the worked pay
 * (Requirement 16.8).
 *
 * Every string is a translation key; money goes through `formatCurrency`, minutes through
 * `formatDuration`, and the layout uses logical properties, so Hebrew renders right-to-left unchanged.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { employeeKeys, listEmployees } from '@/api/employees';
import { calculatePayroll, listPayroll, payrollKeys } from '@/api/payroll';
import type { EmployeeListItem, PayrollRecord } from '@/api/types';
import { PayrollDetail } from '@/app/payroll/PayrollDetail';
import { RecalculatePanel } from '@/app/payroll/RecalculatePanel';
import { Drawer } from '@/components/management/Drawer';
import { Pagination } from '@/components/management/Pagination';
import { EmptyState, ErrorState, LoadingState } from '@/components/management/QueryState';
import { PayrollStatusPill } from '@/components/management/StatusPill';
import { payBuckets, totalMinutes } from '@/app/payroll/payrollView';
import { toError } from '@/lib/apiError';
import { formatCurrency, formatDuration } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';
import type { Language } from '@/i18n';

const PAGE_SIZE = 50;
const OPTION_PAGE = 200;

/** An em-dash placeholder, held in a value so the no-literal-strings lint rule is satisfied. */
const DASH = '—';

/** The current calendar year/month, the sensible default period to read. */
const now = () => {
  const date = new Date();
  return { year: date.getFullYear(), month: date.getMonth() + 1 };
};

const toMonthValue = (year: number, month: number): string =>
  `${year}-${String(month).padStart(2, '0')}`;

const parseMonthValue = (value: string): { year: number; month: number } | null => {
  const match = /^(\d{4})-(\d{2})$/.exec(value);
  if (!match) {
    return null;
  }
  return { year: Number(match[1]), month: Number(match[2]) };
};

/** An employee's reader-language name first, the other form in parentheses to disambiguate. */
const employeeLabel = (language: Language, name: string, nameEn: string): string => {
  const primary = language === 'he' ? name : nameEn;
  const secondary = language === 'he' ? nameEn : name;
  return primary === secondary ? primary : `${primary} (${secondary})`;
};

export function PayrollPage() {
  const { t } = useTranslation();
  const language = useLanguage();
  const queryClient = useQueryClient();

  const initial = now();
  const [monthValue, setMonthValue] = useState(toMonthValue(initial.year, initial.month));
  const [employeeId, setEmployeeId] = useState('');
  const [offset, setOffset] = useState(0);

  // The record whose drill-down is open (Requirement 16.8), and the recalculate panel's open state.
  const [viewing, setViewing] = useState<PayrollRecord | null>(null);
  const [recalculating, setRecalculating] = useState(false);
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [errorParams, setErrorParams] = useState<Record<string, string>>({});

  const period = useMemo(() => parseMonthValue(monthValue), [monthValue]);

  const onFilterChange = <T,>(setter: (value: T) => void) => (value: T) => {
    setter(value);
    setOffset(0);
  };

  const params = useMemo(
    () => ({
      year: period?.year ?? null,
      month: period?.month ?? null,
      employee_id: employeeId || null,
      limit: PAGE_SIZE,
      offset,
    }),
    [period, employeeId, offset],
  );

  const query = useQuery({
    queryKey: payrollKeys.list(params),
    queryFn: () => listPayroll(params),
  });

  // The roster, to name a record's employee and to populate the filter and recalculate selectors.
  const employeeParams = { status: null, limit: OPTION_PAGE, offset: 0 };
  const employees = useQuery({
    queryKey: employeeKeys.list(employeeParams),
    queryFn: () => listEmployees(employeeParams),
  });

  // A name lookup keyed by id, so a record row can label itself from the roster the filter loaded.
  const nameFor = useMemo(() => {
    const map = new Map<string, EmployeeListItem>();
    for (const employee of employees.data?.items ?? []) {
      map.set(employee.id, employee);
    }
    return (id: string): string => {
      const employee = map.get(id);
      return employee ? employeeLabel(language, employee.full_name, employee.full_name_en) : id;
    };
  }, [employees.data, language]);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: payrollKeys.all });

  // A quick per-row recalculate: recompute one employee's month with the adjustments it already has.
  // The panel handles the richer path (choosing an employee, entering travel/bonuses/deductions).
  const rowRecalc = useMutation({
    mutationFn: (record: PayrollRecord) =>
      calculatePayroll({ employee_id: record.employee_id, year: record.year, month: record.month }),
    onSuccess: async () => {
      setErrorKey(null);
      setErrorParams({});
      await invalidate();
    },
    onError: (error) => {
      const translated = toError(error);
      setErrorKey(translated.key);
      setErrorParams(translated.params);
    },
  });

  return (
    <section className="page-body">
      <div className="page-header">
        <h1>{t('nav.payroll')}</h1>
        <div className="page-header__actions">
          <button
            type="button"
            className="button button--primary"
            disabled={period === null}
            onClick={() => {
              setErrorKey(null);
              setRecalculating(true);
            }}
          >
            {t('payroll.recalculate')}
          </button>
        </div>
      </div>
      <p className="subtitle">{t('payroll.subtitle')}</p>

      <div className="toolbar">
        <label className="field field--inline">
          <span className="field__label">{t('payroll.month')}</span>
          <input
            className="input"
            type="month"
            value={monthValue}
            onChange={(event) => onFilterChange(setMonthValue)(event.target.value)}
          />
        </label>

        <select
          className="select"
          aria-label={t('payroll.filterEmployee')}
          value={employeeId}
          onChange={(event) => onFilterChange(setEmployeeId)(event.target.value)}
        >
          <option value="">{t('payroll.allEmployees')}</option>
          {(employees.data?.items ?? []).map((employee) => (
            <option key={employee.id} value={employee.id}>
              {employeeLabel(language, employee.full_name, employee.full_name_en)}
            </option>
          ))}
        </select>
      </div>

      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey, errorParams)}
        </p>
      ) : null}

      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState />
      ) : (query.data?.items ?? []).length === 0 ? (
        <EmptyState messageKey="payroll.empty" />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{t('payroll.employee')}</th>
                <th className="numeric">{t('payroll.bucket.regular')}</th>
                <th className="numeric">{t('payroll.bucket.overtime')}</th>
                <th className="numeric">{t('payroll.bucket.shabbat')}</th>
                <th className="numeric">{t('payroll.bucket.holiday')}</th>
                <th className="numeric">{t('payroll.totalHours')}</th>
                <th className="numeric">{t('payroll.total')}</th>
                <th>{t('common.status')}</th>
                <th>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {(query.data?.items ?? []).map((record) => (
                <PayrollRow
                  key={record.id}
                  record={record}
                  name={nameFor(record.employee_id)}
                  language={language}
                  onView={() => setViewing(record)}
                  onRecalculate={() => rowRecalc.mutate(record)}
                  recalculating={rowRecalc.isPending && rowRecalc.variables?.id === record.id}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {query.data ? (
        <Pagination
          total={query.data.total}
          limit={PAGE_SIZE}
          offset={offset}
          onOffsetChange={setOffset}
        />
      ) : null}

      {viewing ? (
        <Drawer
          title={t('payroll.recordTitle', { name: nameFor(viewing.employee_id) })}
          onClose={() => setViewing(null)}
        >
          <PayrollDetail
            record={viewing}
            language={language}
            onRecalculated={async (updated) => {
              setViewing(updated);
              await invalidate();
            }}
          />
        </Drawer>
      ) : null}

      {recalculating && period ? (
        <Drawer title={t('payroll.recalculateTitle')} onClose={() => setRecalculating(false)}>
          <RecalculatePanel
            year={period.year}
            month={period.month}
            employees={employees.data?.items ?? []}
            initialEmployeeId={employeeId || null}
            language={language}
            onDone={async () => {
              setRecalculating(false);
              await invalidate();
            }}
            onCancel={() => setRecalculating(false)}
          />
        </Drawer>
      ) : null}
    </section>
  );
}

/** One employee's monthly record as a row: the bucket breakdown, total hours and total pay. */
function PayrollRow({
  record,
  name,
  language,
  onView,
  onRecalculate,
  recalculating,
}: {
  record: PayrollRecord;
  name: string;
  language: Language;
  onView: () => void;
  onRecalculate: () => void;
  recalculating: boolean;
}) {
  const { t } = useTranslation();
  const buckets = payBuckets(record);
  const total = parseFloat(record.total_pay);

  return (
    <tr>
      <td>
        <button type="button" className="link-button" onClick={onView}>
          {name}
        </button>
      </td>
      {buckets.map((bucket) => (
        <td key={bucket.key} className="numeric">
          {bucket.minutes === 0 ? DASH : formatDuration(bucket.minutes)}
        </td>
      ))}
      <td className="numeric">{formatDuration(totalMinutes(record))}</td>
      <td className="numeric">{formatCurrency(language, Number.isFinite(total) ? total : 0)}</td>
      <td>
        <PayrollStatusPill status={record.status} />
      </td>
      <td>
        <span className="row-actions">
          <button type="button" className="button button--small" onClick={onView}>
            {t('payroll.view')}
          </button>
          {/* A locked month's record is final and must not be recomputed (Requirement 16.10); the
              server refuses it, and hiding the button keeps the row honest about what it will do. */}
          {record.status === 'final' ? (
            <span className="detail__label">{t('payroll.finalNote')}</span>
          ) : (
            <button
              type="button"
              className="button button--small"
              disabled={recalculating}
              onClick={onRecalculate}
            >
              {recalculating ? t('common.saving') : t('payroll.recalculateRow')}
            </button>
          )}
        </span>
      </td>
    </tr>
  );
}
