// One labelled input of the credential forms (R23.1, R23.2): a visible `<label htmlFor>` that carries the
// "(required)" text, a hint, an input with `aria-required`, and, when the field failed, the message with
// `aria-invalid` and an `aria-describedby` that lists the message first, then the hint.
// Pure presentation: every string arrives already translated, so the component holds no text and no state.
import type { ChangeEvent } from 'react';
import { describedBy } from './authFeedback';

export interface AuthFieldProps {
  id: string;
  name: string;
  label: string;
  /** The "(required)" text, with the space that separates it from the label. */
  requiredText: string;
  hint: string;
  hintId: string;
  errorId: string;
  /** The visible error line of this field, or null when the field is valid. */
  error: string | null;
  /** Id of a form-level message that also describes this field, or null. */
  formErrorId: string | null;
  type: 'tel' | 'password';
  autoComplete: string;
  maxLength?: number | undefined;
  /** `ltr` for the phone number, so its digits are not reordered in right-to-left text (R8.8). */
  dir?: 'ltr' | undefined;
  value: string;
  onChange: (value: string) => void;
}

export function AuthField(props: AuthFieldProps) {
  const invalid = props.error !== null;
  return (
    <div className="portal-field">
      <label htmlFor={props.id}>
        {props.label}
        <span className="portal-required">{props.requiredText}</span>
      </label>
      <span id={props.hintId} className="portal-hint">
        {props.hint}
      </span>
      <input
        id={props.id}
        name={props.name}
        type={props.type}
        inputMode="numeric"
        autoComplete={props.autoComplete}
        maxLength={props.maxLength}
        dir={props.dir}
        value={props.value}
        aria-required="true"
        aria-invalid={invalid ? 'true' : undefined}
        aria-describedby={describedBy([invalid && props.errorId, props.formErrorId, props.hintId])}
        onChange={(event: ChangeEvent<HTMLInputElement>) => props.onChange(event.target.value)}
      />
      {invalid && (
        <p id={props.errorId} className="portal-error">
          {props.error}
        </p>
      )}
    </div>
  );
}
