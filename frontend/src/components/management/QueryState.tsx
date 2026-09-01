/**
 * The three states every list and card share before its data is ready: loading, failed, and (for a
 * list) empty. Centralised so each screen renders them the same way and every message is a
 * translation key.
 */

import { useTranslation } from 'react-i18next';

export function LoadingState() {
  const { t } = useTranslation();
  return (
    <p className="state" aria-busy="true">
      {t('common.loading')}
    </p>
  );
}

export function ErrorState() {
  const { t } = useTranslation();
  return (
    <p className="state" role="alert">
      {t('common.loadError')}
    </p>
  );
}

export function EmptyState({ messageKey }: { messageKey: string }) {
  const { t } = useTranslation();
  return <p className="state">{t(messageKey)}</p>;
}
