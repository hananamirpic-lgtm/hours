// The company logo and name above the page content of every signed-in portal screen (tasks 30.8 and
// 30.15; R28.3, R28.4, R28.7). Presentational: `PortalBranding` fetches the logo with the bearer header
// and passes the object URL. It renders nothing at all when there is neither a name nor a logo, so no
// element, no placeholder and no space is left behind. The name is a text node, so markup typed into it
// is shown literally.
import type { BrandingView } from './settlementModel';
import './branding.css';

export function BrandingHeader({ view }: { view: BrandingView | null }) {
  if (view === null) return null;
  return (
    <div className="portal-branding">
      {view.logoUrl !== null && <img className="portal-branding-logo" src={view.logoUrl} alt={view.logoAlt} />}
      {view.name !== null && (
        <p className="portal-branding-name" dir="auto">
          {view.name}
        </p>
      )}
    </div>
  );
}
