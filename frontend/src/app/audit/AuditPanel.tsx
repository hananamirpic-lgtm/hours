/**
 * The per-entity audit history panel (Requirement 13.4, 13.6).
 *
 * Mounts on the employee, site and time-entry screens and reads `GET /api/audit` for that entity: a
 * page of changes, newest first, each rendered as a readable line in the reader's language — "30/08
 * 18:42 — Abraham changed check-out from 15:30 to 16:00". The sentence is composed by `composeAuditLine`
 * from the locale-neutral parts the server returns; every string on the screen is a translation key and
 * every date and time goes through the shared locale-aware helpers, so Hebrew renders right-to-left with
 * no change here.
 *
 * Read-only, matching the append-only trail (Requirement 13.3): the panel offers no control that could
 * change a row. Access is the server's decision (Requirement 13.6) — an administrator reads any entity's
 * audit, a site manager only entities within their sites — so a caller outside scope gets a 403, which
 * the panel shows as a plain "not permitted" note rather than a generic failure.
 */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { auditKeys, listEntityAudit } from '@/api/audit';
import type { AuditEntityType } from '@/api/types';
import { composeAuditLine } from '@/app/audit/auditLine';
import { Pagination } from '@/components/management/Pagination';
import { EmptyState, LoadingState } from '@/components/management/QueryState';
import { ApiError } from '@/api/client';
import { useLanguage } from '@/lib/useLanguage';

const PAGE_SIZE = 25;

export function AuditPanel({
  entityType,
  entityId,
}: {
  entityType: AuditEntityType;
  entityId: string;
}) {
  const { t } = useTranslation();
  const language = useLanguage();
  const [offset, setOffset] = useState(0);

  const params = { entity_type: entityType, entity_id: entityId, limit: PAGE_SIZE, offset };
  const query = useQuery({
    queryKey: auditKeys.list(params),
    queryFn: () => listEntityAudit(params),
  });

  if (query.isPending) {
    return <LoadingState />;
  }

  if (query.isError) {
    // A 403 is not a failure to retry — the entity is outside the caller's assigned sites
    // (Requirement 13.6). Say so plainly; anything else is a load error.
    const forbidden = query.error instanceof ApiError && query.error.status === 403;
    return (
      <p className="state" role={forbidden ? undefined : 'alert'}>
        {t(forbidden ? 'audit.forbidden' : 'common.loadError')}
      </p>
    );
  }

  if (query.data.items.length === 0) {
    return <EmptyState messageKey="audit.empty" />;
  }

  return (
    <div className="drawer__section">
      <ol className="audit-list">
        {query.data.items.map((entry) => (
          <li key={entry.id} className="audit-entry">
            {composeAuditLine(entry, language, t)}
          </li>
        ))}
      </ol>
      <Pagination total={query.data.total} limit={PAGE_SIZE} offset={offset} onOffsetChange={setOffset} />
    </div>
  );
}
