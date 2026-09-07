/** Mirrors the locale-neutral payloads returned by the API. */

export type HealthStatus = 'healthy' | 'degraded';
export type DependencyState = 'up' | 'down';

export interface HealthResponse {
  status: 'healthy';
  service: string;
  version: string;
  environment: string;
}

export interface DependencyStatus {
  name: string;
  status: DependencyState;
  latency_ms: number;
  detail: string | null;
}

export interface ReadinessResponse {
  status: HealthStatus;
  checks: DependencyStatus[];
}

// --------------------------------------------------------------------------- auth

export type UserRole = 'admin' | 'site_manager' | 'accounting' | 'employee';
export type AppLanguage = 'he' | 'en';

/** The token pair returned by POST /api/auth/login and /api/auth/refresh. */
export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: 'bearer';
  expires_in: number;
}

/** What GET /api/auth/me returns. No credential fields; mirrors the backend schema exactly. */
export interface CurrentUser {
  id: string;
  username: string;
  role: UserRole;
  employee_id: string | null;
  language: AppLanguage;
  is_2fa_enabled: boolean;
  is_2fa_enrolment_required: boolean;
  is_2fa_enrolment_prompted: boolean;
  /**
   * True when the caller owes a first-use password change — a non-administrator created with an
   * administrator-chosen password, or an existing non-admin backfilled by migration 0005. While
   * true, every endpoint outside `/auth` answers 403, so the front end shows the change screen.
   * Always false for the admin role, which is exempt.
   */
  must_change_password: boolean;
  last_login_at: string | null;
}

/** A newly issued 2FA secret (POST /api/auth/2fa/setup). Carries a credential — never persist it. */
export interface TotpSetupResponse {
  /** Base32, for typing into an authenticator by hand. */
  secret: string;
  /** `otpauth://totp/...`, for rendering as a QR code. */
  provisioning_uri: string;
}

// --------------------------------------------------------------------------- users (Requirement 1, 2.1, 2.3, 20.8)

/** A row in the user list: identity, role and active state, no credential. */
export interface UserListItem {
  id: string;
  username: string;
  role: UserRole;
  is_active: boolean;
  is_2fa_enabled: boolean;
}

/**
 * A user card as served (GET /api/users/{id}). No credential field ever appears. `site_ids` carries a
 * site manager's assigned scope (Requirement 2.3) and is empty for every other role. The two
 * enrolment flags let an administrator see whether a manager or accounting user still owes a 2FA
 * enrolment (Requirement 1.6).
 */
export interface UserResponse {
  id: string;
  username: string;
  role: UserRole;
  employee_id: string | null;
  language: AppLanguage;
  is_active: boolean;
  is_2fa_enabled: boolean;
  is_2fa_enrolment_required: boolean;
  is_2fa_enrolment_prompted: boolean;
  /** Whether the login still owes a first-use password change; always false for the admin role. */
  must_change_password: boolean;
  last_login_at: string | null;
  created_at: string;
  updated_at: string;
  site_ids: string[];
}

/** The body of POST /api/users. `site_ids` applies only to a site manager. */
export interface UserCreate {
  username: string;
  password: string;
  role: UserRole;
  employee_id?: string | null;
  language?: AppLanguage;
  site_ids?: string[];
}

/**
 * The body of PATCH /api/users/{id}. Every field optional; an omitted field is unchanged. A
 * `password` here is a reset and ends the user's sessions immediately (Requirement 20.8).
 */
export interface UserUpdate {
  username?: string;
  password?: string;
  role?: UserRole;
  employee_id?: string | null;
  language?: AppLanguage;
}

/** The sites a site manager is assigned to (PUT /api/users/{id}/sites). */
export interface UserSitesResponse {
  user_id: string;
  site_ids: string[];
}

/** The machine error envelope, nested under `detail` until the API-conventions handler lifts it. */
export interface ApiErrorBody {
  detail?: { error?: { code?: string; params?: Record<string, string>; actions?: string[] } };
  error?: { code?: string; params?: Record<string, string>; actions?: string[] };
}

// --------------------------------------------------------------------------- shared

/** The shape every paginated list endpoint returns (Requirement 22.5). */
export interface Paginated<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

// --------------------------------------------------------------------------- employees

export type EmployeeStatus = 'active' | 'on_leave' | 'inactive' | 'terminated';

