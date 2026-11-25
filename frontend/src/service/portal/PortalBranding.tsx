// The company logo and name above the page content of every signed-in portal screen (task 30.15; R28.3,
// R28.4, R28.7). `GatedLayout` renders it once, inside the session gate and above the routed screen, so
// it shows on the ticket list, the new-ticket form, the confirmation, the ticket detail and the
// not-found screen, and never twice on `/new`.
//
// It reads the same two queries the form uses (`formConfigKey` and `brandingLogoKey`), so the form
// configuration and the logo are requested once and come from the cache when the customer moves between
// screens. It is inside the gate, so it never renders without a token (the sign-in screen has none, and the
// logo endpoint needs a customer token) and not while the password change is forced (the API answers
// `password_change_required` to the form configuration until the password is changed).
//
// Nothing here can block or break a screen: a failed form configuration or logo request leaves the query
// in its error state, which is ignored, and nothing is rendered for the part that is missing.
import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { brandingLogoKey, formConfigKey, getBrandingLogo, getFormConfig } from '../api/ticketCreate';
import type { FormBranding } from '../api/formTypes';
import { BrandingHeader } from './screens/BrandingHeader';
import { brandingView } from './screens/settlementModel';

/** The form configuration. The ticket form uses the same hook, so the two share one request and one cache entry. */
export function useFormConfig() {
  return useQuery({ queryKey: formConfigKey, queryFn: ({ signal }) => getFormConfig(signal) });
}

/**
 * An object URL for the logo, fetched with the bearer header only when the configuration says there is
 * one. Revoked when the logo changes and on unmount. Null until it exists and after a failed fetch: a logo
 * that cannot be loaded is simply not shown, and no screen is blocked by it (R28.3).
 */
export function useBrandingLogoUrl(hasLogo: boolean): string | null {
  const logo = useQuery({
    queryKey: brandingLogoKey,
    queryFn: ({ signal }) => getBrandingLogo(signal),
    enabled: hasLogo,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 5 * 60_000,
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
  const config = useFormConfig();
  const branding = config.data?.branding;
  const logoUrl = useBrandingLogoUrl(branding?.has_logo === true);
  return <PortalBrandingView branding={branding} logoUrl={logoUrl} />;
}
