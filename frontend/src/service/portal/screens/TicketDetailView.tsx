// The presentational parts of the ticket detail screen (R12.2, R12.6, R12.8, R12.11, R13.5, R23.5, R23.6).
// They take everything as props and fetch nothing, so a markup test can render every state. The
// container `TicketDetailScreen` owns the queries, the cancellation request and the focus.
//
// Nothing here changes focus or scrolls: a reload only changes the text inside stable elements.
import type { Ref } from 'react';
import { Link } from 'react-router-dom';
import type { CustomerTicketDetail, FormLabels } from '../../api/ticketDetail';
import { PageHeading } from '../PageHeading';
import { directionOf, usePortalTranslation, type PortalLanguage } from '../portalI18n';
import {
  URGENT_ICON,
  buildDetailRows,
  canCancel,
  categoryName,
  fieldLabel,
  ltrIsolate,
  type PhotoView,
  type RowValue,
} from './ticketDetailModel';

const PHOTO_HEADING_ID = 'ticket-detail-photo-heading';

function RowValueView({ value }: { value: RowValue }) {
  switch (value.kind) {
    case 'ltr':
      return <bdi dir="ltr">{value.text}</bdi>;
    case 'description':
      // Text only, never markup: React escapes it and `white-space: pre-wrap` keeps the line breaks.
      return <span className="ticket-detail-description">{value.text}</span>;
    case 'urgent':
      return (
        <span className="ticket-detail-urgent">
          <span aria-hidden="true" className="ticket-detail-urgent-icon">
            {URGENT_ICON}
          </span>
          <span>{value.text}</span>
        </span>
      );
    case 'plain':
      return <>{value.text}</>;
  }
}

/** One or more message lines of a notice. */
function Messages({ keys }: { keys: readonly string[] }) {
  const { t } = usePortalTranslation();
  return (
    <>
      {keys.map((key) => (
        <p key={key}>{t(key)}</p>
      ))}
    </>
  );
}

export interface TicketDetailViewProps {
  ticket: CustomerTicketDetail;
  language: PortalLanguage;
  /** The Field_Labels of `GET /form-config`; undefined until they load or when that call failed. */
  labels: FormLabels | undefined;
  photo: PhotoView;
  /** Set when a reload failed: the ticket stays shown with this error and a retry control (R12.8). */
  staleErrorKeys: readonly string[] | null;
  /** Set when a cancellation failed (R13.9). */
  actionErrorKeys: readonly string[] | null;
  headingRef?: Ref<HTMLHeadingElement>;
  cancelRef?: Ref<HTMLButtonElement>;
  /** The refresh control and the retry control of a failed reload. */
  onRefresh: () => void;
  onRetryPhoto: () => void;
  /** Opens the confirmation. It never sends a request (R13.2). */
  onCancel: () => void;
}

