// Wire types of the staff endpoints under `/api/service/staff` (task 24.1).
//
// Hand-written mirror of the response and request models in backend/app/service_tickets/schemas.py
// (`StaffTicketRow`, `StaffTicketDetail`, `CustomerRecord`, `ReportResponse`, ...). Pure types: no React,
// no DOM. The management pages of tasks 24.2 to 24.7 import them from `./staff`.
import type { TicketStatus } from '../types';
import type { FormLabelKey, LabelTexts } from './formTypes';

export type { FormLabelKey, FormSettings, LabelTexts } from './formTypes';
export type { TicketStatus } from '../types';

/** The four statuses in lifecycle order, as the API names them. */
export const TICKET_STATUSES: readonly TicketStatus[] = ['received', 'in_progress', 'closed', 'cancelled'];

/** `CategoryRef`: the category of a ticket, with both names. */
export interface StaffCategoryRef {
  readonly id: string;
  readonly name_he: string;
  readonly name_en: string;
}

/** `LocalityRef`: a settlement with its current names, active or not. */
export interface StaffLocalityRef {
  readonly id: string;
  readonly name_he: string;
  readonly name_en: string;
}
/** `StaffTicketRow`: a row of the staff list and of the report table. */
export interface StaffTicketRow {
  /** 10000 to 99999. */
  readonly ticket_number: number;
  /** An ISO 8601 instant. Staff see it in Asia/Jerusalem, to the minute. */
  readonly created_at: string;
  readonly customer_name: string;
  readonly city: string;
  readonly apartment: string;
  readonly category: StaffCategoryRef;
  readonly status: TicketStatus;
  /** Empty when no technician is assigned. */
  readonly assigned_technician_name: string;
  readonly urgent: boolean;
  /** The settlement with its current names; null or absent for a ticket with a text city (R30.5). */
  readonly locality?: StaffLocalityRef | null;
}

/** `HistoryEntryOut`: one treatment-history entry. `edited_at` is null until the entry is edited. */
export interface StaffHistoryEntry {
  readonly id: string;
  readonly entry_text: string;
  readonly technician_name: string;
  readonly created_at: string;
  readonly edited_at: string | null;
}

/** `StaffTicketDetail`: the row plus contact data, the history (oldest first) and the last-modified time. */
export interface StaffTicketDetail extends StaffTicketRow {
  readonly phone: string;
  readonly description: string;
  readonly has_photo: boolean;
  readonly history: readonly StaffHistoryEntry[];
  readonly updated_at: string;
  /** The staff username that opened the ticket for the customer; null or absent when the customer opened it. */
  readonly opened_by_username?: string | null;
}

/** `StaffTicketPage`: 25 per page. */
export interface StaffTicketPage {
  readonly items: readonly StaffTicketRow[];
  readonly page: number;
  readonly page_size: number;
  readonly total: number;
}

/** `ServiceDashboardResponse`. The warning is present only for configuration staff, below 1,000 left. */
export interface StaffDashboard {
  readonly open: number;
  readonly in_progress: number;
  readonly urgent: number;
  readonly ticket_number_warning: { readonly remaining: number } | null;
}

/** `CustomerRecord`: no password hash, no token version. */
export interface StaffCustomer {
  readonly id: string;
  readonly full_name: string;
  readonly phone: string;
  readonly city: string | null;
  readonly apartment: string | null;
  readonly is_active: boolean;
  /** The settlement with its current names; null or absent when the city is only text (R30.6). */
  readonly locality?: StaffLocalityRef | null;
}

/** `CustomerRecordPage`: 50 per page. */
export interface StaffCustomerPage {
  readonly items: readonly StaffCustomer[];
  readonly page: number;
  readonly page_size: number;
  readonly total: number;
}

/** `CategoryRecord`: active and inactive categories, for configuration and for the list filters. */
export interface StaffCategory {
  readonly id: string;
  readonly name_he: string;
  readonly name_en: string;
  readonly is_active: boolean;
}

/** `CategoryDeleteResult`: removed, or kept and deactivated because tickets use it. */
export type CategoryDeleteResult = 'removed' | 'deactivated';

/** `StatusCounts`: all four are always present, zero included. */
export interface StaffStatusCounts {
  readonly received: number;
  readonly in_progress: number;
  readonly closed: number;
  readonly cancelled: number;
}

/** `CategoryCount`. */
export interface StaffCategoryCount {
  readonly category: StaffCategoryRef;
  readonly count: number;
}

/** `ReportResponse`. */
export interface StaffReport {
  readonly total: number;
  readonly open: number;
  readonly by_status: StaffStatusCounts;
  readonly by_category: readonly StaffCategoryCount[];
  readonly tickets: StaffTicketPage;
}

/** The labels of the seven form fields as `PUT /staff/labels` returns them. */
export type StaffLabels = Readonly<Record<FormLabelKey, LabelTexts>>;

// --- request bodies --------------------------------------------------------------------------------

export interface StatusChangeBody {
  /** The status the caller last saw. A different stored status gives 409 `ticket_status_changed`. */
  readonly expected_status: TicketStatus;
  readonly target_status: TicketStatus;
  /** Used only when a received ticket moves to in progress. */
  readonly assigned_technician_name?: string;
}

export interface HistoryEntryBody {
  readonly entry_text: string;
  /** On add, an absent name becomes the acting username. On edit it is required. */
  readonly technician_name?: string;
}

export interface CustomerBody {
  readonly phone: string;
  readonly full_name: string;
  readonly city?: string | null;
  readonly apartment?: string | null;
}

export interface CategoryBody {
  readonly name_he: string;
  readonly name_en: string;
}

/** The query of `GET /staff/tickets`. Every value is optional; an absent one is not sent. */
export interface TicketListQuery {
  readonly page?: number;
  /** The raw search box text. The server trims it and answers 422 `invalid_ticket_number` when malformed. */
  readonly search?: string;
  readonly status?: readonly TicketStatus[];
  readonly categoryId?: string;
  readonly city?: string;
  readonly urgentOnly?: boolean;
  /** `YYYY-MM-DD`, inclusive. */
  readonly dateFrom?: string;
  readonly dateTo?: string;
  readonly urgentFirst?: boolean;
}

/** The query of `GET /staff/customers`. `active` omitted means every customer. */
export interface CustomerListQuery {
  readonly page?: number;
  readonly search?: string;
  readonly active?: boolean;
}

/** The query of `GET /staff/reports`. */
export interface ReportQuery {
  readonly page?: number;
  readonly dateFrom?: string;
  readonly dateTo?: string;
  readonly city?: string;
  readonly categoryId?: string;
}
