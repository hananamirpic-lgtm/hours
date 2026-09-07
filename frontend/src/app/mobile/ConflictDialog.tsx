/**
 * The open-shift-elsewhere conflict (Requirement 11.4, 11.6).
 *
 * When an employee scans in at one site while a shift is still open at another, the server refuses
 * the plain check-in and names the other site. This dialog states that plainly and offers exactly the
 * choices the requirement lists:
 *
 *  - Check out of the previous site and move here: the confirmed transition, which closes the old
 *    shift and opens the new one in one atomic step (Requirement 11.5). It re-sends the token the
 *    scan carried, held on the conflict state.
 *  - End work and move to another site: closes the current shift with no departure QR
 *    (Requirement 11.6), leaving the employee to scan in wherever they go next.
 *  - Cancel: nothing changes; the previous shift stays open.
 *
 * The previous site's name and the since-time come straight from the conflict envelope. The time
 * digits are isolated against the surrounding Hebrew.
 */

import { useTranslation } from 'react-i18next';

import type { ConflictState } from './scanFlow';
import { formatTime } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';
import { HindiHelper } from '@/components/HindiHelper';

interface ConflictDialogProps {
  conflict: ConflictState;
  busy: boolean;
  /** Confirm the transition to the scanned site, re-sending the pending token. */
  onTransition: () => void;
  /** End the current shift without opening a new one. */
  onEndAndMove: () => void;
  onCancel: () => void;
}

export function ConflictDialog({ conflict, busy, onTransition, onEndAndMove, onCancel }: ConflictDialogProps) {
  const { t } = useTranslation();
  const language = useLanguage();

  return (
    <div className="conflict feedback feedback--error" role="alertdialog" aria-label={t('scan.conflict.title')}>
      <p className="conflict__headline">
        {t('scan.conflict.title')}
        <HindiHelper textKey="scan.conflict.title" />
      </p>
      <p className="conflict__line">
        {conflict.since
          ? t('scan.conflict.openSince', { site: conflict.siteName })
          : t('scan.conflict.open', { site: conflict.siteName })}
        {conflict.since ? <span className="numeric"> {formatTime(language, conflict.since)}</span> : null}
        <HindiHelper
          textKey={conflict.since ? 'scan.conflict.openSince' : 'scan.conflict.open'}
          params={{ site: conflict.siteName }}
        />
      </p>

      <div className="conflict__actions">
        <button type="button" className="button button--primary button--large" onClick={onTransition} disabled={busy}>
          {t('scan.conflict.checkOutAndMove', { site: conflict.siteName })}
          <HindiHelper textKey="scan.conflict.checkOutAndMove" params={{ site: conflict.siteName }} />
        </button>
        <button type="button" className="button button--large" onClick={onEndAndMove} disabled={busy}>
          {t('scan.conflict.endAndMove')}
          <HindiHelper textKey="scan.conflict.endAndMove" />
        </button>
        <button type="button" className="button button--large" onClick={onCancel} disabled={busy}>
          {t('common.cancel')}
          <HindiHelper textKey="common.cancel" />
        </button>
      </div>
    </div>
  );
}
