/**
 * The technician notice (Requirement 21.7): while the Feature_Flag is off (or could not be read), a
 * signed-in Technician sees one message that the ticket system is unavailable and one control, the
 * sign-out button, with no redirect. Sign-out is the staff mechanism (`useAuth().signOut`).
 *
 * The text is Hebrew and English together, from constants, because the staff i18n instance holds no
 * ticket strings and the `service` namespace is not loaded here.
 */

import { useAuth } from '@/auth/AuthProvider';
import { TicketsUnavailableView } from './TicketsUnavailableView';

export function TicketsUnavailable() {
  const { signOut } = useAuth();
  return (
    <TicketsUnavailableView
      onSignOut={() => {
        void signOut();
      }}
    />
  );
}
