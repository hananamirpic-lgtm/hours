// Wire types of the customer ticket endpoints. Pure types: no React, no DOM.
// Mirror of `CategoryRef`, `CustomerTicketRow` and `CustomerTicketPage` in
// backend/app/service_tickets/schemas.py. The ticket list screen (task 23.2) owns this file; the detail
// and create modules may import from it and add their own types in their own files.

import type { TicketStatus } from '../types';

/** An issue category as a ticket shows it: id and both names. */
export interface TicketCategory {
  readonly id: string;
  readonly name_he: string;
  readonly name_en: string;
}

/** One row of `GET /service/tickets`. */
export interface TicketRow {
  /** 10000 to 99999. */
  readonly ticket_number: number;
  readonly category: TicketCategory;
  /** `YYYY-MM-DD`, computed by the server. Never converted through a time zone. */
  readonly created_date: string;
  readonly status: TicketStatus;
  readonly urgent: boolean;
}

/** `GET /service/tickets?page=N`. */
export interface TicketPage {
  readonly items: readonly TicketRow[];
  readonly page: number;
  /** Always 50. */
  readonly page_size: number;
  /** Every ticket the customer owns, not only this page. */
  readonly total: number;
}