export function TicketDetailView({
  ticket,
  language,
  labels,
  photo,
  staleErrorKeys,
  actionErrorKeys,
  headingRef,
  cancelRef,
  onRefresh,
  onRetryPhoto,
  onCancel,
}: TicketDetailViewProps) {
  const { t } = usePortalTranslation();
  const translate = (key: string): string => t(key);
  const rows = buildDetailRows(ticket, language, labels, translate);

  return (
    <section className="ticket-detail" lang={language} dir={directionOf(language)}>
      <PageHeading pageKey="detail" params={{ number: String(ticket.ticket_number) }} headingRef={headingRef}>
        {t('portal.pageTitle.detail', { number: ltrIsolate(String(ticket.ticket_number)) })}
      </PageHeading>

      <div className="ticket-detail-toolbar">
        <Link to="/" className="portal-button portal-button--quiet">
          {t('portal.detail.back')}
        </Link>
        <button type="button" className="portal-button portal-button--quiet" onClick={onRefresh}>
          {t('portal.detail.refresh')}
        </button>
      </div>

      {staleErrorKeys !== null && (
        <div className="portal-notice portal-notice--error">
          <Messages keys={staleErrorKeys} />
          <button type="button" className="portal-button" onClick={onRefresh}>
            {t('portal.detail.retry')}
          </button>
        </div>
      )}

      {actionErrorKeys !== null && (
        <div className="portal-notice portal-notice--error">
          <Messages keys={actionErrorKeys} />
        </div>
      )}

      <dl className="ticket-detail-fields">
        {rows.map((row) => (
          <div className="ticket-detail-row" key={row.key}>
            <dt>{row.label}</dt>
            <dd>
              <RowValueView value={row.value} />
            </dd>
          </div>
        ))}
      </dl>

      {photo.kind !== 'none' && (
        <section className="ticket-detail-photo-section" aria-labelledby={PHOTO_HEADING_ID}>
          <h2 id={PHOTO_HEADING_ID}>{fieldLabel('photo', labels, language, translate)}</h2>
          {photo.kind === 'loading' && <p>{t('portal.detail.photoLoading')}</p>}
          {photo.kind === 'error' && (
            <div className="portal-notice portal-notice--error">
              <Messages keys={photo.messageKeys} />
              {photo.retryable && (
                <button type="button" className="portal-button" onClick={onRetryPhoto}>
                  {t('portal.detail.retry')}
                </button>
              )}
            </div>
          )}
          {photo.kind === 'ready' && (
            <img
              className="ticket-detail-photo"
              src={photo.url}
              alt={t('portal.detail.photoAlt', {
                number: String(ticket.ticket_number),
                category: categoryName(ticket.category, language),
              })}
              decoding="async"
            />
          )}
        </section>
      )}

      {canCancel(ticket.status) && (
        <p className="ticket-detail-actions">
          <button
            type="button"
            ref={cancelRef}
            className="portal-button portal-button--danger"
            aria-haspopup="dialog"
            onClick={onCancel}
          >
            {t('portal.detail.cancelTicket')}
          </button>
        </p>
      )}
    </section>
  );
}

export interface TicketDetailMessageProps {
  /** The number from the address, for the heading. */
  ticketNumber: number;
  language: PortalLanguage;
  /** `loading` shows the loading text; `error` shows `messageKeys` with a retry control and no data. */
  state: { kind: 'loading' } | { kind: 'error'; messageKeys: readonly string[] };
  headingRef?: Ref<HTMLHeadingElement>;
  onRetry: () => void;
}

/** The two states in which no ticket data exists yet: loading, and a failed first load (R12.8). */
export function TicketDetailMessage({ ticketNumber, language, state, headingRef, onRetry }: TicketDetailMessageProps) {
  const { t } = usePortalTranslation();
  return (
    <section className="ticket-detail" lang={language} dir={directionOf(language)} aria-busy={state.kind === 'loading'}>
      <PageHeading pageKey="detail" params={{ number: String(ticketNumber) }} headingRef={headingRef}>
        {t('portal.pageTitle.detail', { number: ltrIsolate(String(ticketNumber)) })}
      </PageHeading>
      <div className="ticket-detail-toolbar">
        <Link to="/" className="portal-button portal-button--quiet">
          {t('portal.detail.back')}
        </Link>
      </div>
      {state.kind === 'loading' ? (
        <p>{t('portal.loading')}</p>
      ) : (
        <div className="portal-notice portal-notice--error">
          <Messages keys={state.messageKeys} />
          <button type="button" className="portal-button" onClick={onRetry}>
            {t('portal.detail.retry')}
          </button>
        </div>
      )}
    </section>
  );
}

export interface TicketNotFoundPanelProps {
  language: PortalLanguage;
  headingRef?: Ref<HTMLHeadingElement>;
}

/** R12.11: the ticket-not-found message and a control back to the list. It carries no ticket data. */
export function TicketNotFoundPanel({ language, headingRef }: TicketNotFoundPanelProps) {
  const { t } = usePortalTranslation();
  return (
    <section className="ticket-detail" lang={language} dir={directionOf(language)}>
      <PageHeading pageKey="notFound" headingRef={headingRef}>
        {t('portal.detail.notFound.title')}
      </PageHeading>
      <p>{t('portal.detail.notFound.body')}</p>
      <p>
        <Link to="/" className="portal-button">
          {t('portal.detail.notFound.back')}
        </Link>
      </p>
    </section>
  );
}
