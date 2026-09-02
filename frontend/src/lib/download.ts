/**
 * Trigger a browser download of an in-memory blob.
 *
 * A temporary object URL drives a synthetic anchor click, then the URL is revoked immediately so
 * nothing leaks. Shared so every in-browser export — a site's printable QR, a report's CSV — drives
 * the download the same way, with no backend round trip and no object storage.
 */
export function triggerBrowserDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
