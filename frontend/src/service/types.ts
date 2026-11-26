// Shared types of the service ticket portal. Pure types: no React, no DOM.

/** The four Ticket_Status values the API sends (wire values, not display text). */
export type TicketStatus = 'received' | 'in_progress' | 'closed' | 'cancelled';

/** The minimum a status comparison needs from a ticket in a list. */
export interface TicketStatusEntry {
  readonly ticketNumber: number;
  readonly status: TicketStatus;
}
