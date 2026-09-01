/**
 * The camera QR scanner (Requirement 23.8, 8.x on the read side).
 *
 * It opens the rear camera through `@zxing/browser`, previews it, and reports the first decoded token
 * to its parent. The whole flow asks for one permission and one only — the camera — and never touches
 * `navigator.geolocation`; the design and Requirement 23.8/20.10 forbid a location prompt, and the
 * absence is verified against the built bundle.
 *
 * Permission and failure are first-class states, not afterthoughts. A denied or absent camera, or a
 * device with none, lands on a plain message and a retry rather than a blank frame, which is the
 * "clear failure path" the task asks for. The reader's per-frame callback reports a not-found on most
 * frames while it hunts for a code; those are swallowed, and only a genuine open/permission failure
 * becomes the error state.
 *
 * The scanner stops its camera track on unmount and the moment it hands a token up, so the light goes
 * out as soon as the work is done rather than lingering behind the confirmation panel.
 */

import { BrowserQRCodeReader, type IScannerControls } from '@zxing/browser';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

interface QrScannerProps {
  /** Called with the decoded token text the first time a code is read. */
  onDecoded: (token: string) => void;
  /** Called when the employee dismisses the scanner without a scan. */
  onCancel: () => void;
}

type Phase = 'starting' | 'scanning' | 'failed';

export function QrScanner({ onDecoded, onCancel }: QrScannerProps) {
  const { t } = useTranslation();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [phase, setPhase] = useState<Phase>('starting');
  // Bumped by "retry", which re-runs the camera effect after a failure rather than leaving a dead frame.
  const [attempt, setAttempt] = useState(0);
  // A ref so the per-frame callback fires the token up exactly once, before React has re-rendered.
  const decodedRef = useRef(false);

  useEffect(() => {
    let controls: IScannerControls | null = null;
    let cancelled = false;
    decodedRef.current = false;

    const reader = new BrowserQRCodeReader();

    // `facingMode: environment` asks for the rear camera by constraint, so no device is enumerated
    // and no permission beyond the camera is touched. There is no geolocation call anywhere here.
    reader
      .decodeFromConstraints({ video: { facingMode: 'environment' } }, videoRef.current ?? undefined, (result) => {
        if (cancelled || decodedRef.current || !result) {
          return;
        }
        const text = result.getText();
        if (text) {
          decodedRef.current = true;
          controls?.stop();
          onDecoded(text);
        }
      })
      .then((activeControls) => {
        if (cancelled) {
          activeControls.stop();
          return;
        }
        controls = activeControls;
        setPhase('scanning');
      })
      .catch(() => {
        // A denied permission, a device with no camera, or an insecure context: all land here, and
        // all get the same plain "camera unavailable" message and a retry. We never fall through to
        // a location prompt as an alternative — the camera is the only path.
        if (!cancelled) {
          setPhase('failed');
        }
      });

    return () => {
      cancelled = true;
      controls?.stop();
    };
    // `attempt` is the only trigger: mount, and each retry after a failure. Re-running for any other
    // reason would needlessly restart the camera. `onDecoded` is intentionally not a dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt]);

  return (
    <div className="scanner" role="dialog" aria-modal="true" aria-label={t('scan.title')}>
      <div className="scanner__frame">
        <video ref={videoRef} className="scanner__video" muted playsInline aria-label={t('scan.cameraLabel')} />
        {phase === 'scanning' ? <div className="scanner__reticle" aria-hidden="true" /> : null}
      </div>

      {phase === 'starting' ? <p className="scanner__hint">{t('scan.starting')}</p> : null}
      {phase === 'scanning' ? <p className="scanner__hint">{t('scan.aim')}</p> : null}
      {phase === 'failed' ? (
        <div className="feedback feedback--error" role="alert">
          <p className="scanner__hint">{t('scan.cameraFailed')}</p>
        </div>
      ) : null}

      <div className="scanner__actions">
        {phase === 'failed' ? (
          <button
            type="button"
            className="button button--primary button--large"
            onClick={() => {
              setPhase('starting');
              setAttempt((n) => n + 1);
            }}
          >
            {t('scan.retry')}
          </button>
        ) : null}
        <button type="button" className="button button--large" onClick={onCancel}>
          {t('common.cancel')}
        </button>
      </div>
    </div>
  );
}