/** A row in the employee list — a directory entry, no sensitive or wage fields. */
export interface EmployeeListItem {
  id: string;
  full_name: string;
  full_name_en: string;
  /** The auto-generated 4-digit login number, or null for a record that predates it. */
  employee_number: string | null;
  staffing_company_id: string | null;
  country: string | null;
  position: string | null;
  status: EmployeeStatus;
  start_date: string;
}

/** One effective-dated pay row. Absent entirely from a site manager's payload (redaction). */
export interface EmployeeRate {
  id: string;
  hourly_wage: string;
  overtime_rate: string;
  shabbat_holiday_rate: string;
  travel_allowance_daily: string;
  effective_from: string;
  effective_to: string | null;
}

/** A pay row as submitted when replacing the history. */
export interface EmployeeRateInput {
  hourly_wage: string;
  overtime_rate: string;
  shabbat_holiday_rate: string;
  travel_allowance_daily: string;
  effective_from: string;
  effective_to?: string | null;
}

/**
 * The employee card. The wage fields and `rates` are optional because a site manager's response has
 * them stripped by redaction — their absence means "not permitted to see", not "unset".
 */
export interface EmployeeResponse {
  id: string;
  full_name: string;
  full_name_en: string;
  /** The auto-generated 4-digit login number, or null for a record that predates it. */
  employee_number: string | null;
  staffing_company_id: string | null;
  photo_key: string | null;
  passport_number: string;
  phone: string;
  country: string | null;
  date_of_birth: string | null;
  address: string | null;
  emergency_contact_name: string | null;
  emergency_contact_phone: string | null;
  notes: string | null;
  start_date: string;
  position: string | null;
  status: EmployeeStatus;
  created_at: string;
  updated_at: string;
  has_expired_document: boolean;
  hourly_wage?: string | null;
  overtime_rate?: string | null;
  shabbat_holiday_rate?: string | null;
  travel_allowance_daily?: string | null;
  rates?: EmployeeRate[];
}

/** The mandatory and optional fields for creating an employee (Requirement 3.1–3.3). */
export interface EmployeeCreate {
  full_name: string;
  full_name_en: string;
  passport_number: string;
  phone: string;
  /** Mandatory on create (Requirement 2.1-2.3); optional on update via EmployeeUpdate. */
  staffing_company_id: string;
  country?: string | null;
  emergency_contact_name?: string | null;
  emergency_contact_phone?: string | null;
  start_date: string;
  position?: string | null;
  date_of_birth?: string | null;
  address?: string | null;
  notes?: string | null;
  photo_key?: string | null;
  status?: EmployeeStatus;
  rate?: EmployeeRateInput | null;
}

/** Every field optional; an omitted field is left unchanged. Status moves through its own endpoint. */
export type EmployeeUpdate = Partial<Omit<EmployeeCreate, 'status' | 'rate'>>;

// --------------------------------------------------------------------------- documents

export type DocumentType = 'passport' | 'work_permit' | 'other';

export interface DocumentResponse {
  id: string;
  employee_id: string;
  type: DocumentType;
  file_name: string;
  mime_type: string;
  size_bytes: number;
  expiry_date: string | null;
  is_expired: boolean;
  created_at: string;
  updated_at: string;
}

export interface DocumentListResponse {
  items: DocumentResponse[];
  total: number;
  has_expired_document: boolean;
}

export interface DocumentUploadTicket {
  file_key: string;
  upload_url: string;
  required_headers: Record<string, string>;
  expires_in_seconds: number;
  max_bytes: number;
}

// --------------------------------------------------------------------------- clients

export interface ClientListItem {
  id: string;
  name: string;
  company: string | null;
  company_number: string | null;
  is_archived: boolean;
}

