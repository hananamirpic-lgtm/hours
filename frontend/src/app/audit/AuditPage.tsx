/**
 * The global audit console (Requirement 13.4).
 *
 * The top-level "Audit" screen: recent changes across every entity, newest first, each rendered as a
 * readable line in the reader's language by `composeAuditLine` from the locale-neutral parts the
 * server returns. Unlike the per-entity `AuditPanel` that mounts on a card, this reads the whole trail
 * through `GET /api/audit/all`, which is administrator-only — an audit row is polymorphic and many
 * kinds of row carry no site, so a cross-entity feed cannot be scoped to a site manager. A caller
 * without the administrator role gets a 403, which this shows as a plain "not permitted" note rather
 * than a load error.
 *
 * Two filters narrow the feed: the entity type (a select over the types the feed surfaces) and a
 * date-from / date-to range (inclusive at both ends, ISO calendar days). Read-only, matching the
 * append-only trail (Requirement 13.3): the page offers no control that could change a row. Every
 * string is a translation key and every date and time goes through the shared locale-aware helpers,
 * so Hebrew renders right-to-left with no change here.
 */

import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { auditKeys, listAllAudit } from '@/api/audit';
import { ApiError } from '@/api/client';
import { composeAuditLine } from '@/app/audit/auditLine';
import { EmptyState, LoadingState } from '@/components/management/QueryState';
import { Pagination } from '@/components/management/Pagination';
import { useLanguage } from '@/lib/useLanguage';

const PAGE_SIZE = 50;

/** The entity types the global feed offers to filter on; an empty value reads them all. */
const ENTITY_TYPES = ['employees', 'sites', 'time_entries', 'users'] as const;

export function AuditPage() {
  const { t } = useTranslation();
  const language = useLanguage();

  const [entityType, setEntityType] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [offset, setOffset] = useState(0);

  // Any filter change returns to the first page: the old offset may point past the smaller result.
  const onFilterChange = <T,>(setter: (value: T) => void) => (value: T) => {
    setter(value);
    setOffset(0);
  };

  const params = useMemo(
    () => ({
      entityType: entityType || null,
      dateFrom: dateFrom || null,
      dateTo: dateTo || null,
      limit: PAGE_SIZE,
      offset,
    }),
    [entityType, dateFrom, dateTo, offset],
  );

  const query = useQuery({
    queryKey: auditKeys.global(params),
    queryFn: () => listAllAudit(params),
  });

  return (
    <section className="page-body">
      <div className="page-header">
        <h1>{t('nav.audit')}</h1>
      </div>
      <p className="subtitle">{t('audit.subtitle')}</p>

      <div className="toolbar">
        <select
          className="select"
          aria-label={t('audit.filterEntityType')}
          value={entityType}
          onChange={(event) => onFilterChange(setEntityType)(event.target.value)}
        >
          <option value="">{t('audit.allEntityTypes')}</option>
          {ENTITY_TYPES.map((value) => (
            <option key={value} value={value}>
              {t(`audit.entityType.${value}`)}
            </option>
          ))}
        </select>

        <label className="field field--inline">
          <span className="field__label">{t('audit.dateFrom')}</span>
          <input
            className="input"
            type="date"
            value={dateFrom}
            onChange={(event) => onFilterChange(setDateFrom)(event.target.value)}
          />
        </label>
        <label className="field field--inline">
          <span className="field__label">{t('audit.dateTo')}</span>
          <input
            className="input"
            type="date"
            value={dateTo}
            onChange={(event) => onFilterChange(setDateTo)(event.target.value)}
          />
        </label>
      </div>

      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        // A 403 is not a failure to retry — the global feed is administrator-only (Requirement 13.4).
        // Say so plainly; anything else is a load error.
        <p
          className="state"
          role={query.error instanceof ApiError && query.error.status === 403 ? undefined : 'alert'}
        >
          {t(
            query.error instanceof ApiError && query.error.status === 403
              ? 'audit.forbiddenGlobal'
              : 'common.loadError',
          )}
        </p>
      ) : query.data.items.length === 0 ? (
        <EmptyState messageKey="audit.empty" />
      ) : (
        <ol className="audit-list">
          {query.data.items.map((entry) => (
            <li key={entry.id} className="audit-entry">
              {composeAuditLine(entry, language, t)}
            </li>
          ))}
        </ol>
      )}

      {query.data ? (
        <Pagination
          total={query.data.total}
          limit={PAGE_SIZE}
          offset={offset}
          onOffsetChange={setOffset}
        />
      ) : null}
    </section>
  );
}
