/**
 * The no-QR self check-in picker (Requirement 7, 9, 23).
 *
 * The secondary path off the home screen when there is no QR to scan — the camera is broken, or no
 * code is reachable. It lists the employee's own assigned sites (fetched from GET /api/scans/my-sites,
 * self-scoped server-side) and lets them pick one to open a shift against. It states plainly, before
 * the choice, that a no-QR check-in has no presence proof and so must be approved by a manager, so
 * the employee is never surprised that the shift starts Draft.
 *
 * This is deliberately secondary to scanning: the QR path is the primary, anti-fraud one, and this
 * dialog is only reached from a smaller link beneath the big Scan button. While the sites load there
 * is a quiet loading line; if the employee is assigned to no active site there is nothing to pick,
 * and the dialog says so rather than offering an empty list. Every control is at least 44 px and the
 * layout is flow-relative, so it works one-handed and mirrors for Hebrew from the one stylesheet.
 */

import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { getMySites, scanKeys } from '@/api/scans';
import { HindiHelper } from '@/components/HindiHelper';

interface SelfCheckInPickerProps {
  busy: boolean;
  /** Open a self-reported shift at the chosen site. */
  onPick: (siteId: string) => void;
  onCancel: () => void;
}

export function SelfCheckInPicker({ busy, onPick, onCancel }: SelfCheckInPickerProps) {
  const { t } = useTranslation();

  const sitesQuery = useQuery({ queryKey: scanKeys.mySites, queryFn: getMySites });
  const sites = sitesQuery.data?.sites ?? [];

  return (
    <div className="self-check-in feedback" role="dialog" aria-label={t('mobile.selfCheckIn.title')}>
      <p className="self-check-in__headline">
        {t('mobile.selfCheckIn.title')}
        <HindiHelper textKey="mobile.selfCheckIn.title" />
      </p>
      <p className="self-check-in__note">
        {t('mobile.selfCheckIn.approvalNote')}
        <HindiHelper textKey="mobile.selfCheckIn.approvalNote" />
      </p>

      {sitesQuery.isPending ? (
        <p className="subtitle">
          {t('common.loading')}
          <HindiHelper textKey="common.loading" />
        </p>
      ) : sites.length === 0 ? (
        <p className="subtitle">
          {t('mobile.selfCheckIn.noSites')}
          <HindiHelper textKey="mobile.selfCheckIn.noSites" />
        </p>
      ) : (
        <ul className="self-check-in__list">
          {sites.map((site) => (
            <li key={site.id} className="self-check-in__item">
              <button
                type="button"
                className="button button--large"
                onClick={() => onPick(site.id)}
                disabled={busy}
              >
                {site.name}
              </button>
            </li>
          ))}
        </ul>
      )}

      <button type="button" className="button button--large" onClick={onCancel} disabled={busy}>
        {t('common.cancel')}
        <HindiHelper textKey="common.cancel" />
      </button>
    </div>
  );
}
