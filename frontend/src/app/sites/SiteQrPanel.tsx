/**
 * The site's printable QR code (Requirement 8.5, 8.6, 8.7).
 *
 * Downloads the code an employee scans at the site, as a PNG (an image to drop into a document) or a
 * PDF (a ready-to-print page carrying the site name and number). A unified site has one code; a
 * separate-mode site has two — a check-in code and a check-out code — so this offers each.
 *
 * Regeneration is administrator-only and rotates the token: every previously printed code stops
 * working on the next scan. It is therefore behind a confirmation, because it invalidates codes that
 * may already be posted in the field. After a rotation the freshly downloaded file replaces the old
 * print.
 *
 * The download goes through `apiFetchBlob` (the file needs the bearer token, so it cannot be a plain
 * link), then a temporary object URL drives the browser's download. The URL is revoked immediately
 * after, so nothing leaks.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { downloadSiteQr, regenerateSiteQr, siteKeys, type QrAction } from '@/api/sites';
import type { SiteResponse } from '@/api/types';
import { toError } from '@/lib/apiError';
import { triggerBrowserDownload } from '@/lib/download';

export function SiteQrPanel({
  site,
  canManage,
}: {
  site: SiteResponse;
  canManage: boolean;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  const separate = site.qr_mode === 'separate';

  const download = useMutation({
    mutationFn: ({ format, action }: { format: 'png' | 'pdf'; action?: QrAction }) =>
      downloadSiteQr(site.id, format, action),
    onSuccess: (blob, variables) => {
      const suffix = variables.action ? `-${variables.action}` : '';
      triggerBrowserDownload(blob, `site-${site.site_number}${suffix}.${variables.format}`);
      setErrorKey(null);
    },
    onError: (error) => setErrorKey(toError(error).key),
  });

  const regenerate = useMutation({
    mutationFn: () => regenerateSiteQr(site.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: siteKeys.detail(site.id) });
      setConfirming(false);
      setErrorKey(null);
    },
    onError: (error) => setErrorKey(toError(error).key),
  });

  const downloadRow = (action?: QrAction) => {
    const start = (format: 'png' | 'pdf') => {
      setErrorKey(null);
      download.mutate(action ? { format, action } : { format });
    };
    return (
      <div className="inline-actions">
        <button
          type="button"
          className="button button--small button--primary"
          disabled={download.isPending}
          onClick={() => start('pdf')}
        >
          {t('siteQr.downloadPdf')}
        </button>
        <button
          type="button"
          className="button button--small"
          disabled={download.isPending}
          onClick={() => start('png')}
        >
          {t('siteQr.downloadPng')}
        </button>
      </div>
    );
  };

  return (
    <div className="drawer__section">
      <p className="subtitle">{t('siteQr.intro')}</p>

      {separate ? (
        <>
          <div className="card" style={{ marginBlockEnd: 'var(--space)' }}>
            <h4 className="detail__label">{t('siteQr.checkIn')}</h4>
            {downloadRow('check_in')}
          </div>
          <div className="card" style={{ marginBlockEnd: 'var(--space)' }}>
            <h4 className="detail__label">{t('siteQr.checkOut')}</h4>
            {downloadRow('check_out')}
          </div>
        </>
      ) : (
        downloadRow()
      )}

      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey)}
        </p>
      ) : null}

      {canManage ? (
        <div className="drawer__section" style={{ marginBlockStart: 'calc(var(--space) * 2)' }}>
          <p className="subtitle">{t('siteQr.regenerateHint')}</p>
          {confirming ? (
            <div className="inline-actions">
              <button
                type="button"
                className="button button--small button--danger"
                disabled={regenerate.isPending}
                onClick={() => {
                  setErrorKey(null);
                  regenerate.mutate();
                }}
              >
                {regenerate.isPending ? t('common.saving') : t('siteQr.regenerateConfirm')}
              </button>
              <button
                type="button"
                className="button button--small"
                onClick={() => setConfirming(false)}
              >
                {t('common.cancel')}
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="button button--small button--danger"
              onClick={() => setConfirming(true)}
            >
              {t('siteQr.regenerate')}
            </button>
          )}
        </div>
      ) : null}
    </div>
  );
}