// Route `/tickets/:ticketNumber` (task 23.4; R12.2, R12.5, R12.8, R12.10, R12.11, R13.2, R13.3, R13.5, R13.9,
// R19.8, R23.3, R23.6, R23.14, R23.15).
//
// Decisions worth knowing:
//  - The cancellation dialog's open state is a reducer in this component, not part of the query data, and
//    `<CancelDialog>` sits at a fixed position next to the content. A reload replaces `data`; it never
//    changes `phase` and never moves the dialog in the tree, so the dialog is not remounted and keeps its
//    focus (R12.5).
//  - The photo is fetched with the bearer header as a Blob, shown through `URL.createObjectURL`, and the
//    URL is revoked when the blob changes and on unmount. It is fetched whenever `has_photo` is set, and
//    the Photo upload setting is not read here (R12.2).
//  - Polling is the list's: 30 s after the previous load, not in a hidden tab, immediately when the tab
//    becomes visible. A reload changes no focus and no scroll: text is replaced inside stable elements.
import { keepPreviousData, skipToken, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useReducer, useRef, useState, type ReactNode } from 'react';
import { useParams } from 'react-router-dom';
import {
  cancelTicket,
  getFormLabels,
  getTicket,
  getTicketPhoto,
  ticketDetailKeys,
} from '../../api/ticketDetail';
import type { TicketStatusEntry } from '../../types';
import { useAnnouncer } from '../Announcer';
import { resolveLanguage, usePortalTranslation } from '../portalI18n';
import { CancelDialog } from './CancelDialog';
import { TicketDetailMessage, TicketDetailView, TicketNotFoundPanel } from './TicketDetailView';
import {
  cancelFailureOf,
  cancelFlowReducer,
  parseTicketNumber,
  photoView,
  screenState,
  statusAnnouncement,
} from './ticketDetailModel';
import './ticketDetail.css';

/** The reload interval of the list and the detail (R12.5). */
export const DETAIL_REFETCH_INTERVAL_MS = 30_000;

/** The latest value in a ref, for effects that must not re-run when it changes. */
function useLatest<T>(value: T): { readonly current: T } {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}

export function TicketDetailScreen() {
  const { ticketNumber } = useParams();
  // Keyed by the address, so a move from one ticket to another starts with no data of the previous one
  // (`placeholderData: keepPreviousData` would otherwise show it).
  return <TicketDetail key={ticketNumber ?? ''} rawNumber={ticketNumber} />;
}

