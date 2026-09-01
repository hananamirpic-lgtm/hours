/**
 * The console's global search (Requirement 22.1, 22.2, 22.4, 22.5, 21.5).
 *
 * A single box in the top bar that searches employees, sites and clients at once. The server does the
 * matching and the scoping — a partial name in either language, a site number, a client phone and a
 * passport number, case- and accent-insensitive (Requirement 22.4, 21.5), limited to what the caller
 * may see (Requirement 22.2) — so this component only debounces the term, shows the grouped result in
 * a popover, and lets the keyboard drive it.
 *
 * The whole result is one navigable list across the three headings, the way a command palette works:
 * Down and Up move the highlight through every hit regardless of its group and wrap at the ends,
 * Enter opens the highlighted one, and Escape closes the popover (Requirement 22.5). Pointer and
 * keyboard stay in agreement because both read the same flattened list from `searchView`, which is
 * where the ordering and the arrow arithmetic are tested. Choosing a hit navigates to the console
 * screen that shows it, carrying the id so that screen can open it.
 *
 * Every visible string is a translation key and the layout uses logical properties, so the box and
 * its popover sit correctly and read correctly in both Hebrew and English with no direction-specific
 * code here.
 */

import { useQuery } from '@tanstack/react-query';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';

import { search, searchKeys } from '@/api/search';
import { useLanguage } from '@/lib/useLanguage';
import {
  flattenResults,
  moveActive,
  type HitKind,
  type RenderGroup,
  type SearchItem,
} from '@/app/search/searchView';

/** How many hits per group to fetch — a popover shows a handful; the group total states the rest. */
const PER_GROUP = 6;
/** Milliseconds to wait after the last keystroke before searching, so typing does not spam the API. */
const DEBOUNCE_MS = 200;

/** The heading label key for each kind. */
const GROUP_LABEL: Record<HitKind, string> = {
  employee: 'search.groupEmployees',
  site: 'search.groupSites',
  client: 'search.groupClients',
};

export function GlobalSearch() {
  const { t } = useTranslation();
  const language = useLanguage();
  const navigate = useNavigate();

  const [term, setTerm] = useState('');
  const [debounced, setDebounced] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState<number | null>(null);

  const containerRef = useRef<HTMLDivElement>(null);
  const listId = useId();

  // Debounce the term: a search only runs once typing pauses, and a blank term never hits the API —
  // the backend would return empty groups for it anyway (Requirement 22.5 guardrail).
  useEffect(() => {
    const handle = window.setTimeout(() => setDebounced(term.trim()), DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
  }, [term]);

  const params = useMemo(() => ({ q: debounced, limit: PER_GROUP, offset: 0 }), [debounced]);
  const query = useQuery({
    queryKey: searchKeys.query(params),
    queryFn: () => search(params),
    enabled: debounced.length > 0,
  });

  const flat = useMemo(() => flattenResults(query.data, language), [query.data, language]);

  // The highlight cannot outlive the list it points into: whenever the results change, clear it so a
  // stale index never selects the wrong row.
  useEffect(() => {
    setActive(null);
  }, [flat.count, debounced]);

  // Close the popover on a click outside it, the ordinary dismissal for a floating panel.
  useEffect(() => {
    if (!open) {
      return;
    }
    const onPointerDown = (event: PointerEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('pointerdown', onPointerDown);
    return () => document.removeEventListener('pointerdown', onPointerDown);
  }, [open]);

  const choose = (item: SearchItem) => {
    setOpen(false);
    setTerm('');
    setDebounced('');
    navigate(item.to);
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        setOpen(true);
        setActive((current) => moveActive(current, 1, flat.count));
        break;
      case 'ArrowUp':
        event.preventDefault();
        setOpen(true);
        setActive((current) => moveActive(current, -1, flat.count));
        break;
      case 'Enter': {
        if (active !== null && flat.items[active]) {
          event.preventDefault();
          choose(flat.items[active]);
        }
        break;
      }
      case 'Escape':
        setOpen(false);
        setActive(null);
        break;
    }
  };

  const showPopover = open && debounced.length > 0;
  const activeItem = active !== null ? flat.items[active] : undefined;

  return (
    <div className="global-search" ref={containerRef}>
      <input
        type="search"
        className="input global-search__input"
        role="combobox"
        aria-expanded={showPopover}
        aria-controls={listId}
        aria-activedescendant={activeItem ? `${listId}-${activeItem.key}` : undefined}
        aria-label={t('search.label')}
        placeholder={t('search.placeholder')}
        value={term}
        onChange={(event) => {
          setTerm(event.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
      />

      {showPopover ? (
        <div className="global-search__popover" id={listId} role="listbox" aria-label={t('search.results')}>
          {query.isPending ? (
            <p className="global-search__note">{t('common.loading')}</p>
          ) : query.isError ? (
            <p className="global-search__note">{t('common.loadError')}</p>
          ) : flat.count === 0 ? (
            <p className="global-search__note">{t('search.empty')}</p>
          ) : (
            flat.groups.map((group) => (
              <SearchGroupBlock
                key={group.kind}
                group={group}
                listId={listId}
                activeIndex={active}
                onChoose={choose}
                onHover={setActive}
              />
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

/** One heading and its hits, with a count that states the total behind the shown page. */
function SearchGroupBlock({
  group,
  listId,
  activeIndex,
  onChoose,
  onHover,
}: {
  group: RenderGroup;
  listId: string;
  activeIndex: number | null;
  onChoose: (item: SearchItem) => void;
  onHover: (index: number) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="global-search__group">
      <div className="global-search__group-head">
        <span className="global-search__group-label">{t(GROUP_LABEL[group.kind])}</span>
        <span className="global-search__group-count">
          {t('search.count', { count: group.total })}
        </span>
      </div>
      <ul className="global-search__options">
        {group.items.map((item) => (
          <li
            key={item.key}
            id={`${listId}-${item.key}`}
            role="option"
            aria-selected={activeIndex === item.index}
            className={
              activeIndex === item.index
                ? 'global-search__option global-search__option--active'
                : 'global-search__option'
            }
            onMouseEnter={() => onHover(item.index)}
            onClick={() => onChoose(item)}
          >
            <span className="global-search__option-label">{item.label}</span>
            {item.detail ? (
              <span className="global-search__option-detail">{item.detail}</span>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
