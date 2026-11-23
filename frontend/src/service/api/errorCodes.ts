// Every error code the Ticket_API can return (R8.12). Hand-written mirror of `ERROR_CODES` in
// backend/app/service_tickets/error_codes.py, same order. errorCodes.test.ts compares the two when the
// backend file is reachable. Add a code in both places; the i18n test then demands its text.

export const ERROR_CODES = [
  // Access
  'not_authenticated',
  'invalid_token',
  'customer_token_not_accepted',
  'staff_token_not_accepted',
  'insufficient_role',
  'password_change_required',
  'totp_enrolment_required',
  // Sign-in
  'invalid_credentials',
  'invalid_phone',
  'too_many_attempts',
  'rate_limited',
  // Password
  'invalid_password_format',
  'password_not_changed',
  // Customers
  'phone_already_registered',
  'customer_not_found',
  'customer_inactive',
  'invalid_full_name',
  'invalid_city',
  'invalid_apartment',
  // Tickets
  'invalid_category',
  'invalid_description',
  'invalid_urgent',
  'unknown_field',
  'ticket_not_found',
  'invalid_transition',
  'ticket_status_changed',
  'invalid_expected_status',
  'invalid_target_status',
  'invalid_assigned_technician_name',
  'ticket_numbers_exhausted',
  // Photo
  'invalid_photo_type',
  'photo_dimensions_too_large',
  'photo_upload_disabled',
  'too_many_photos',
  'photo_too_large',
  'request_too_large',
  // History
  'invalid_history_text',
  'invalid_technician_name',
  'history_entry_not_found',
  // Lists
  'invalid_page',
  'invalid_ticket_number',
  'invalid_date_range',
  'invalid_filter',
  // Config
  'invalid_name_he',
  'invalid_name_en',
  'invalid_label_he',
  'invalid_label_en',
  'duplicate_category',
  'last_active_category',
  'category_not_found',
  // Server
  'service_unavailable',
  'photo_storage_unavailable',
] as const;

export type ErrorCode = (typeof ERROR_CODES)[number];

const KNOWN: ReadonlySet<string> = new Set<string>(ERROR_CODES);

/** True when `code` is one of the codes the backend defines. */
export function isErrorCode(code: unknown): code is ErrorCode {
  return typeof code === 'string' && KNOWN.has(code);
}