function TicketDetail({ rawNumber }: { rawNumber: string | undefined }) {
  const { t, i18n } = usePortalTranslation();
  const language = resolveLanguage(i18n.language);
  const number = parseTicketNumber(rawNumber);
  const queryClient = useQueryClient();
  const { announce, alert } = useAnnouncer();

  // --- the ticket, polled ---------------------------------------------------------------------------
  const detail = useQuery({
    queryKey: ticketDetailKeys.detail(number ?? 0),
    queryFn: number === null ? skipToken : ({ signal }) => getTicket(number, signal),
    refetchInterval: DETAIL_REFETCH_INTERVAL_MS,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: 'always',
    placeholderData: keepPreviousData,
  });
  const ticket = detail.data;
  const state = screenState({ numberValid: number !== null, hasData: ticket !== undefined, error: detail.error });

  // --- the Field_Labels (a failure falls back to the locale's own labels) ---------------------------
  const labels = useQuery({
    queryKey: ticketDetailKeys.labels,
    queryFn: number === null ? skipToken : ({ signal }) => getFormLabels(signal),
  });

  // --- the photo: bearer header, Blob, object URL ---------------------------------------------------
  const wantsPhoto = ticket?.has_photo === true;
  const photo = useQuery({
    queryKey: ticketDetailKeys.photo(number ?? 0),
    queryFn: number === null ? skipToken : ({ signal }) => getTicketPhoto(number, signal),
    enabled: wantsPhoto,
    staleTime: Infinity,
    // The image is private (Cache-Control: no-store): keep nothing once the screen is left.
    gcTime: 0,
    refetchOnWindowFocus: false,
  });
  const blob = photo.data;
  const [photoUrl, setPhotoUrl] = useState<string | null>(null);
  useEffect(() => {
    if (blob === undefined) {
      setPhotoUrl(null);
      return undefined;
    }
    const url = URL.createObjectURL(blob);
    setPhotoUrl(url);
    return () => {
      URL.revokeObjectURL(url);
    };
  }, [blob]);

  // --- status changes found by a reload (R23.15) ----------------------------------------------------
  const snapshot = useRef<TicketStatusEntry | null>(null);
  const liveText = useLatest({ t, announce, alert });
  useEffect(() => {
    if (ticket === undefined) return;
    const next: TicketStatusEntry = { ticketNumber: ticket.ticket_number, status: ticket.status };
    const change = statusAnnouncement(snapshot.current, next);
    snapshot.current = next;
    if (change !== null) {
      const { t: translate, announce: say } = liveText.current;
      say(
        translate('a11y.live.statusChanged', {
          number: String(change.ticketNumber),
          status: translate(`status.${change.status}`),
        }),
      );
    }
  }, [ticket, liveText]);

  // A failed load or reload is announced once, without moving focus (R23.14).
  const loadErrorKeys = state.kind === 'error' ? state.messageKeys : state.kind === 'ready' ? state.staleErrorKeys : null;
  const latestErrorKeys = useLatest(loadErrorKeys);
  useEffect(() => {
    const keys = latestErrorKeys.current;
    if (detail.error === null || keys === null) return;
    const { t: translate, alert: say } = liveText.current;
    say(keys.map((key) => translate(key)).join(' '));
  }, [detail.error, latestErrorKeys, liveText]);

  // --- cancellation ---------------------------------------------------------------------------------
  const [phase, dispatch] = useReducer(cancelFlowReducer, 'closed');
  const [actionErrorKeys, setActionErrorKeys] = useState<readonly string[] | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const returnFocusTo = useRef<'cancel' | 'heading' | null>(null);
  const inFlight = useRef(false);

  // After the dialog closes: back to the cancel control that opened it, or to the heading when that
  // control is gone (a successful cancellation, or a status that no longer allows it).
  useEffect(() => {
    if (phase !== 'closed' || returnFocusTo.current === null) return;
    const wanted = returnFocusTo.current;
    returnFocusTo.current = null;
    (wanted === 'cancel' ? cancelRef.current ?? headingRef.current : headingRef.current)?.focus();
  }, [phase]);

  function openDialog(): void {
    setActionErrorKeys(null);
    dispatch('open');
  }

  function dismissDialog(): void {
    if (phase !== 'confirming') return;
    returnFocusTo.current = 'cancel';
    dispatch('dismiss');
  }

  async function confirmCancel(): Promise<void> {
    // One confirmation sends one request: the guard also covers a second click before the re-render.
    if (number === null || ticket === undefined || phase !== 'confirming' || inFlight.current) return;
    inFlight.current = true;
    dispatch('confirm');
    announce(t('portal.detail.cancelDialog.pending'));
    try {
      const updated = await cancelTicket(number, ticket.status);
      // The poll must not announce this change a second time: the success message already does.
      snapshot.current = { ticketNumber: updated.ticket_number, status: updated.status };
      queryClient.setQueryData(ticketDetailKeys.detail(number), updated);
      // Other portal views (the ticket list) show the new status the next time they load (R13.3).
      void queryClient.invalidateQueries({
        predicate: (query) => query.queryKey[0] === 'portal' && query.queryKey[1] !== 'me',
        refetchType: 'none',
      });
      announce(t('a11y.live.ticketCancelled', { number: String(updated.ticket_number) }));
      returnFocusTo.current = 'heading';
    } catch (error) {
      const failure = cancelFailureOf(error);
      setActionErrorKeys(failure.messageKeys);
      alert(failure.messageKeys.map((key) => t(key)).join(' '));
      // ticket_status_changed and the other state errors: show the ticket as it is now (R13.9).
      if (failure.reload) void detail.refetch();
      returnFocusTo.current = 'cancel';
    } finally {
      inFlight.current = false;
      dispatch('settled');
    }
  }

  function refresh(): void {
    setActionErrorKeys(null);
    void detail.refetch();
  }

  // --- render ---------------------------------------------------------------------------------------
  let content: ReactNode = null;
  if (state.kind === 'not-found') {
    content = <TicketNotFoundPanel language={language} headingRef={headingRef} />;
  } else if (state.kind === 'ready' && ticket !== undefined) {
    content = (
      <TicketDetailView
        ticket={ticket}
        language={language}
        labels={labels.data}
        photo={photoView({ hasPhoto: wantsPhoto, status: photo.status, error: photo.error, url: photoUrl })}
        staleErrorKeys={state.staleErrorKeys}
        actionErrorKeys={actionErrorKeys}
        headingRef={headingRef}
        cancelRef={cancelRef}
        onRefresh={refresh}
        onRetryPhoto={() => {
          void photo.refetch();
        }}
        onCancel={openDialog}
      />
    );
  } else if (number !== null) {
    content = (
      <TicketDetailMessage
        ticketNumber={number}
        language={language}
        state={state.kind === 'error' ? state : { kind: 'loading' }}
        headingRef={headingRef}
        onRetry={refresh}
      />
    );
  }

  // The dialog is a sibling at a fixed position: data changes never remount it.
  return (
    <>
      {content}
      {phase !== 'closed' && state.kind === 'ready' && ticket !== undefined && (
        <CancelDialog
          ticketNumber={ticket.ticket_number}
          pending={phase === 'pending'}
          onConfirm={() => {
            void confirmCancel();
          }}
          onDismiss={dismissDialog}
        />
      )}
    </>
  );
}
