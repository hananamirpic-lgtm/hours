"""Machine error codes of the service ticket system (design.md, "Error code catalogue"; R8.12).

The server sends codes, never messages. The front end translates `detail.error.code` through the
`errors.<code>` keys of the `service` locale namespace, and a test asserts that every code in
`ERROR_CODES` has both translations. `ERROR_CODES` is therefore the single list: add a code here
(constant and list entry) and the translation test demands its text.

A few codes are shared with the Hours API and keep the value Hours already uses.
"""

from __future__ import annotations

# --- Access
CODE_NOT_AUTHENTICATED = "not_authenticated"
CODE_INVALID_TOKEN = "invalid_token"
CODE_CUSTOMER_TOKEN_NOT_ACCEPTED = "customer_token_not_accepted"
CODE_STAFF_TOKEN_NOT_ACCEPTED = "staff_token_not_accepted"
CODE_INSUFFICIENT_ROLE = "insufficient_role"
CODE_PASSWORD_CHANGE_REQUIRED = "password_change_required"
CODE_TOTP_ENROLMENT_REQUIRED = "totp_enrolment_required"

# --- Sign-in
CODE_INVALID_CREDENTIALS = "invalid_credentials"
CODE_INVALID_PHONE = "invalid_phone"
CODE_TOO_MANY_ATTEMPTS = "too_many_attempts"
CODE_RATE_LIMITED = "rate_limited"

# --- Password
CODE_INVALID_PASSWORD_FORMAT = "invalid_password_format"
CODE_PASSWORD_NOT_CHANGED = "password_not_changed"

# --- Customers
CODE_PHONE_ALREADY_REGISTERED = "phone_already_registered"
CODE_CUSTOMER_NOT_FOUND = "customer_not_found"
CODE_CUSTOMER_INACTIVE = "customer_inactive"
CODE_INVALID_FULL_NAME = "invalid_full_name"
CODE_INVALID_CITY = "invalid_city"
CODE_INVALID_APARTMENT = "invalid_apartment"

# --- Tickets
CODE_INVALID_CATEGORY = "invalid_category"
CODE_INVALID_DESCRIPTION = "invalid_description"
CODE_INVALID_URGENT = "invalid_urgent"
CODE_UNKNOWN_FIELD = "unknown_field"
CODE_TICKET_NOT_FOUND = "ticket_not_found"
CODE_INVALID_TRANSITION = "invalid_transition"
CODE_TICKET_STATUS_CHANGED = "ticket_status_changed"
CODE_INVALID_EXPECTED_STATUS = "invalid_expected_status"
CODE_INVALID_TARGET_STATUS = "invalid_target_status"
CODE_INVALID_ASSIGNED_TECHNICIAN_NAME = "invalid_assigned_technician_name"
CODE_TICKET_NUMBERS_EXHAUSTED = "ticket_numbers_exhausted"

# --- Photo
CODE_INVALID_PHOTO_TYPE = "invalid_photo_type"
CODE_PHOTO_DIMENSIONS_TOO_LARGE = "photo_dimensions_too_large"
CODE_PHOTO_UPLOAD_DISABLED = "photo_upload_disabled"
CODE_TOO_MANY_PHOTOS = "too_many_photos"
CODE_PHOTO_TOO_LARGE = "photo_too_large"
CODE_REQUEST_TOO_LARGE = "request_too_large"

# --- History
CODE_INVALID_HISTORY_TEXT = "invalid_history_text"
CODE_INVALID_TECHNICIAN_NAME = "invalid_technician_name"
CODE_HISTORY_ENTRY_NOT_FOUND = "history_entry_not_found"

# --- Lists
CODE_INVALID_PAGE = "invalid_page"
CODE_INVALID_TICKET_NUMBER = "invalid_ticket_number"
CODE_INVALID_DATE_RANGE = "invalid_date_range"
CODE_INVALID_FILTER = "invalid_filter"

# --- Config
CODE_INVALID_NAME_HE = "invalid_name_he"
CODE_INVALID_NAME_EN = "invalid_name_en"
CODE_INVALID_LABEL_HE = "invalid_label_he"
CODE_INVALID_LABEL_EN = "invalid_label_en"
CODE_DUPLICATE_CATEGORY = "duplicate_category"
CODE_LAST_ACTIVE_CATEGORY = "last_active_category"
CODE_CATEGORY_NOT_FOUND = "category_not_found"

# --- Server
CODE_SERVICE_UNAVAILABLE = "service_unavailable"
CODE_PHOTO_STORAGE_UNAVAILABLE = "photo_storage_unavailable"

ERROR_CODES: tuple[str, ...] = (
    # Access
    CODE_NOT_AUTHENTICATED,
    CODE_INVALID_TOKEN,
    CODE_CUSTOMER_TOKEN_NOT_ACCEPTED,
    CODE_STAFF_TOKEN_NOT_ACCEPTED,
    CODE_INSUFFICIENT_ROLE,
    CODE_PASSWORD_CHANGE_REQUIRED,
    CODE_TOTP_ENROLMENT_REQUIRED,
    # Sign-in
    CODE_INVALID_CREDENTIALS,
    CODE_INVALID_PHONE,
    CODE_TOO_MANY_ATTEMPTS,
    CODE_RATE_LIMITED,
    # Password
    CODE_INVALID_PASSWORD_FORMAT,
    CODE_PASSWORD_NOT_CHANGED,
    # Customers
    CODE_PHONE_ALREADY_REGISTERED,
    CODE_CUSTOMER_NOT_FOUND,
    CODE_CUSTOMER_INACTIVE,
    CODE_INVALID_FULL_NAME,
    CODE_INVALID_CITY,
    CODE_INVALID_APARTMENT,
    # Tickets
    CODE_INVALID_CATEGORY,
    CODE_INVALID_DESCRIPTION,
    CODE_INVALID_URGENT,
    CODE_UNKNOWN_FIELD,
    CODE_TICKET_NOT_FOUND,
    CODE_INVALID_TRANSITION,
    CODE_TICKET_STATUS_CHANGED,
    CODE_INVALID_EXPECTED_STATUS,
    CODE_INVALID_TARGET_STATUS,
    CODE_INVALID_ASSIGNED_TECHNICIAN_NAME,
    CODE_TICKET_NUMBERS_EXHAUSTED,
    # Photo
    CODE_INVALID_PHOTO_TYPE,
    CODE_PHOTO_DIMENSIONS_TOO_LARGE,
    CODE_PHOTO_UPLOAD_DISABLED,
    CODE_TOO_MANY_PHOTOS,
    CODE_PHOTO_TOO_LARGE,
    CODE_REQUEST_TOO_LARGE,
    # History
    CODE_INVALID_HISTORY_TEXT,
    CODE_INVALID_TECHNICIAN_NAME,
    CODE_HISTORY_ENTRY_NOT_FOUND,
    # Lists
    CODE_INVALID_PAGE,
    CODE_INVALID_TICKET_NUMBER,
    CODE_INVALID_DATE_RANGE,
    CODE_INVALID_FILTER,
    # Config
    CODE_INVALID_NAME_HE,
    CODE_INVALID_NAME_EN,
    CODE_INVALID_LABEL_HE,
    CODE_INVALID_LABEL_EN,
    CODE_DUPLICATE_CATEGORY,
    CODE_LAST_ACTIVE_CATEGORY,
    CODE_CATEGORY_NOT_FOUND,
    # Server
    CODE_SERVICE_UNAVAILABLE,
    CODE_PHOTO_STORAGE_UNAVAILABLE,
)
