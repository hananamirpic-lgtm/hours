/**
 * The not-available page (Requirement 21.5).
 *
 * One text, shown in Hebrew and English together. It holds no sign-in form, no ticket data and no
 * link or control of any kind, and it needs no language switch. It uses no i18n instance and no
 * auth provider, so it renders the same before anything else has loaded.
 */

import type { CSSProperties } from 'react';

import { BILINGUAL_SEPARATOR, NOT_AVAILABLE_TEXT } from './flagGateStrings';

export const pageStyle: CSSProperties = {
  minHeight: '100vh',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: '1rem',
  textAlign: 'center',
  fontFamily: 'system-ui, sans-serif',
};

export function NotAvailable() {
  return (
    <main style={pageStyle}>
      <h1 style={{ fontSize: '1.25rem', fontWeight: 600 }}>
        <span lang="he" dir="rtl">
          {NOT_AVAILABLE_TEXT.he}
        </span>{' '}
        <span aria-hidden="true">{BILINGUAL_SEPARATOR}</span>{' '}
        <span lang="en" dir="ltr">
          {NOT_AVAILABLE_TEXT.en}
        </span>
      </h1>
    </main>
  );
}
