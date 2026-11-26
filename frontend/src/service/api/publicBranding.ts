// The two anonymous branding reads behind the header of every portal screen (task 30.17; R28.3, R28.4,
// R28.5): `GET /service/public/branding` (company name and whether a logo exists) and
// `GET /service/public/branding/logo` (the PNG).
//
// These use a plain `fetch` and NOT `portalFetch`, on purpose. They carry no Authorization header, and
// `portalFetch` applies the 401 rule (clear the token, show the session-expired notice), which must never
// happen because a public read failed. Every failure is a `PortalApiError` that the header's query keeps
// in its error state and ignores: a 401, 403, 404, 429 or 503, a network error, a timeout and a body that
// is not usable all leave the screen exactly as it would be without a header.
import { GENERIC_CODE, NETWORK_CODE, PORTAL_READ_TIMEOUT_MS, PortalApiError, TIMEOUT_CODE, portalUrl } from './portalFetch';
import { parseBranding, type FormBranding } from './formTypes';

/** Under the `['portal']` root. Signing out drops the root, which refetches these two: acceptable. */
export const publicBrandingKey = ['portal', 'public-branding'] as const;
export const publicBrandingLogoKey = ['portal', 'public-branding-logo'] as const;

/** Both reads are cached for five minutes, never retried, and not refetched on window focus. */
export const PUBLIC_BRANDING_QUERY_OPTIONS = {
  retry: false,
  refetchOnWindowFocus: false,
  staleTime: 5 * 60_000,
} as const;

/** A GET with no credentials, no Authorization header and the read timeout. Throws `PortalApiError` on any failure. */
async function publicGet(path: string, accept: string, signal: AbortSignal | undefined): Promise<Response> {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, PORTAL_READ_TIMEOUT_MS);
  const onCallerAbort = (): void => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', onCallerAbort, { once: true });
  }
  try {
    const response = await fetch(portalUrl(path), {
      method: 'GET',
      headers: { Accept: accept },
      credentials: 'omit',
      cache: 'default',
      signal: controller.signal,
    });
    if (!response.ok) throw new PortalApiError(response.status, GENERIC_CODE, {}, response.headers.get('Retry-After'));
    return response;
  } catch (caught) {
    if (caught instanceof PortalApiError) throw caught;
    throw new PortalApiError(0, timedOut ? TIMEOUT_CODE : NETWORK_CODE);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onCallerAbort);
  }
}

/** `GET /service/public/branding`: the company name and whether there is a logo. No token. */
export async function getPublicBranding(signal?: AbortSignal): Promise<FormBranding> {
  const response = await publicGet('/public/branding', 'application/json', signal);
  let body: unknown;
  try {
    body = (await response.json()) as unknown;
  } catch {
    throw new PortalApiError(response.status, GENERIC_CODE);
  }
  return parseBranding(body);
}

/**
 * `GET /service/public/branding/logo` as a Blob. Ask for it only when the branding says `has_logo`; the
 * server answers 404 with an empty body otherwise. A body that is empty or is not an image is the
 * `generic` error, so a proxy page is never handed to `URL.createObjectURL`.
 */
export async function getPublicBrandingLogo(signal?: AbortSignal): Promise<Blob> {
  const response = await publicGet('/public/branding/logo', 'image/*', signal);
  let blob: Blob;
  try {
    blob = await response.blob();
  } catch {
    throw new PortalApiError(response.status, GENERIC_CODE);
  }
  if (blob.size === 0 || !blob.type.toLowerCase().startsWith('image/')) throw new PortalApiError(200, GENERIC_CODE);
  return blob;
}