export interface ClientResponse {
  id: string;
  name: string;
  company: string | null;
  company_number: string | null;
  contact_person: string | null;
  phone: string | null;
  email: string | null;
  address: string | null;
  payment_terms_days: number | null;
  payment_terms_notes: string | null;
  notes: string | null;
  is_archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface ClientCreate {
  name: string;
  company?: string | null;
  company_number?: string | null;
  contact_person?: string | null;
  phone?: string | null;
  email?: string | null;
  address?: string | null;
  payment_terms_days?: number | null;
  payment_terms_notes?: string | null;
  notes?: string | null;
}

export type ClientUpdate = Partial<ClientCreate>;

export interface ClientSiteItem {
  id: string;
  name: string;
  site_number: string;
  status: SiteStatus;
}

export interface ClientSitesResponse {
  client_id: string;
  items: ClientSiteItem[];
  total: number;
}

// --------------------------------------------------------------------------- sites

export type SiteStatus = 'active' | 'completed' | 'on_hold';
export type QrMode = 'unified' | 'separate';
export type AssignmentMode = 'open' | 'strict';

export interface SiteListItem {
  id: string;
  name: string;
  site_number: string;
  client_id: string;
  status: SiteStatus;
}

export interface SiteRate {
  id: string;
  billing_rate: string;
  overtime_billing_rate: string | null;
  effective_from: string;
  effective_to: string | null;
}

export interface SiteRateInput {
  billing_rate: string;
  overtime_billing_rate?: string | null;
  effective_from: string;
  effective_to?: string | null;
}

export interface SiteResponse {
  id: string;
  name: string;
  site_number: string;
  client_id: string;
  manager_user_id: string | null;
  address: string | null;
  start_date: string | null;
  end_date: string | null;
  status: SiteStatus;
  qr_mode: QrMode;
  assignment_mode: AssignmentMode;
  notes: string | null;
  created_at: string;
  updated_at: string;
  employee_ids: string[];
  billing_rate?: string | null;
  overtime_billing_rate?: string | null;
  site_rates?: SiteRate[];
}

export interface SiteCreate {
  name: string;
  site_number: string;
  client_id: string;
  address?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  notes?: string | null;
  status?: SiteStatus;
  qr_mode?: QrMode;
  assignment_mode?: AssignmentMode;
  rate?: SiteRateInput | null;
}

export type SiteUpdate = Partial<Omit<SiteCreate, 'rate'>>;

export interface EmployeeSitesResponse {
  employee_id: string;
  site_ids: string[];
}

// --------------------------------------------------------------------------- scans (attendance)

/**
 * What the server did with a scan (POST /api/scans), mirroring `ScanAction` on the backend. A
 * conflict — an open shift at another site — is not an action; it comes back as a 409 error with
 * `open_shift_elsewhere`, so it is absent here.
 */
export type ScanAction = 'check_in' | 'check_out' | 'duplicate_ignored';

/**
 * The outcome of a scan or an explicit check-out. Locale-neutral: the server states which entry, at
 * which site, what happened, and the UTC time the action recorded; the front end formats and
 * translates. `flags` surfaces an anomaly marker (e.g. an unassigned-site check-in) so the
 * confirmation can note it without a second request.
 *
 * The response carries `site_id` but no site name — the employee scan endpoints are deliberately
 * spare — so the confirmation states the action and local time, and names the site only where a
 * name is separately available (the conflict error carries one).
 */
export interface ScanResult {
  time_entry_id: string;
  site_id: string;
  action: ScanAction;
  /** The server-side timestamp the action recorded (UTC ISO 8601). */
  at: string;
  work_date: string;
  is_open: boolean;
  flags: string[];
}

/** The caller's current open shift, from GET /api/scans/status. `site_id` only, no name. */
export interface OpenShift {
  time_entry_id: string;
  site_id: string;
  check_in_at: string;
  work_date: string;
  flags: string[];
}

/** The body of GET /api/scans/status: the open shift, or its absence. */
export interface ScanStatusResponse {
  open_shift: OpenShift | null;
}

/**
 * One day of the caller's own recent work, from GET /api/scans/history. Locale-neutral: `work_date`
 * is the local calendar day as an ISO date and `total_minutes` is the whole minutes worked that day
 * summed across every site (Requirement 11.2). The same shape the `WorkHistory` component renders.
 */
export interface WorkHistoryDay {
  work_date: string;
  total_minutes: number;
}

/**
 * The body of GET /api/scans/history: the caller's recent completed days, newest first. An empty
 * `days` means no recent completed work — or a login not linked to an employee — and the home screen
 * shows "no recent work" (Requirement 23.3).
 */
export interface WorkHistoryResponse {
  days: WorkHistoryDay[];
}

/**
 * One site the caller is assigned to, from GET /api/scans/my-sites. Just an id and a name — enough
 * for the self-check-in picker to list where the employee may open a no-QR shift, and nothing more
 * (no billing, no client): the employee is choosing where they work, not administering the site.
 */
export interface AssignedSite {
  id: string;
  name: string;
}

/**
 * The body of GET /api/scans/my-sites: the caller's own assigned active sites for the self-check-in
 * picker. An empty `sites` means the employee is assigned to no active site — or is a login not
 * linked to an employee — and the picker shows there is nowhere to check in without a QR.
 */
export interface MySitesResponse {
  sites: AssignedSite[];
}

// --------------------------------------------------------------------------- time entries (hours view)

/** The approval status of a time entry (Requirement 15.1), mirroring `TimeEntryStatus` on the backend. */
export type TimeEntryStatus = 'draft' | 'review' | 'approved' | 'locked';

/** How an entry came to exist (Requirement 12.4), mirroring `TimeEntrySource`. */
export type TimeEntrySource = 'qr_scan' | 'manual' | 'system_transition';

/** The anomaly markers an entry may carry; the hours view surfaces them and filters on them. */
export type TimeEntryFlag = 'unassigned_site' | 'implausible_duration' | 'self_reported';

/**
 * One recorded shift as the hours view reads it (GET /api/time-entries). Locale-neutral: ISO
 * timestamps, whole minutes, and both employee name forms so the console labels rows in either
 * language (Requirement 21.5). `total_minutes` and `check_out_at` are null while the shift is still
 * open. No wage or billing field appears — a time entry records when someone worked, not their pay.
 */
export interface TimeEntryListItem {
  id: string;
  employee_id: string;
  employee_name: string;
  employee_name_en: string;
  employee_number: string | null;
  site_id: string;
  site_name: string;
  work_date: string;
  check_in_at: string;
  check_out_at: string | null;
  total_minutes: number | null;
  source: TimeEntrySource;
  is_manual: boolean;
  status: TimeEntryStatus;
  flags: string[];
}

/**
 * One entry as a write endpoint returns it (POST/PATCH/DELETE /api/time-entries), mirroring
 * `TimeEntryResponse` on the backend. The same shape a list row carries minus the joined labels,
 * plus the soft-delete marker so the console can confirm a deletion retained the record
 * (Requirement 12.7). No wage or billing field — a time entry records when someone worked.
 */
export interface TimeEntryResponse {
  id: string;
  employee_id: string;
  site_id: string;
  work_date: string;
  check_in_at: string;
  check_out_at: string | null;
  total_minutes: number | null;
  source: TimeEntrySource;
  is_manual: boolean;
  manual_reason: string | null;
  status: TimeEntryStatus;
  flags: string[];
  deleted_at: string | null;
}

/**
 * The body of POST /api/time-entries: a manually recorded, completed shift (Requirement 12.1). The
 * reason is mandatory and non-blank (Requirement 12.3); `work_date` is optional and the server
 * attributes it from the check-in when omitted (Requirement 10.4).
 */
export interface TimeEntryCreate {
  employee_id: string;
  site_id: string;
  check_in_at: string;
  check_out_at: string;
  work_date?: string | null;
  reason: string;
  /** An administrator's override to write into a locked month (Requirement 15.5). */
  override?: boolean;
}

/**
 * The body of PATCH /api/time-entries/{id}: a correction to an entry's times (Requirement 12.2).
 * Either time may be sent; at least one must be. The reason is mandatory and non-blank
 * (Requirement 12.3). Editing marks the entry manual wherever it appears (Requirement 12.4).
 */
export interface TimeEntryUpdate {
  check_in_at?: string;
  check_out_at?: string;
  reason: string;
  /** An administrator's override to correct an entry in a locked month (Requirement 15.5). */
  override?: boolean;
}

/** The body of DELETE /api/time-entries/{id}: a soft delete with a mandatory reason (Req 12.7). */
export interface TimeEntryDelete {
  reason: string;
  /** An administrator's override to delete an entry in a locked month (Requirement 15.5). */
  override?: boolean;
}

// --------------------------------------------------------------------------- approval workflow & periods (Requirement 15)

/**
 * The body of POST /api/time-entries/bulk-status: move a set of entries to a target status along the
 * ladder Draft → Review → Approved → Locked (Requirement 15.2, 15.3). A site manager may only step
 * forward; an administrator may also reverse one rung, which requires a reason (Requirement 15.6).
 */
export interface BulkStatusRequest {
  entry_ids: string[];
  target_status: TimeEntryStatus;
  reason?: string | null;
}

/** What a bulk status change moved (Requirement 15.2). */
export interface BulkStatusResponse {
  updated_count: number;
  updated_ids: string[];
  target_status: TimeEntryStatus;
}

/** One entry standing in the way of a clean lock, for the warning list (Requirement 15.7). */
export interface UnapprovedEntry {
  id: string;
  employee_id: string;
  site_id: string;
  work_date: string;
  status: TimeEntryStatus;
}

/** One calendar month's lock state, derived from the timestamps (Requirement 15.4, 15.6). */
export interface PeriodState {
  year: number;
  month: number;
  is_locked: boolean;
  locked_at: string | null;
  locked_by_user_id: string | null;
  unlocked_at: string | null;
  unlocked_by_user_id: string | null;
  unlock_reason: string | null;
}

/** The months the workflow has touched, most recent first (GET /api/periods). */
export interface PeriodListResponse {
  items: PeriodState[];
}

/**
 * The outcome of a lock attempt (Requirement 15.4, 15.7). When `locked` is false the month held
 * unapproved entries and none was frozen — `unapproved` is the warning list the administrator must
 * act on or override with `force`.
 */
export interface PeriodLockResult {
  locked: boolean;
  year: number;
  month: number;
  locked_count: number;
  unapproved: UnapprovedEntry[];
  state: PeriodState | null;
}

/** The body of POST /api/periods/{year}/{month}/lock (Requirement 15.4, 15.7). */
export interface PeriodLockRequest {
  force?: boolean;
}

/** The body of POST /api/periods/{year}/{month}/unlock: reopen a locked month (Requirement 15.6). */
export interface PeriodUnlockRequest {
  reason: string;
}

// --------------------------------------------------------------------------- payroll (Requirement 16)

/**
 * Whether a payroll record is a working draft or final (mirrors `CalculationStatus` on the backend).
 * A record for an open month is `draft` and may be recalculated; `final` marks a record whose month
 * is locked, so its figures are frozen with the entries they were computed from (Requirement 16.10).
 */
export type CalculationStatus = 'draft' | 'final';

/**
 * One site's share of an employee's monthly cost (Requirement 16.8), mirroring `SiteAllocationResponse`.
 * The minutes are the month's minutes worked at this site per bucket; `cost` is the largest-remainder
 * corrected figure that, summed across a record's allocations, equals the record's worked pay exactly.
 * Locale-neutral: `cost` is a raw decimal string the screen formats through `formatCurrency`.
 */
export interface SiteAllocation {
  site_id: string;
  regular_minutes: number;
  overtime_minutes: number;
  shabbat_minutes: number;
  holiday_minutes: number;
  cost: string;
}

/**
 * One employee's monthly payroll record with its per-site allocation (GET /api/payroll and the
 * calculate/read endpoints), mirroring `PayrollRecordResponse`. The four bucket minute totals and
 * their pay, the allowances (`travel`, `bonuses`), the `deductions`, the `total_pay`, the status, and
 * when it was calculated. Every money field is a raw decimal string the screen formats; the minutes
 * are integers the screen renders through `formatDuration`. `allocations` is the per-site cost
 * breakdown whose costs sum to the worked pay exactly (Requirement 16.8). The whole payload is wage
 * data — the finance-role guard on the server, not a per-field strip, is what keeps it from the wrong
 * reader (Requirement 2.6).
 */
export interface PayrollRecord {
  id: string;
  employee_id: string;
  year: number;
  month: number;

