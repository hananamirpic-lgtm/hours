/**
 * The QR scan landing page (Requirement 3, 11, 23.5, 23.6).
 *
 * A printed site QR encodes `<public_app_url>/m/scan?token=...`, so scanning it with any phone camera
 * opens this route. If the employee is not signed in, `RequireAuth` sends them to the login screen
 * carrying this full location (path + token) as `state.from`, and login returns them here — so the
 * token survives the round-trip and the check-in continues automatically after sign-in.
 *
 * On mount, with a token present, it submits the scan straight away (no second scan, no button): the
 * whole point is that one physical scan checks the employee in. The outcome reuses the same flow the
 * mobile home uses — a success confirms with site/action/time; a 409 open_shift_elsewhere opens the
 * conflict dialog (transition / end-and-move / cancel); an offline or dropped request states plainly
 * that nothing was recorded and never a false success. `onDone` returns to the mobile home.
 *
 * A missing or malformed token (someone opened /m/scan by hand) is shown as a plain error rather than
 * a silent no-op, so the employee knows the code did not read.
 */

import { useEffect, useRef } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate, useSearchParams } from 'react-router-dom';

import {
  endAndMove as endAndMoveRequest,
  recordScan,
  scanKeys,
  transition as transitionRequest,
} from '@/api/scans';
import type { ScanResult } from '@/api/types';
import { ConfirmationPanel } from '@/app/mobile/ConfirmationPanel';
import { ConflictDialog } from '@/app/mobile/ConflictDialog';
import {
  confirming,
  failureToState,
  offline as offlineState,
  submitting,
  type ScanFlowState,
} from '@/app/mobile/scanFlow';
import { HindiHelper } from '@/components/HindiHelper';
import { useOnline } from '@/lib/useOnline';
import { useState } from 'react';

export function MobileScanLandingPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const online = useOnline();
  const queryClient = useQueryClient();
  const [params] = useSearchParams();
  const token = params.get('token') ?? '';

  const [flow, setFlow] = useState<ScanFlowState>(submitting);
  // Auto-submit exactly once on mount; a re-render must not fire a second scan.
  const submitted = useRef(false);

  const settle = (result: ScanResult) => {
    setFlow(confirming(result));
    void queryClient.invalidateQueries({ queryKey: scanKeys.status });
    void queryClient.invalidateQueries({ queryKey: scanKeys.history });
  };

  const scanMutation = useMutation({
    mutationFn: (qrToken: string) => recordScan({ qr_token: qrToken }),
    onSuccess: (result) => settle(result),
    onError: (error, qrToken) => setFlow(failureToState(error, qrToken)),
  });

  const transitionMutation = useMutation({
    mutationFn: (qrToken: string) => transitionRequest({ qr_token: qrToken }),
    onError: (error, qrToken) => setFlow(failureToState(error, qrToken)),
  });

  const endAndMoveMutation = useMutation({
    mutationFn: () => endAndMoveRequest(),
    onSuccess: (result) => settle(result),
    onError: (error) => setFlow(failureToState(error, '')),
  });

  const busy = flow.kind === 'submitting' || transitionMutation.isPending || endAndMoveMutation.isPending;

  useEffect(() => {
    if (submitted.current) {
      return;
    }
    submitted.current = true;
    if (!token) {
      setFlow({ kind: 'error', code: 'invalid_qr_token' });
      return;
    }
    if (!online) {
      setFlow(offlineState);
      return;
    }
    setFlow(submitting);
    scanMutation.mutate(token);
    // Run once on mount; the mutation and token are stable for this landing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const done = () => navigate('/m', { replace: true });

  return (
    <section className="mobile-home">
      <p className="mobile-home__greeting">
        {t('scanLanding.title')}
        <HindiHelper textKey="scanLanding.title" />
      </p>

      {flow.kind === 'submitting' ? (
        <p className="subtitle">
          {t('scan.submitting')}
          <HindiHelper textKey="scan.submitting" />
        </p>
      ) : null}

      {flow.kind === 'confirming' ? (
        <ConfirmationPanel result={flow.result} siteName={null} onDone={done} />
      ) : null}

      {flow.kind === 'conflict' ? (
        <ConflictDialog
          conflict={flow.conflict}
          busy={busy}
          onTransition={() =>
            transitionMutation.mutate(flow.conflict.pendingToken, {
              onSuccess: (result) => settle(result),
            })
          }
          onEndAndMove={() => endAndMoveMutation.mutate()}
          onCancel={done}
        />
      ) : null}

      {flow.kind === 'offline' ? (
        <div className="feedback feedback--error" role="alert">
          <p>
            {t('scan.notRecorded')}
            <HindiHelper textKey="scan.notRecorded" />
          </p>
          <button type="button" className="button button--large" onClick={done}>
            {t('common.close')}
          </button>
        </div>
      ) : null}

      {flow.kind === 'error' ? (
        <div className="feedback feedback--error" role="alert">
          <p>
            {t([`apiError.${flow.code ?? 'unknown'}`, 'apiError.unknown'])}
            <HindiHelper textKey={[`apiError.${flow.code ?? 'unknown'}`, 'apiError.unknown']} />
          </p>
          <button type="button" className="button button--large" onClick={done}>
            {t('common.close')}
          </button>
        </div>
      ) : null}
    </section>
  );
}