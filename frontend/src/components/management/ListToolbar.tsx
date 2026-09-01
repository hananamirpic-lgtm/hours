/**
 * The toolbar above a list: a search box and any number of filter selects. Search matches on the
 * client over the loaded page; the filters (status, archived) are query parameters the list re-fetches
 * on. Both are controlled by the parent screen so the state lives with the query.
 */

import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

export function ListToolbar({
  search,
  onSearchChange,
  children,
}: {
  search: string;
  onSearchChange: (value: string) => void;
  children?: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div className="toolbar">
      <input
        className="input toolbar__search"
        type="search"
        placeholder={t('list.searchPlaceholder')}
        aria-label={t('list.search')}
        value={search}
        onChange={(event) => onSearchChange(event.target.value)}
      />
      {children}
    </div>
  );
}
