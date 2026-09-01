/**
 * A side drawer for a record card or form. Slides from the inline-end edge, so it comes from the
 * right in English and the left in Hebrew from the one stylesheet. Escape and an overlay click both
 * close it, and focus moves into it on open so keyboard users are not left behind the overlay.
 */

import { useEffect, useRef, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

export function Drawer({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose();
      }
    };
    document.addEventListener('keydown', onKey);
    panelRef.current?.focus();
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="overlay"
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={panelRef}
      >
        <header className="drawer__header">
          <h2 className="drawer__title">{title}</h2>
          <button type="button" className="button button--small" onClick={onClose} aria-label={t('common.close')}>
            {t('common.close')}
          </button>
        </header>
        {children}
      </div>
    </div>
  );
}