  regular_minutes: number;
  overtime_minutes: number;
  shabbat_minutes: number;
  holiday_minutes: number;

  regular_pay: string;
  overtime_pay: string;
  shabbat_pay: string;
  holiday_pay: string;

  travel: string;
  bonuses: string;
  deductions: string;
  total_pay: string;

  status: CalculationStatus;
  calculated_at: string | null;

  allocations: SiteAllocation[];
}

/** A page of payroll records for a period (GET /api/payroll), mirroring `PayrollListResponse`. */
export interface PayrollListResponse {
  items: PayrollRecord[];
  total: number;
  limit: number;
  offset: number;
}

// --------------------------------------------------------------------------- billing (Requirement 17)

/**
 * One site's billing, cost and profit for a month (GET /api/billing and the calculate endpoint),
 * mirroring `SiteBillingResponse`. `regular_minutes` and `overtime_minutes` are the month's billable
 * minutes at the site; `billing` is the summed billing amount; the two applied rates are the ones the
 * record stored for the invoice (Requirement 17.2). `cost` and `profit` are computed live at
 * calculation time (Requirement 17.4) and are therefore present on the calculate response but `null`
 * on a stored read — `billing_records` stores the billed amount, not the cost it was compared
 * against. Every money field is a raw decimal string the screen formats; the minutes are integers the
 * screen renders through `formatDuration`. The whole payload is billing data — the finance-role guard
 * on the server, not a per-field strip, is what keeps it from the wrong reader (Requirement 17.7).
 */
export interface SiteBilling {
  site_id: string;
  client_id: string;
  regular_minutes: number;
  overtime_minutes: number;
  billing_rate_applied: string;
  overtime_rate_applied: string | null;
  billing: string;
  cost: string | null;
  profit: string | null;
  status: CalculationStatus;
  calculated_at: string | null;
}

/**
 * One client's billing, cost and profit for a month, summed over its sites (Requirement 17.3),
 * mirroring `ClientBillingResponse`. On a stored read `cost` is zero and `profit` equals `billing`,
 * because the stored records carry the billed amount but not the cost; the calculate response carries
 * the live cost and profit. Money fields are raw decimal strings the screen formats.
 */
export interface ClientBilling {
  client_id: string;
  billing: string;
  cost: string;
  profit: string;
}

/**
 * A month's billing summary (GET /api/billing and the calculate endpoint), mirroring
 * `BillingSummaryResponse`. `sites` is one row per billed site; `clients` aggregates them per client
 * (Requirement 17.3); the `total_*` are the grand totals (Requirement 17.4). `excluded_entry_count`
 * and `excluded_minutes` state the count and hours of unapproved entries left out, so a low billing
 * figure is never mistaken for a low month (Requirement 17.6).
 */
export interface BillingSummary {
  year: number;
  month: number;
  sites: SiteBilling[];
  clients: ClientBilling[];
  total_billing: string;
  total_cost: string;
  total_profit: string;
  excluded_entry_count: number;
  excluded_minutes: number;
}

/**
 * The body of POST /api/billing/calculate (Requirement 17.1, 17.3). Names the month; there is no
 * per-site or per-client selector, because the summary is the whole period's billing.
 */
export interface BillingCalculateRequest {
  year: number;
  month: number;
}

/**
 * The body of POST /api/payroll/calculate (Requirement 16.5, 16.10). Names the employee and month;
 * `travel`, `bonuses` and `deductions` are optional decimal strings, omitted to let the service
 * derive the travel allowance and treat the adjustments as nothing.
 */
export interface PayrollCalculateRequest {
  employee_id: string;
  year: number;
  month: number;
  travel?: string | null;
  bonuses?: string | null;
  deductions?: string | null;
}

// --------------------------------------------------------------------------- audit history

/** The entity types the audit history view is offered for (Requirement 13.4). */
export type AuditEntityType = 'employees' | 'sites' | 'time_entries';

/**
 * One field-level change from the audit trail (GET /api/audit), mirroring `AuditEntry` on the
 * backend. Locale-neutral: `old_value` and `new_value` are the audit writer's stable strings (an ISO
 * date, a plain decimal, `true`/`false`, an enum's value, or the redaction marker for a sensitive
 * field), and the screen composes the readable sentence of Requirement 13.4 in the reader's language.
 * `actor_name` is the acting user's name, or null when the system acted on its own account — a
 * scheduled job or a shift closed by a site transition (Requirement 11.8). `changed_at` is a UTC ISO
 * 8601 instant the screen renders in local time.
 */
export interface AuditEntry {
  id: string;
  entity_type: string;
  entity_id: string;
  field: string;
  old_value: string | null;
  new_value: string | null;
  actor_name: string | null;
  changed_at: string;
  reason: string | null;
  request_id: string | null;
}

// --------------------------------------------------------------------------- settings (the settings screen)

/**
 * How a setting's text `value` is interpreted, mirroring `SettingValueType` on the backend. The
 * settings screen reads this to pick the right input — a number box for `integer`/`decimal`, a time
 * picker for `time`, a checkbox for `boolean`, a text box otherwise.
 */
export type SettingValueType = 'string' | 'integer' | 'decimal' | 'boolean' | 'time' | 'json';

/**
 * One tuning knob as the settings screen reads it (GET /api/settings), mirroring `SettingItem`.
 * `value` is the stored text, exactly as the column holds it, which the screen parses by `value_type`.
 * `description` is the seeded, locale-neutral note the screen falls back to as a label for any key it
 * does not itself name.
 */
export interface SettingItem {
  key: string;
  value: string;
  value_type: SettingValueType;
  description: string | null;
}

/** Every settings row, ordered by key (GET /api/settings), mirroring `SettingsListResponse`. */
export interface SettingsListResponse {
  items: SettingItem[];
}

/**
 * The body of PATCH /api/settings: the knobs to change, keyed by name, each a new text value in the
 * same untyped form the row stores. Only the keys present are touched; a key that is not already a
 * setting is refused by the server.
 */
export interface SettingsUpdate {
  updates: Record<string, string>;
}

// --------------------------------------------------------------------------- reports & dashboards (Requirement 18)

/**
 * The filters a report was run with, echoed back so a response is self-describing (Requirement 18.7),
 * mirroring `ReportFiltersApplied`. `year` and `month` are always the period; the rest are the
 * optional narrowings the profitability report accepts (Requirement 18.4), null when unused so a
 * reader can tell an unfiltered figure from a filtered one.
 */
export interface ReportFiltersApplied {
  year: number;
  month: number;
  employee_id: string | null;
  site_id: string | null;
  client_id: string | null;
  project: string | null;
}

/**
 * Total billing, employee cost and gross profit for the selected filters (GET /api/reports/profitability),
 * mirroring `ProfitabilityResponse` (Requirement 18.4). Money is raw decimal strings the screen
 * formats; `site_count` states how many sites contributed, so a filtered figure carries its own
 * breadth. The envelope (`year`, `month`, `filters`, `currency`) states the period and filters
 * applied and the currency ILS (Requirement 18.7).
 */
export interface ProfitabilityReport {
  year: number;
  month: number;
  filters: ReportFiltersApplied;
  currency: string;
  total_billing: string;
  total_cost: string;
  total_profit: string;
  site_count: number;
}

/**
 * One employee's hours (and, for a finance reader, cost) for a month (GET /api/reports/by-employee),
 * mirroring `EmployeeReportRow`. The four bucket minute totals and their sum are integers the screen
 * renders through `formatDuration`; both employee name forms are carried so a row labels in either
 * language (Requirement 21.5). `cost` is a raw decimal string for a finance caller and `null` for a
 * site manager, whose payload the server strips of wage — a null means "not permitted to see", so the
 * screen omits the cost entirely rather than showing a zero.
 */
export interface EmployeeReportRow {
  employee_id: string;
  employee_name: string;
  employee_name_en: string;
  employee_number: string | null;
  regular_minutes: number;
  overtime_minutes: number;
  shabbat_minutes: number;
  holiday_minutes: number;
  total_minutes: number;
  cost: string | null;
}

/**
 * Each employee's hours (and cost) for the selected month (GET /api/reports/by-employee), mirroring
 * `EmployeeReportResponse`. The envelope (`year`, `month`, `filters`, `currency`) states the period
 * and filters applied and the currency ILS, so the response — and any export built from it — is
 * self-describing (Requirement 18.7). `total_cost`, like each row's `cost`, is a raw decimal string
 * for a finance reader and `null` for a site manager; when it is null the screen shows and exports no
 * cost at all, keeping the manager view wage-free.
 */
export interface EmployeeReport {
  year: number;
  month: number;
  filters: ReportFiltersApplied;
  currency: string;
  rows: EmployeeReportRow[];
  total_minutes: number;
  total_cost: string | null;
}

/**
 * The attention counts the administrator home dashboard heads (Requirement 18.6), mirroring
 * `DashboardAttentionResponse`. Three current-month counts — employees without a check-out, without a
 * check-in, and days missing entirely — each of which the dashboard links to the missing-report list
 * filtered to that kind.
 */
export interface DashboardAttention {
  missing_checkout: number;
  missing_checkin: number;
  missing_reports: number;
}

/**
 * The administrator home dashboard for the current month (GET /api/reports/dashboard), mirroring
 * `DashboardResponse` (Requirement 18.5, 18.6). `year` and `month` name the current month on the
 * server; `active_employees` and `active_sites` are current counts; `total_minutes` is the month's
 * billable minutes the screen renders as hours; `total_billing`, `total_cost` and `total_profit` are
 * the month's finance figures (raw decimal strings the screen formats), taken from the by-site report
 * so the dashboard reconciles with it (Requirement 18.9). `attention` heads the call-to-action
 * section. Money is ILS (Requirement 18.7). The whole payload is finance data — the finance-role
 * guard on the server keeps it from the wrong reader (Requirement 2.5).
 */
export interface DashboardReport {
  year: number;
  month: number;
  currency: string;
  active_employees: number;
  active_sites: number;
  total_minutes: number;
  total_billing: string;
  total_cost: string;
  total_profit: string;
  attention: DashboardAttention;
}

/** The three kinds of missing time report (Requirement 14.3), mirroring `MissingReportKind`. */
export type MissingReportKind = 'missing_checkout' | 'missing_checkin' | 'both_missing';

/**
 * One missing-report finding (GET /api/reports/missing-reports), mirroring
 * `MissingReportFindingResponse` (Requirement 14.3). Both employee name forms are carried so the row
 * labels in either language; `site_name` names the expected site. Locale-neutral: `work_date` is an
 * ISO date the screen formats.
 */
export interface MissingReportFinding {
  employee_id: string;
  employee_name: string;
  employee_name_en: string;
  employee_number: string | null;
  work_date: string;
  site_id: string;
  site_name: string;
  kind: MissingReportKind;
}

/**
 * The missing-report findings for a date range (GET /api/reports/missing-reports), mirroring
 * `MissingReportsResponse` (Requirement 14.3). Carries the range and filters so the response is
 * self-describing; `total` is the number of findings.
 */
export interface MissingReportsResult {
  date_from: string;
  date_to: string;
  employee_id: string | null;
  site_id: string | null;
  findings: MissingReportFinding[];
  total: number;
}

// --------------------------------------------------------------------------- global search (Requirement 22)

/**
 * One employee matched by the global search (GET /api/search), mirroring `EmployeeSearchHit`. Both
 * name forms are carried so a row labels in either language (Requirement 21.5); no wage or sensitive
 * field appears, so a hit is safe for any reader.
 */
export interface EmployeeSearchHit {
  id: string;
  full_name: string;
  full_name_en: string;
  status: EmployeeStatus;
}

/** One site matched by the search, mirroring `SiteSearchHit`. `site_number` is the value it matched. */
export interface SiteSearchHit {
  id: string;
  name: string;
  site_number: string;
  client_id: string;
  status: SiteStatus;
}

/** One client matched by the search, mirroring `ClientSearchHit`. */
export interface ClientSearchHit {
  id: string;
  name: string;
  company: string | null;
}

/** A page of hits of one kind, with the total that matched before paging (Requirement 22.5). */
export interface SearchGroup<Hit> {
  items: Hit[];
  total: number;
  limit: number;
  offset: number;
}

/**
 * The grouped result of a global search (GET /api/search), mirroring `SearchResponse`
 * (Requirement 22.1). `query` echoes the term the result ran for. Each group is scoped to the caller
 * and paged independently.
 */
export interface SearchResults {
  query: string;
  employees: SearchGroup<EmployeeSearchHit>;
  sites: SearchGroup<SiteSearchHit>;
  clients: SearchGroup<ClientSearchHit>;
}

// --------------------------------------------------------------------------- staffing companies

export interface StaffingCompanyListItem {
  id: string;
  name: string;
  contact_person: string;
  hourly_rate: string | null;
}

export interface StaffingCompanyResponse {
  id: string;
  name: string;
  contact_person: string;
  hourly_rate: string | null;
  telephone: string | null;
  comments: string | null;
  created_at: string;
  updated_at: string;
}

export interface StaffingCompanyCreate {
  name: string;
  contact_person: string;
  hourly_rate?: string | null;
  telephone?: string | null;
  comments?: string | null;
}

export type StaffingCompanyUpdate = Partial<StaffingCompanyCreate>;

/** The by-staffing-company report: total hours and payment (null payment => no rate set). */
export interface StaffingCompanyReport {
  company_id: string;
  company_name: string;
  hourly_rate: string | null;
  total_minutes: number;
  total_payment: string | null;
}