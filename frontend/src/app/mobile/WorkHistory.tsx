/**
 * Recent work history on the employee home (Requirement 23.3): each recent day as a date and the
 * total hours worked that day.
 *
 * The component is pure presentation over a list of day summaries. Totals are whole minutes rendered
 * as h:mm through the shared formatter, with the digits isolated so Hebrew text around them is not
 * reordered by the bidirectional algorithm. An empty list shows a plain "no recent work" line rather
 * than a bare heading.
 *
 * The data itself comes from the employee's own completed entries, served by `GET /api/scans/history`
 * scoped server-side to the caller's linked employee (Requirement 23.3). The home page fetches those
 * days and passes them here; while the request is in flight it passes an empty list, which renders as
 * "no recent work" until the days arrive.
 */

import { useTranslation } from 'react-i18next';

import { formatDate, formatDuration } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';

/** One day of the employee's history: the local work date and the minutes worked across all sites. */
export interface DayHistory {
  /** The local calendar date, canonical YYYY-MM-DD. */
  work_date: string;
  /** Total worked minutes that day, summed across every site (Requirement 11.2). */
  total_minutes: number;
}

interface WorkHistoryProps {
  days: DayHistory[];
}

export function WorkHistory({ days }: WorkHistoryProps) {
  const { t } = useTranslation();
  const language = useLanguage();

  return (
    <section className="history" aria-label={t('mobile.history')}>
      <h2 className="history__title">{t('mobile.history')}</h2>
      {days.length === 0 ? (
        <p className="subtitle">{t('mobile.noHistory')}</p>
      ) : (
        <ul className="history__list">
          {days.map((day) => (
            <li key={day.work_date} className="history__row">
              <span className="history__date">{formatDate(language, day.work_date)}</span>
              <span className="numeric history__hours">{formatDuration(day.total_minutes)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
