// Wire types of the ticket form (R9.1, R9.3, R9.5, R11.1; backend `FormConfig` in
// backend/app/service_tickets/api/schemas.py). Pure types and constants: no React, no DOM.

/** The seven field labels the Management_Interface can edit (R19). Keys of `labels` in `GET /form-config`. */
export const FORM_LABEL_KEYS = ['city', 'apartment', 'phone', 'category', 'description', 'photo', 'urgent'] as const;
export type FormLabelKey = (typeof FORM_LABEL_KEYS)[number];

/** One active Issue_Category as the form shows it. The server sends them oldest first. */
export interface FormCategory {
  readonly id: string;
  readonly name_he: string;
  readonly name_en: string;
}

/** The Hebrew and English text of one Field_Label. */
export interface LabelTexts {
  readonly he: string;
  readonly en: string;
}

/** The two form options of Ticket_Settings (R19.10). */
export interface FormSettings {
  readonly description_required: boolean;
  readonly photo_upload_enabled: boolean;
}

/** `GET /service/form-config`. */
export interface FormConfig {
  readonly categories: readonly FormCategory[];
  readonly labels: Readonly<Partial<Record<FormLabelKey, LabelTexts>>>;
  readonly settings: FormSettings;
}

/** `POST /service/tickets` answers 201 with only the number (R9.8). */
export interface TicketCreated {
  readonly ticket_number: number;
}
