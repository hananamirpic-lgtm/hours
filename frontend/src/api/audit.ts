/**
 * Audit-history endpoint — the per-entity change log (Requirement 13.4, 13.6).
 *
 * `GET /api/audit?entity_type=&entity_id=` returns the change history of one entity, newest first, so
 * the employee, site and time-entry screens can show who changed what, when and why. The server scopes
 * the read — an administrator reads any entity's audit, a site manager only entities within their
 * assigned sites (Requirement 13.6), and it answers a 403 when the entity is out of scope — so nothing
 * here decides access; the panel renders what the server returns and surfaces the refusal.
 *
 * Read-only by design: the audit trail is append-only (Requirement 13.3), so there is no create,
 * update or delete call in this module and there never will be. Every value is locale-neutral — the
 * server returns the field, the old and new values as stable strings, the actor, the timestamp and the
 * reason, and the screen composes the readable sentence in the reader's language.
 */

import type { AuditEntityType, AuditEntry, Paginated } from './types';

import { apiFetch } from './client';

export const auditKeys = {
  all: ['audit'] as const,
  list: (params: AuditListParams) => ['audit', 'list', params] as const,
  global: (params: AuditGlobalParams) => ['audit', 'global', params] as const,
};

/** Which entity's history to read, and the page window. */
export interface AuditListParams {
  entity_type: AuditEntityType;
  entity_id: string;
  limit: number;
  offset: number;
}

/**
 * The global audit feed's filters and page window (Requirement 13.4). Every filter is optional: an
 * absent `entityType` reads every entity type, and an absent date bound leaves that end of the range
 * open. The dates are ISO calendar days (`YYYY-MM-DD`); the server treats the range as inclusive at
 * both ends. This endpoint is administrator-only — the feed is polymorphic and many rows have no
 * site, so it cannot be scoped to a site manager — and answers a 403 to anyone else.
 */
export interface AuditGlobalParams {
  entityType?: string | null;
  dateFrom?: string | null;
  dateTo?: string | null;
  limit: number;
  offset: number;
}

const buildQuery = (params: AuditListParams): string => {
  const search = new URLSearchParams();
  search.set('entity_type', params.entity_type);
  search.set('entity_id', params.entity_id);
  search.set('limit', String(params.limit));
  search.set('offset', String(params.offset));
  return search.toString();
};

const buildGlobalQuery = (params: AuditGlobalParams): string => {
  const search = new URLSearchParams();
  if (params.entityType) {
    search.set('entity_type', params.entityType);
  }
  if (params.dateFrom) {
    search.set('date_from', params.dateFrom);
  }
  if (params.dateTo) {
    search.set('date_to', params.dateTo);
  }
  search.set('limit', String(params.limit));
  search.set('offset', String(params.offset));
  return search.toString();
};

/** A page of audit entries for one entity, newest first (Requirement 13.4). */
export const listEntityAudit = (params: AuditListParams): Promise<Paginated<AuditEntry>> =>
  apiFetch<Paginated<AuditEntry>>(`/audit?${buildQuery(params)}`);

/**
 * A page of the global audit feed — recent changes across every entity, newest first (Req 13.4).
 *
 * Administrator-only on the server; a site manager, accounting or employee caller gets a 403, which
 * the audit page surfaces as a "not permitted" note rather than a generic failure.
 */
export const listAllAudit = (params: AuditGlobalParams): Promise<Paginated<AuditEntry>> =>
  apiFetch<Paginated<AuditEntry>>(`/audit/all?${buildGlobalQuery(params)}`);
