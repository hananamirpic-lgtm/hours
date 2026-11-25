// The company logo and name above the page content of every portal screen (tasks 30.15 and 30.17; R28.3,
// R28.4, R28.7). `PortalShell` renders it once, inside `<main>` and above the routed screen, so it shows on
// the sign-in screen, the forced change-password screen, the ticket list, the new-ticket form, the
// confirmation, the ticket detail, the accessibility statement and the not-found screen, and never twice.
//
// It is fed only by the two anonymous endpoints (`/public/branding` and `/public/branding/logo`), so it
// needs no customer token and does not depend on the form configuration or on the session gate. The
// `branding` member of the form configuration is left alone; the header no longer reads it.
//
// Nothing here can block or break a screen: a failed request (401, 403, 429, 503, a network error, a body
// that is not an image) leaves the query in its error state, which is ignored, and nothing is rendered for
// the part that is missing. The requests do not go through `portalFetch`, so a failure never signs the
// customer out or redirects.
import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import {
  PUBLIC_BRANDING_QUERY_OPTIONS,
  getPublicBranding,
  getPublicBrandingLogo,
  publicBrandingKey,
  publicBrandingLogoKey,
} from '../api/publicBranding';
import { formConfigKey, getFormConfig } from '../api/ticketCreate';
import type { FormBranding } from '../api/formTypes';
import { BrandingHeader } from './screens/BrandingHeader';
import { brandingView } from './screens/settlementModel';

/** The form configuration. Only the ticket form uses it now; the branding header does not. */
export function useFormConfig() {
  return useQuery({ queryKey: formConfigKey, queryFn: ({ signal }) => getFormConfig(signal) });
}

/** The public branding answer: the company name and whether a logo exists. Undefined until it arrives or when it failed. */
export function usePublicBranding(): FormBranding | undefined {
  const query = useQuery({
    queryKey: publicBrandingKey,
    queryFn: ({ signal }) => getPublicBranding(signal),
    ...PUBLIC_BRANDING_QUERY_OPTIONS,
  });
  return query.data;
}

/**
 * An object URL for the logo, fetched without a token only when the branding says there is one. Revoked
 * when the logo changes and on unmount. Null until it exists and after a failed fetch: a logo that cannot
 * be loaded is simply not shown, and no screen is blocked by it (R28.3).
 */
export function useBrandingLogoUrl(hasLogo: boolean): string | null {
  const logo = useQuery({
    queryKey: publicBrandingLogoKey,
    queryFn: ({ signal }) => getPublicBrandingLogo(signal),
    enabled: hasLogo,
    ...PUBLIC_BRANDING_QUERY_OPTIONS,
  });
  const blob = hasLogo && logo.data !== undefined ? logo.data : null;
  const [object, setObject] = useState<{ blob: Blob; url: string } | null>(null);
  useEffect(() => {
    if (blob === null || typeof URL.createObjectURL !== 'function') {
      setObject(null);
      return;
    }
    const url = URL.createObjectURL(blob);
    setObject({ blob, url });
    return () => {
      URL.revokeObjectURL(url);
    };
  }, [blob]);
  return object !== null && object.blob === blob ? object.url : null;
}

/** The header for a given branding answer and logo URL: nothing at all when there is neither name nor logo. */
export function PortalBrandingView({ branding, logoUrl }: { branding: FormBranding | null | undefined; logoUrl: string | null }) {
  return <BrandingHeader view={brandingView(branding, logoUrl)} />;
}

export function PortalBranding() {
  const branding = usePublicBranding();
  const logoUrl = useBrandingLogoUrl(branding?.has_logo === true);
  return <PortalBrandingView branding={branding} logoUrl={logoUrl} />;
}
