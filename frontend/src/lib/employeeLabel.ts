/**
 * The single shared helper for rendering an employee's name with its 4-digit number (Requirement 6).
 *
 * Every place that shows an employee name — reports, time/hours reports, the audit log, admin lists —
 * composes the display string through this one function so the format is identical everywhere:
 * "Full Name (NNNN)" when a number is present, and just "Full Name" when it is not (a record that
 * predates the number, or a payload that omitted it). It is a pure string function: no directional or
 * physical CSS and no bidi control characters, so an RTL layout renders it through the existing
 * logical CSS unchanged.
 */
export function employeeNumberLabel(name: string, employeeNumber?: string | null): string {
  return employeeNumber ? `${name} (${employeeNumber})` : name;
}