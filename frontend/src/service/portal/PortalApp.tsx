// The portal route table (design "Routes"; R6.2, R8.2, R8.3, R8.10).
//
//   Router: BrowserRouter basename="/service"        (all paths below are relative to /service)
//
//   path                      screen                   guard
//   ------------------------  -----------------------  ----------------------------------------------
//   /                         ticket list              FlagGate -> PortalSessionGate
//   /new                      ticket form              FlagGate -> PortalSessionGate
//   /new/done                 confirmation             FlagGate -> PortalSessionGate
//   /tickets/:ticketNumber    ticket detail            FlagGate -> PortalSessionGate
//   /accessibility            accessibility statement  FlagGate only (opens without a token, R8.2)
//   *                         not found, no ticket     FlagGate -> PortalSessionGate
//                             data
//
// There is no `/sign-in` or `/change-password` route. While there is no valid token the gate renders the
// sign-in screen in place of ANY gated path, and while `must_change_password` is set it renders the
// change-password screen in place of any gated path; the URL does not change, so a bookmarked path
// survives both (R6.2). `/new/done` is not in the design's route table; it is the one addition, so the
// confirmation survives a reload without a second submit.
//
// Behind the session gate `GatedLayout` renders the company branding (logo and name) once, above the
// routed screen, so every signed-in screen shows it (R28.3). The sign-in screen does not (no token).
//
// `FlagGate` wraps everything, outside the session gate, so with the flag off or unknown no sign-in
// screen can render (R8.2, R21.5). The two live regions are above `FlagGate`. With the flag off, the gate
// shows `PortalNotAvailable` (same text as the shared page, but with the portal `PageHeading`).
import { Outlet, Route, Routes } from 'react-router-dom';
import { FlagGate } from '../gates/FlagGate';
import { PortalAnnouncerProvider } from './Announcer';
import { PortalBranding } from './PortalBranding';
import { PortalNotAvailable } from './PortalNotAvailable';
import { PortalSessionGate } from './PortalSessionGate';
import { PortalShell } from './PortalShell';
import { AccessibilityStatementScreen } from './screens/AccessibilityStatementScreen';
import { ConfirmationScreen } from './screens/ConfirmationScreen';
import { NotFoundScreen } from './screens/NotFoundScreen';
import { TicketDetailScreen } from './screens/TicketDetailScreen';
import { TicketFormScreen } from './screens/TicketFormScreen';
import { TicketListScreen } from './screens/TicketListScreen';

/**
 * Layout route: every child renders behind the session gate, under the company branding (R28.3). The
 * branding is a child of the gate, so it shows only for a signed-in customer and once per screen.
 */
export function GatedLayout() {
  return (
    <PortalSessionGate>
      <PortalBranding />
      <Outlet />
    </PortalSessionGate>
  );
}

export function PortalRoutes() {
  return (
    <Routes>
      <Route element={<PortalShell />}>
        <Route path="accessibility" element={<AccessibilityStatementScreen />} />
        <Route element={<GatedLayout />}>
          <Route index element={<TicketListScreen />} />
          <Route path="new" element={<TicketFormScreen />} />
          <Route path="new/done" element={<ConfirmationScreen />} />
          <Route path="tickets/:ticketNumber" element={<TicketDetailScreen />} />
          <Route path="*" element={<NotFoundScreen />} />
        </Route>
      </Route>
    </Routes>
  );
}

/** Everything inside the providers that `mountPortal` (or a test) supplies. */
export function PortalApp() {
  return (
    <PortalAnnouncerProvider>
      <FlagGate notAvailable={<PortalNotAvailable />}>
        <PortalRoutes />
      </FlagGate>
    </PortalAnnouncerProvider>
  );
}
