/**
 * Route `/tickets/list/:ticketNumber` (task 24.4; R15.9, R15.10, R16.1 to R16.7, R16.9, R9.13, R23.12).
 *
 * Shows one ticket to Ticket_Staff: every field of R16.1, the photo preview, the status actions, the assigned
 * technician editor and the treatment history with its add and edit forms. The queries and requests live
 * here; the markup is in `ticketDetailSections.tsx` and the decisions are in `ticketDetailModel.ts`.
 *
 * Decisions worth knowing:
 *  - The photo is fetched with the bearer header as a Blob (`fetchTicketPhoto`), shown through
 *    `URL.createObjectURL`, and the object URL is revoked when the blob changes and on unmount.
 *  - A status change sends the status the page displays as `expected_status`. When it fails, the ticket is
 *    reloaded so the page shows its current status next to the message that names the failed action (R15.10).
 *  - An add or edit that fails leaves the form open with the typed values and shows the message beside each
 *    failing field (R16.6). The form component holds the values, so a reload of the ticket never clears them.
 *  - The ticket is loaded once and refreshed after each change. There is no polling: staff act on the data.
 *  - Resolving (R29.4, R29.5, R29.7): the action to Resolved opens the Resolve form under the status section and
 *    sends nothing. The form sends the status change with its resolution; a failure keeps the form and its values
 *    (the form component holds them) and, when the status change itself failed, reloads the ticket. `?resolve=1`
 *    opens the form on load for a ticket in progress and is then removed from the address.
 */
import { skipToken, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { useAuth } from '@/auth/AuthProvider';
import {
  addHistoryEntry,
  changeTicketStatus,
  editHistoryEntry,
  fetchTicketPhoto,
  getTicket,
  setAssignedTechnician,
  staffKeys,
} from '../../api/staff';
import { activePartsKey, listActiveParts } from '../../api/staffParts';
import type { StaffTicketDetail, TicketStatus } from '../../api/staffTypes';
import '../i18n';
import { SERVICE_NAMESPACE } from '../namespace';
import {
  partsView,
  reloadsTicket,
  resolveFailureOf,
  resolveInitialValues,
  resolveStatusBody,
  resolveUrlDecision,
  type ResolveFailure,
  type ResolveValues,
} from '../resolveModel';
import { ResolvePanel } from '../resolveSections';
import {
  categoryName,
  codeOf,
  entryFailureOf,
  errorKeyOf,
  isTicketNotFound,
  languageOf,
  ltrIsolate,
  parseTicketNumber,
  photoView,
  screenState,
  statusChangeBody,
  statusFailureOf,
  withEntry,
  type EntryFailure,
  type EntryValues,
  type StatusFailure,
} from '../ticketDetailModel';
import {
  AssignedTechnicianEditor,
  HistorySection,
  PartsReplacedSection,
  PhotoSection,
  StatusActions,
  TicketFields,
  type HistoryMode,
} from '../ticketDetailSections';

/** The key of the photo of one ticket. It sits under `staffKeys.all` but outside the ticket detail key. */
function photoKey(ticketNumber: number): readonly unknown[] {
  return [...staffKeys.all, 'ticket-photo', ticketNumber];
}

export function TicketDetailPage() {
  const { ticketNumber } = useParams();
  // Keyed by the address, so a move from one ticket to another starts with no state of the previous one.
  return <TicketDetail key={ticketNumber ?? ''} rawNumber={ticketNumber} />;
}

function PageHeading({ ticketNumber }: { ticketNumber: number | null }) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  return (
    <div className="page-header">
      <h1>
        {ticketNumber === null
          ? t('management.ticket.notFound.title')
          : t('management.ticket.title', { number: ltrIsolate(String(ticketNumber)) })}
      </h1>
      <Link to="/tickets/list" className="button ticket-detail-back">
        {t('management.ticket.back')}
      </Link>
    </div>
  );
}

