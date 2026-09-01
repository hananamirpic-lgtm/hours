/**
 * The employee "My hours" screen (Requirement 23.3, 11.2).
 *
 * An employee sees all their own working hours and filters them by a quick preset — this week, this
 * month, a single day — or a custom From–To range. The range is computed in the browser's local time
 * (see `myHoursView`), and the data comes from `GET /api/scans/history` with `date_from`/`date_to`,
 * scoped server-side to the caller's own linked employee; no employee id is ever sent from the
 * client, and a login with no linked employee gets an empty list.
 *
 * The screen defaults to this month. Picking a preset sets the range; editing the custom From/To
 * inputs switches to the custom preset and drives the range directly. The range total — the sum of
 * every day's minutes — is shown prominently at the top. When the range is a whole month the days are
 * rolled up into Sun..Sat weekly groups each with its own subtotal (the user asked for this); for a
 * day, a week, or a custom range a flat day list is shown.
 *
 * Mobile-first and one-handed: the filter controls and inputs clear the 44px minimum, the layout is
 * flow-relative (logical properties only), and every label is translated. Durations are isolated with
 * `.numeric` so the bidirectional algorithm does not reorder them against Hebrew text.
 */

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { getWorkHistory, scanKeys } from '@/api/scans';
import {
  groupByWeek,
  rangeForPreset,
  totalMinutes,
  type DateRange,
  type HoursDay,
  type HoursPreset,
} from '@/app/mobile/myHoursView';
import { formatDate, formatDuration } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';

/** A day row shared by the flat list and the weekly groups. */
function DayRow({ day }: { day: HoursDay }) {
  const language = useLanguage();
  return (
    <li className="history__row">
      <span className="history__date">{formatDate(language, day.work_date)}</span>
      <span className="numeric history__hours">{formatDuration(day.total_minutes)}</span>
    </li>
  );
}

export function MobileMyHoursPage() {
  const { t } = useTranslation();
  const language = useLanguage();

  // `today` is read once per render from local time; the preset math derives every window from it.
  const today = useMemo(() => new Date(), []);

  const [preset, setPreset] = useState<HoursPreset>('month');
  // The single day the "Pick a day" preset points at, and the two custom bounds. Seeded from the
  // month so the inputs start on sensible dates.
  const initialMonth = useMemo(() => rangeForPreset('month', today), [today]);
  const [day, setDay] = useState<string>(() => rangeForPreset('day', today).from);
  const [customFrom, setCustomFrom] = useState<string>(initialMonth.from);
  const [customTo, setCustomTo] = useState<string>(initialMonth.to);

  // The active range: presets compute it; the custom inputs and the day picker drive it directly.
  const range: DateRange = useMemo(() => {
    if (preset === 'day') {
      return { from: day, to: day };
    }
    if (preset === 'custom') {
      return { from: customFrom, to: customTo };
    }
    return rangeForPreset(preset, today);
  }, [preset, day, customFrom, customTo, today]);

  const historyQuery = useQuery({
    queryKey: scanKeys.historyRange(range.from, range.to),
    queryFn: () => getWorkHistory({ dateFrom: range.from, dateTo: range.to }),
  });

  const days = useMemo(() => historyQuery.data?.days ?? [], [historyQuery.data]);
  const total = totalMinutes(days);
  // The month preset rolls the days up into weekly groups; every other range is a flat list.
  const weekly = preset === 'month';
  const weekGroups = useMemo(() => (weekly ? groupByWeek(days) : []), [weekly, days]);

  const choosePreset = (next: HoursPreset) => setPreset(next);

  return (
    <section className="my-hours">
      <h1 className="my-hours__title">{t('myHours.title')}</h1>

      <div className="my-hours__filter" role="group" aria-label={t('myHours.filter')}>
        <button
          type="button"
          className={`button my-hours__preset${preset === 'week' ? ' my-hours__preset--active' : ''}`}
          aria-pressed={preset === 'week'}
          onClick={() => choosePreset('week')}
        >
          {t('myHours.thisWeek')}
        </button>
        <button
          type="button"
          className={`button my-hours__preset${preset === 'month' ? ' my-hours__preset--active' : ''}`}
          aria-pressed={preset === 'month'}
          onClick={() => choosePreset('month')}
        >
          {t('myHours.thisMonth')}
        </button>
        <button
          type="button"
          className={`button my-hours__preset${preset === 'day' ? ' my-hours__preset--active' : ''}`}
          aria-pressed={preset === 'day'}
          onClick={() => choosePreset('day')}
        >
          {t('myHours.pickDay')}
        </button>
        <button
          type="button"
          className={`button my-hours__preset${preset === 'custom' ? ' my-hours__preset--active' : ''}`}
          aria-pressed={preset === 'custom'}
          onClick={() => choosePreset('custom')}
        >
          {t('myHours.customRange')}
        </button>
      </div>

      {preset === 'day' ? (
        <label className="my-hours__field">
          <span className="my-hours__label">{t('myHours.day')}</span>
          <input
            type="date"
            className="input"
            value={day}
            onChange={(event) => setDay(event.target.value)}
          />
        </label>
      ) : null}

      {preset === 'custom' ? (
        <div className="my-hours__dates">
          <label className="my-hours__field">
            <span className="my-hours__label">{t('myHours.from')}</span>
            <input
              type="date"
              className="input"
              value={customFrom}
              onChange={(event) => setCustomFrom(event.target.value)}
            />
          </label>
          <label className="my-hours__field">
            <span className="my-hours__label">{t('myHours.to')}</span>
            <input
              type="date"
              className="input"
              value={customTo}
              onChange={(event) => setCustomTo(event.target.value)}
            />
          </label>
        </div>
      ) : null}

      <div className="my-hours__total card">
        <span className="my-hours__total-label">{t('myHours.total')}</span>
        <span className="numeric my-hours__total-value">{formatDuration(total)}</span>
      </div>

      {historyQuery.isPending ? (
        <p className="subtitle">{t('common.loading')}</p>
      ) : historyQuery.isError ? (
        <p className="feedback feedback--error" role="alert">
          {t('common.loadError')}
        </p>
      ) : days.length === 0 ? (
        <p className="subtitle">{t('myHours.empty')}</p>
      ) : weekly ? (
        <div className="my-hours__weeks">
          {weekGroups.map((group) => (
            <section key={group.weekStart} className="my-hours__week">
              <div className="my-hours__week-head">
                <span className="my-hours__week-title">
                  {t('myHours.weekOf', { date: formatDate(language, group.weekStart) })}
                </span>
                <span className="numeric my-hours__week-subtotal">
                  {formatDuration(group.subtotalMinutes)}
                </span>
              </div>
              <ul className="history__list">
                {group.days.map((entry) => (
                  <DayRow key={entry.work_date} day={entry} />
                ))}
              </ul>
            </section>
          ))}
        </div>
      ) : (
        <ul className="history__list">
          {days.map((entry) => (
            <DayRow key={entry.work_date} day={entry} />
          ))}
        </ul>
      )}
    </section>
  );
}
