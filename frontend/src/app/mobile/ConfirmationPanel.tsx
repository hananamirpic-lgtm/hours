/**
 * The scan confirmation (Requirement 23.5): after a scan, state the action and the local time, and
 * name the site where a name is available.
 *
 * The action wording is chosen from the outcome — checked in, checked out, or "already recorded" for
 * a duplicate the server absorbed inside its window (Requirement 9.3), so a double-tap reads as a
 * settled state rather than a second event. Time comes from the server (`result.at`) and is formatted
 * in the reader's locale; the digits are isolated so the bidirectional algorithm does not reorder
 * them against Hebrew text.
 *
 * The employee scan endpoints return `site_id` but no site name, so the panel names the site only
 * when one was passed in — which happens on a confirmed transition, where the previous 409 carried
 * the target's name. On a plain check-in the site line is omitted rather than showing a raw id.
 *
 * Any anomaly flag the entry carries (an unassigned-site check-in, an implausible duration on close)
 * is surfaced as a note, so the employee is not left thinking a flagged scan was clean.
 */

import { useTranslation } from 'react-i18next';

import type { ScanResult } from '@/api/types';
import { formatTime } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';

interface ConfirmationPanelProps {
  result: ScanResult;
  /** The site name, where the flow knows one (e.g. after a transition). Omitted for a plain scan. */
  siteName?: string | null;
  onDone: () => void;
}

export function ConfirmationPanel({ result, siteName, onDone }: ConfirmationPanelProps) {
  const { t } = useTranslation();
  const language = useLanguage();

  const time = formatTime(language, result.at);

  return (
    <div className="confirm feedback feedback--success" role="alert">
      <p className="confirm__headline">{t(`scan.confirm.${result.action}`)}</p>

      {siteName ? (
        <p className="confirm__line">{t('scan.confirm.atSite', { site: siteName })}</p>
      ) : null}

      <p className="confirm__line">
        {t('scan.confirm.atTime')} <span className="numeric">{time}</span>
      </p>

      {result.flags.length > 0 ? (
        <ul className="confirm__flags">
          {result.flags.map((flag) => (
            <li key={flag}>{t([`scan.flag.${flag}`, 'scan.flag.generic'])}</li>
          ))}
        </ul>
      ) : null}

      <button type="button" className="button button--primary button--large" onClick={onDone}>
        {t('common.close')}
      </button>
    </div>
  );
}
