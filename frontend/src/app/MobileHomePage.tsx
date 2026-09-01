/**
 * The employee home (Requirement 23).
 *
 * One screen, one large action. It shows a greeting, and when a shift is open it shows the check-in
 * time and makes the primary action check-out; when none is open the action is scan-to-check-in
 * (Requirement 23.1, 23.2). Below sit the employee's own alerts (23.7) and recent work history by day
 * (23.3). Every control is at least 44 px and the layout is flow-relative, so it works one-handed and
 * mirrors for Hebrew from the one stylesheet (23.4).
 *
 * The scan flow is a small state machine (see `scanFlow`). Scanning opens the camera — the only
 * permission asked for, never location (23.8). A decoded token is submitted; a success confirms with
 * site, action and local time (23.5); a 409 opens the conflict dialog with transition / end-and-move
 * / cancel (11.4, 11.6). Offline is handled up front and again on failure: a scan attempted with no
 * connection, or one whose request is lost, states plainly that nothing was recorded and never shows
 * a false success (23.6).
 *
 * The site name is not returned by the scan or status endpoints, so the open-shift card shows the
 * check-in time without a fabricated site id, and the confirmation names a site only after a
 * transition, where the prior conflict supplied the name.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  checkOut as checkOutRequest,
  endAndMove as endAndMoveRequest,
  getScanStatus,
  getWorkHistory,
  recordScan,
  scanKeys,
  transition as transitionRequest,
} from '@/api/scans';
import type { ScanResult } from '@/api/types';
import { useAuth } from '@/auth/AuthProvider';
import { ConfirmationPanel } from '@/app/mobile/ConfirmationPanel';
import { ConflictDialog } from '@/app/mobile/ConflictDialog';
import { QrScanner } from '@/app/mobile/QrScanner';
import { WorkHistory } from '@/app/mobile/WorkHistory';
import {
  confirming,
  failureToState,
  idle,
  offline as offlineState,
  scanning,
  submitting,
  type ScanFlowState,
} from '@/app/mobile/scanFlow';
import { formatTime } from '@/lib/format';
import { useLanguage } from '@/lib/useLanguage';
import { useOnline } from '@/lib/useOnline';

export function MobileHomePage() {
  const { t } = useTranslation();
  const language = useLanguage();
  const { user } = useAuth();
  const online = useOnline();
  const queryClient = useQueryClient();

  const statusQuery = useQuery({ queryKey: scanKeys.status, queryFn: getScanStatus });
  const openShift = statusQuery.data?.open_shift ?? null;

  // The employee's own recent completed work by day (Requirement 23.3), served scoped to their
  // linked employee. While it loads there is nothing to show yet, so an empty list stands in and
  // `WorkHistory` renders "no recent work" until the days arrive.
  const historyQuery = useQuery({ queryKey: scanKeys.history, queryFn: () => getWorkHistory() });
  const history = historyQuery.data?.days ?? [];

  const [flow, setFlow] = useState<ScanFlowState>(idle);
  // The name of the site a confirmed transition moved to, so the confirmation can state it.
  const [transitionedSiteName, setTransitionedSiteName] = useState<string | null>(null);

  const settle = (result: ScanResult, siteName: string | null) => {
    setTransitionedSiteName(siteName);
    setFlow(confirming(result));
    void queryClient.invalidateQueries({ queryKey: scanKeys.status });
    // A settled scan may have just closed a shift, which becomes a completed day the history shows,
    // so refresh it alongside the open-shift status (Requirement 23.3).
    void queryClient.invalidateQueries({ queryKey: scanKeys.history });
  };

  // Each write shares the same success and failure handling: a success confirms and refreshes the
  // open-shift status; a failure is classified into offline / conflict / error and never a success.
  const scanMutation = useMutation({
    mutationFn: (token: string) => recordScan({ qr_token: token }),
    onSuccess: (result) => settle(result, null),
    onError: (error, token) => setFlow(failureToState(error, token)),
  });

  const checkoutMutation = useMutation({
    mutationFn: () => checkOutRequest(),
    onSuccess: (result) => settle(result, null),
    onError: (error) => setFlow(failureToState(error, '')),
  });

  const transitionMutation = useMutation({
    mutationFn: (token: string) => transitionRequest({ qr_token: token }),
    onError: (error, token) => setFlow(failureToState(error, token)),
  });

  const endAndMoveMutation = useMutation({
    mutationFn: () => endAndMoveRequest(),
    onSuccess: (result) => settle(result, null),
    onError: (error) => setFlow(failureToState(error, '')),
  });

  const busy = flow.kind === 'submitting' || transitionMutation.isPending || endAndMoveMutation.isPending;

  const startScan = () => {
    // Refuse before the camera even opens if the device is offline: a scan we cannot record must not
    // look like it is under way (Requirement 23.6).
    if (!online) {
      setFlow(offlineState);
      return;
    }
    setFlow(scanning);
  };

  const onDecoded = (token: string) => {
    if (!online) {
      setFlow(offlineState);
      return;
    }
    setFlow(submitting);
    scanMutation.mutate(token);
  };

  const onCheckOut = () => {
    if (!online) {
      setFlow(offlineState);
      return;
    }
    setFlow(submitting);
    checkoutMutation.mutate();
  };

  const onTransition = (token: string, siteName: string) => {
    transitionMutation.mutate(token, {
      onSuccess: (result) => settle(result, siteName),
    });
  };

  const reset = () => {
    setTransitionedSiteName(null);
    setFlow(idle);
  };

  // Alerts are the employee's own: the flags the current open shift carries (e.g. an unassigned-site
  // check-in). No open shift, no flags — nothing to show (Requirement 23.7).
  const alerts = openShift?.flags ?? [];

  if (flow.kind === 'scanning') {
    return <QrScanner onDecoded={onDecoded} onCancel={reset} />;
  }

  return (
    <section className="mobile-home">
      <p className="mobile-home__greeting">{t('mobile.greeting', { name: user?.username ?? '' })}</p>

      {statusQuery.isPending ? (
        <p className="subtitle">{t('common.loading')}</p>
      ) : openShift ? (
        <div className="card status-card">
          <p className="status-card__label">{t('mobile.checkedIn')}</p>
          <p className="status-card__time">
            {t('mobile.since')} <span className="numeric">{formatTime(language, openShift.check_in_at)}</span>
          </p>
        </div>
      ) : (
        <p className="subtitle">{t('mobile.noOpenShift')}</p>
      )}

      {!online ? (
        <p className="feedback feedback--error" role="status">
          {t('mobile.offlineBanner')}
        </p>
      ) : null}

      {/* The one large action: check-out when a shift is open, scan when not (Requirement 23.2). */}
      {openShift ? (
        <button type="button" className="button button--primary button--large" onClick={onCheckOut} disabled={busy}>
          {t('mobile.checkOut')}
        </button>
      ) : (
        <button type="button" className="button button--primary button--large" onClick={startScan} disabled={busy}>
          {t('mobile.scan')}
        </button>
      )}

      {flow.kind === 'submitting' ? <p className="subtitle">{t('scan.submitting')}</p> : null}

      {flow.kind === 'confirming' ? (
        <ConfirmationPanel result={flow.result} siteName={transitionedSiteName} onDone={reset} />
      ) : null}

      {flow.kind === 'conflict' ? (
        <ConflictDialog
          conflict={flow.conflict}
          busy={busy}
          onTransition={() => onTransition(flow.conflict.pendingToken, flow.conflict.siteName)}
          onEndAndMove={() => endAndMoveMutation.mutate()}
          onCancel={reset}
        />
      ) : null}

      {flow.kind === 'offline' ? (
        <div className="feedback feedback--error" role="alert">
          <p>{t('scan.notRecorded')}</p>
          <button type="button" className="button button--large" onClick={reset}>
            {t('common.close')}
          </button>
        </div>
      ) : null}

      {flow.kind === 'error' ? (
        <div className="feedback feedback--error" role="alert">
          <p>{t([`apiError.${flow.code ?? 'unknown'}`, 'apiError.unknown'])}</p>
          <button type="button" className="button button--large" onClick={reset}>
            {t('common.close')}
          </button>
        </div>
      ) : null}

      {alerts.length > 0 ? (
        <section className="alerts" aria-label={t('mobile.alerts')}>
          <h2 className="alerts__title">{t('mobile.alerts')}</h2>
          <ul className="alerts__list">
            {alerts.map((flag) => (
              <li key={flag} className="alerts__item">
                {t([`scan.flag.${flag}`, 'scan.flag.generic'])}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <WorkHistory days={history} />
    </section>
  );
}
