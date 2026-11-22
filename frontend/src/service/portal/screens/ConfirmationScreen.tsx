// The confirmation after a ticket is created (task 23.3; R9.10, R23.14; route `/new/done`).
//
//   ConfirmationScreen  container: reads the ticket number from the router state.
//   ConfirmationView    presentational: the number in `<bdi dir="ltr">` and a link to the list.
//
// The number arrives in the router state. The browser keeps that state across a reload, so a reload shows
// the same confirmation and sends nothing (the route submits nothing). A visit with no state (a bookmark,
// a typed address) shows a neutral page that points to the list, never a number the customer did not just
// receive. The announcement of the new ticket is written by the form when the ticket is created.
import type { Ref } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { directionOf, getLanguage, usePortalTranslation } from '../portalI18n';
import { usePageChrome } from '../usePageChrome';
import { splitAroundNumber, ticketNumberFromState } from './ticketFormModel';
import './ticketForm.css';

export interface ConfirmationViewProps {
  /** The new ticket's number, or null for the neutral page. */
  readonly ticketNumber: number | null;
  readonly headingRef: Ref<HTMLHeadingElement> | null;
}

export function ConfirmationView({ ticketNumber, headingRef }: ConfirmationViewProps) {
  const { t, i18n } = usePortalTranslation();
  const language = getLanguage(i18n);
  const sentence =
    ticketNumber === null ? null : splitAroundNumber((slot) => String(t('portal.confirmation.message', { number: slot })));
  return (
    <section className="portal-confirmation" lang={language} dir={directionOf(language)}>
      <h1 ref={headingRef} tabIndex={-1}>
        {ticketNumber === null ? t('portal.confirmation.neutralTitle') : t('portal.confirmation.title')}
      </h1>
      <p className="portal-notice portal-notice--success">
        {sentence === null ? (
          t('portal.confirmation.neutralMessage')
        ) : (
          <>
            {sentence.before}
            <bdi dir="ltr">{ticketNumber}</bdi>
            {sentence.after}
          </>
        )}
      </p>
      <p className="portal-confirmation-actions">
        <Link to="/" className="portal-button">
          {t('portal.confirmation.toList')}
        </Link>
      </p>
    </section>
  );
}

/** Route `/new/done`. */
export function ConfirmationScreen() {
  const location = useLocation();
  const ticketNumber = ticketNumberFromState(location.state);
  const headingRef = usePageChrome(ticketNumber === null ? 'list' : 'confirmation');
  return <ConfirmationView ticketNumber={ticketNumber} headingRef={headingRef} />;
}
