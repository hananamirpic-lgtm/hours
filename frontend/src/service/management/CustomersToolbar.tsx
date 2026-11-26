// The search and filter bar of the Customers screen (task 24.5; R3.10, R23.12). One search box (name or phone
// substring, applied with the Search button or Enter) and one state filter (all, active only, inactive only),
// combined by AND on the server. Both controls have a visible label tied to them.
import type { FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { ACTIVE_FILTERS, SEARCH_MAX_LENGTH, type ActiveFilter } from './customersModel';
import { SERVICE_NAMESPACE } from './namespace';
import './customers.css';

export const TOOLBAR_IDS = {
  search: 'customers-search',
  filter: 'customers-filter',
} as const;

const FILTER_KEYS: Readonly<Record<ActiveFilter, string>> = {
  all: 'management.customers.filterAll',
  active: 'management.customers.filterActive',
  inactive: 'management.customers.filterInactive',
};

export interface CustomersToolbarProps {
  /** The text in the search box. It is applied on submit, not on every key. */
  readonly searchDraft: string;
  readonly active: ActiveFilter;
  /** True when a search or a filter narrows the list, so "clear filters" is offered. */
  readonly canClear: boolean;
  readonly onSearchDraftChange: (value: string) => void;
  readonly onSearchSubmit: () => void;
  readonly onActiveChange: (active: ActiveFilter) => void;
  readonly onClear: () => void;
}

export function CustomersToolbar({
  searchDraft,
  active,
  canClear,
  onSearchDraftChange,
  onSearchSubmit,
  onActiveChange,
  onClear,
}: CustomersToolbarProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const onSubmit = (event: FormEvent): void => {
    event.preventDefault();
    onSearchSubmit();
  };
  return (
    <form className="toolbar customers-toolbar" role="search" onSubmit={onSubmit}>
      <div className="field customers-toolbar__search">
        <label className="field__label" htmlFor={TOOLBAR_IDS.search}>
          {t('management.customers.searchLabel')}
        </label>
        <input
          id={TOOLBAR_IDS.search}
          className="input"
          type="search"
          autoComplete="off"
          maxLength={SEARCH_MAX_LENGTH}
          value={searchDraft}
          onChange={(event) => onSearchDraftChange(event.target.value)}
        />
      </div>
      <div className="field">
        <label className="field__label" htmlFor={TOOLBAR_IDS.filter}>
          {t('management.customers.filterLabel')}
        </label>
        <select
          id={TOOLBAR_IDS.filter}
          className="input"
          value={active}
          onChange={(event) => onActiveChange(event.target.value as ActiveFilter)}
        >
          {ACTIVE_FILTERS.map((value) => (
            <option key={value} value={value}>
              {t(FILTER_KEYS[value])}
            </option>
          ))}
        </select>
      </div>
      <div className="customers-toolbar__buttons">
        <button type="submit" className="button button--primary">
          {t('management.common.search')}
        </button>
        {canClear ? (
          <button type="button" className="button" onClick={onClear}>
            {t('management.common.clearFilters')}
          </button>
        ) : null}
      </div>
    </form>
  );
}
