/**
 * Page controls for a list. The list endpoints page by limit/offset and return a total, so this shows
 * the current window and steps by a page at a time. The range readout is bidi-isolated so its digits
 * do not reorder against Hebrew text.
 */

import { useTranslation } from 'react-i18next';

export function Pagination({
  total,
  limit,
  offset,
  onOffsetChange,
}: {
  total: number;
  limit: number;
  offset: number;
  onOffsetChange: (offset: number) => void;
}) {
  const { t } = useTranslation();
  const first = total === 0 ? 0 : offset + 1;
  const last = Math.min(offset + limit, total);
  const canPrev = offset > 0;
  const canNext = offset + limit < total;

  return (
    <div className="pagination">
      <span className="pagination__info">
        {t('pagination.range', { first, last, total })}
      </span>
      <button
        type="button"
        className="button button--small"
        disabled={!canPrev}
        onClick={() => onOffsetChange(Math.max(0, offset - limit))}
      >
        {t('pagination.previous')}
      </button>
      <button
        type="button"
        className="button button--small"
        disabled={!canNext}
        onClick={() => onOffsetChange(offset + limit)}
      >
        {t('pagination.next')}
      </button>
    </div>
  );
}