function TicketDetail({ rawNumber }: { rawNumber: string | undefined }) {
  const { t, i18n } = useTranslation(SERVICE_NAMESPACE);
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const language = languageOf(i18n.language);
  const number = parseTicketNumber(rawNumber);

  // --- the ticket ------------------------------------------------------------------------------------
  const detail = useQuery({
    queryKey: staffKeys.ticket(number ?? 0),
    queryFn: number === null ? skipToken : ({ signal }) => getTicket(number, signal),
    // A missing ticket will not appear on a second try.
    retry: (count, error) => !isTicketNotFound(error) && count < 1,
  });
  const ticket = detail.data;
  const state = screenState({ numberValid: number !== null, hasData: ticket !== undefined, error: detail.error });

  // --- the photo: bearer header, Blob, object URL (revoked on change and on unmount) ------------------
  const wantsPhoto = ticket?.has_photo === true;
  const photo = useQuery({
    queryKey: photoKey(number ?? 0),
    queryFn: number === null ? skipToken : ({ signal }) => fetchTicketPhoto(number, signal),
    enabled: wantsPhoto,
    staleTime: Infinity,
    // The image is private (Cache-Control: no-store): keep nothing once the page is left.
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

  // --- changes ---------------------------------------------------------------------------------------
  const detailKey = staffKeys.ticket(number ?? 0);

  /** Puts a server answer into the cache and marks the other staff views (list, dashboard, reports) stale. */
  function remember(update: (current: StaffTicketDetail) => StaffTicketDetail): void {
    queryClient.setQueryData<StaffTicketDetail>(detailKey, (current) => (current === undefined ? current : update(current)));
    void queryClient.invalidateQueries({ queryKey: staffKeys.all, refetchType: 'none' });
  }

  // Status (R15.9, R15.10)
  const [statusPending, setStatusPending] = useState<TicketStatus | null>(null);
  const [statusFailure, setStatusFailure] = useState<StatusFailure | null>(null);
  const [approvalName, setApprovalName] = useState('');
  const statusBusy = useRef(false);

  async function changeStatus(target: TicketStatus): Promise<void> {
    if (number === null || ticket === undefined || statusBusy.current) return;
    const from = ticket.status;
    statusBusy.current = true;
    setStatusPending(target);
    setStatusFailure(null);
    setResolvedNotice(false);
    try {
      const updated = await changeTicketStatus(number, statusChangeBody(from, target, approvalName));
      remember(() => updated);
      setApprovalName('');
    } catch (error) {
      setStatusFailure(statusFailureOf(from, target, error));
      // Show the current status of the ticket, which is what the server compared with (R15.10).
      void detail.refetch();
    } finally {
      statusBusy.current = false;
      setStatusPending(null);
    }
  }

  // Resolve (R29.4, R29.5). The status action that moves a ticket to Resolved opens a form on the page; the form
  // sends the change, with its resolution, in one request. The parts of the picker load while the form is open.
  const location = useLocation();
  const navigate = useNavigate();
  const [resolveOpen, setResolveOpen] = useState(false);
  const [resolvedNotice, setResolvedNotice] = useState(false);
  /** Counts the requests to put focus on the status heading (after a success or a cancel). */
  const [statusFocus, setStatusFocus] = useState(0);
  const statusHeadingRef = useRef<HTMLHeadingElement>(null);
  const activeParts = useQuery({
    queryKey: activePartsKey,
    queryFn: ({ signal }) => listActiveParts(signal),
    enabled: resolveOpen,
    retry: false,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    if (statusFocus > 0) statusHeadingRef.current?.focus();
  }, [statusFocus]);

  // `?resolve=1` (the list's "Resolve…" action) opens the form once the ticket is loaded, only for a ticket that is
  // in progress, and the parameter is then removed from the address (R29.5). Wait for a running reload, so a
  // ticket shown from the cache with an old status is not judged.
  const urlHandled = useRef(false);
  useEffect(() => {
    if (urlHandled.current || ticket === undefined || detail.isFetching) return;
    const decision = resolveUrlDecision(location.search, ticket.status);
    if (decision.nextSearch === null) return;
    urlHandled.current = true;
    if (decision.open) setResolveOpen(true);
    navigate({ pathname: location.pathname, search: decision.nextSearch }, { replace: true });
  }, [ticket, detail.isFetching, location.pathname, location.search, navigate]);

  async function submitResolve(values: ResolveValues): Promise<ResolveFailure | null> {
    if (number === null || ticket === undefined) return null;
    try {
      const updated = await changeTicketStatus(number, resolveStatusBody(ticket.status, values));
      remember(() => updated);
      setResolveOpen(false);
      setResolvedNotice(true);
      setStatusFocus((count) => count + 1);
      return null;
    } catch (error) {
      const failure = resolveFailureOf(error);
      // A failure of the status change itself: show the current status of the ticket (R15.10). The form stays open.
      if (reloadsTicket(failure)) void detail.refetch();
      return failure;
    }
  }

  // Assigned technician (R16.2, R16.7). `draft` is null until the staff member types: the field then follows the stored name.
  const [draft, setDraft] = useState<string | null>(null);
  const [assignedSaving, setAssignedSaving] = useState(false);
  const [assignedError, setAssignedError] = useState<string | null>(null);
  const [assignedSaved, setAssignedSaved] = useState(false);
  const assignedBusy = useRef(false);

  async function saveAssigned(): Promise<void> {
    if (number === null || ticket === undefined || assignedBusy.current) return;
    assignedBusy.current = true;
    setAssignedSaving(true);
    setAssignedError(null);
    setAssignedSaved(false);
    try {
      const updated = await setAssignedTechnician(number, draft ?? ticket.assigned_technician_name);
      remember(() => updated);
      setDraft(null);
      setAssignedSaved(true);
    } catch (error) {
      // The typed value stays in the field.
      setAssignedError(codeOf(error));
    } finally {
      assignedBusy.current = false;
      setAssignedSaving(false);
    }
  }

  // History (R16.3 to R16.6)
  const [mode, setMode] = useState<HistoryMode>({ kind: 'closed' });

  async function submitEntry(at: HistoryMode, values: EntryValues): Promise<EntryFailure | null> {
    if (number === null || at.kind === 'closed') return null;
    try {
      const entry =
        at.kind === 'edit'
          ? await editHistoryEntry(number, at.id, values)
          : await addHistoryEntry(number, values);
      remember((current) => withEntry(current, entry));
      setMode({ kind: 'closed' });
      return null;
    } catch (error) {
      return entryFailureOf(error);
    }
  }

  // --- render ----------------------------------------------------------------------------------------
  if (state.kind === 'not-found') {
    return (
      <section className="page-body">
        <PageHeading ticketNumber={null} />
        <p>{t('management.ticket.notFound.body')}</p>
      </section>
    );
  }

  if (state.kind !== 'ready' || ticket === undefined) {
    return (
      <section className="page-body" aria-busy={state.kind === 'loading'}>
        <PageHeading ticketNumber={number} />
        {state.kind === 'error' ? (
          <div className="feedback feedback--error" role="alert">
            <p>{t('management.ticket.loadError')}</p>
            <p>{t(errorKeyOf(state.code))}</p>
            <button
              type="button"
              className="button"
              onClick={() => {
                void detail.refetch();
              }}
            >
              {t('management.common.retry')}
            </button>
          </div>
        ) : (
          <p className="state">{t('management.common.loading')}</p>
        )}
      </section>
    );
  }

  return (
    <section className="page-body ticket-detail">
      <PageHeading ticketNumber={ticket.ticket_number} />

      {state.staleErrorCode !== null && (
        // A reload failed: the ticket already shown stays, with a retry control.
        <div className="feedback feedback--error" role="alert">
          <p>{t('management.ticket.loadError')}</p>
          <p>{t(errorKeyOf(state.staleErrorCode))}</p>
          <button
            type="button"
            className="button"
            onClick={() => {
              void detail.refetch();
            }}
          >
            {t('management.common.retry')}
          </button>
        </div>
      )}

      <TicketFields ticket={ticket} language={language} />

      <PhotoSection
        ticketNumber={ticket.ticket_number}
        categoryLabel={categoryName(ticket.category, language)}
        view={photoView({ hasPhoto: wantsPhoto, status: photo.status, error: photo.error, url: photoUrl })}
        onRetry={() => {
          void photo.refetch();
        }}
      />

      <StatusActions
        ticketNumber={ticket.ticket_number}
        status={ticket.status}
        // While the Resolve form is open, the action that opened it is off.
        pending={statusPending ?? (resolveOpen && ticket.status === 'in_progress' ? 'closed' : null)}
        technicianName={approvalName}
        onTechnicianNameChange={setApprovalName}
        onChange={(target) => {
          // Resolving never sends from here: the form collects the resolution first (R29.4).
          if (target === 'closed') setResolveOpen(true);
          else void changeStatus(target);
        }}
        failure={statusFailure}
        headingRef={statusHeadingRef}
        notice={resolvedNotice ? t('management.ticket.resolve.success', { number: ltrIsolate(String(ticket.ticket_number)) }) : null}
      />

      {resolveOpen && (
        <ResolvePanel
          language={language}
          initial={resolveInitialValues(ticket.assigned_technician_name, user?.username)}
          partsView={partsView({ status: activeParts.status, data: activeParts.data, error: activeParts.error })}
          onRetryParts={() => {
            void activeParts.refetch();
          }}
          onSubmit={submitResolve}
          onCancel={() => {
            setResolveOpen(false);
            setStatusFocus((count) => count + 1);
          }}
        />
      )}

      <PartsReplacedSection parts={ticket.parts_replaced} language={language} />

      <AssignedTechnicianEditor
        value={draft ?? ticket.assigned_technician_name}
        saving={assignedSaving}
        errorCode={assignedError}
        saved={assignedSaved}
        onChange={(value) => {
          setDraft(value);
          setAssignedSaved(false);
        }}
        onSave={() => {
          void saveAssigned();
        }}
      />

      <HistorySection
        entries={ticket.history}
        mode={mode}
        username={user?.username ?? ''}
        onOpenAdd={() => setMode({ kind: 'add' })}
        onOpenEdit={(id) => setMode({ kind: 'edit', id })}
        onClose={() => setMode({ kind: 'closed' })}
        onSubmit={submitEntry}
      />
    </section>
  );
}
