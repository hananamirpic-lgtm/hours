// Live-region announcements for the ticket list (R23.15; design Property 34).
// Pure: no React, no DOM, no i18n. The caller writes the announcement text in the current language.

import type { TicketStatus, TicketStatusEntry } from '../types';

export interface StatusAnnouncement {
  readonly ticketNumber: number;
  /** The new status. */
  readonly status: TicketStatus;
}

/**
 * Compares two successive loads of the ticket list and returns one announcement per ticket whose status
 * differs, ordered by ticket number ascending.
 *
 * - `previous === null` is the first load: nothing is announced.
 * - A ticket unchanged between the loads is not announced, so a reload of identical data is silent.
 * - A ticket that is in `next` but not in `previous` is not announced. R23.15 announces only "a
 *   Ticket_Status that changed", and a ticket with no earlier status has not changed (it is a new ticket,
 *   or one that came into view on another page).
 * - A ticket that is in `previous` but not in `next` is not announced (nothing left to describe).
 * - If a list holds the same ticket number twice, the last entry counts.
 */
export function diffStatuses(
  previous: readonly TicketStatusEntry[] | null,
  next: readonly TicketStatusEntry[],
): StatusAnnouncement[] {
  if (previous === null) return [];
  const before = new Map<number, TicketStatus>();
  for (const entry of previous) before.set(entry.ticketNumber, entry.status);
  const after = new Map<number, TicketStatus>();
  for (const entry of next) after.set(entry.ticketNumber, entry.status);

  const changed: StatusAnnouncement[] = [];
  for (const [ticketNumber, status] of after) {
    const old = before.get(ticketNumber);
    if (old !== undefined && old !== status) changed.push({ ticketNumber, status });
  }
  return changed.sort((a, b) => a.ticketNumber - b.ticketNumber);
}
