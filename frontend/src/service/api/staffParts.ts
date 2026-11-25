/**
 * The list of active replaceable parts for the Resolve form (R29.4), readable by every Ticket_Staff member.
 *
 * `GET /staff/parts` is the Config_Staff list (active and inactive parts), so a technician gets 403 there. The
 * Resolve form needs the active parts for any ticket staff member, so it reads the read-only route
 * `GET /staff/parts/active`, whose answer has the same shape: `{ items: [{ id, name_he, name_en, is_active }] }`,
 * active parts only, oldest first.
 *
 * It sits on `apiFetch` and throws `StaffApiError` like `staff.ts`, whose helpers it reuses. It is a separate
 * file so the shared client stays untouched while other screens change it.
 */
import { ApiError, apiFetch, type RequestOptions } from '@/api/client';
import { ABORTED_CODE, GENERIC_CODE, NETWORK_CODE } from './portalFetch';
import { STAFF_PREFIX, StaffApiError, isErrorBody, staffErrorFromBody, staffKeys } from './staff';

/** One part of the picker. */
export interface ActivePart {
  readonly id: string;
  readonly name_he: string;
  readonly name_en: string;
  readonly is_active: boolean;
}

/** The path under `/service/staff` of the read-only list of active parts. */
export const ACTIVE_PARTS_PATH = '/parts/active';

/** The query key of the list. It sits under `staffKeys.all`, so a staff-wide invalidation reaches it. */
export const activePartsKey = [...staffKeys.all, 'parts', 'active'] as const;

/** Every 4xx and 5xx status, so `apiFetch` hands back the body of an error answer instead of dropping it. */
const ERROR_STATUSES: readonly number[] = Array.from({ length: 200 }, (_, index) => 400 + index);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * The parts of an answer body, in the order given, active ones only. A body that is not `{ items: [...] }`, or
 * an item without an id and two names, is an unusable answer (`generic`). An item marked inactive is dropped,
 * so the picker never offers a part the server would refuse.
 */
export function activePartsFromBody(body: unknown): ActivePart[] {
  if (!isRecord(body) || !Array.isArray(body.items)) throw new StaffApiError(GENERIC_CODE);
  const parts: ActivePart[] = [];
  for (const item of body.items as unknown[]) {
    if (
      !isRecord(item) ||
      typeof item.id !== 'string' ||
      typeof item.name_he !== 'string' ||
      typeof item.name_en !== 'string'
    ) {
      throw new StaffApiError(GENERIC_CODE);
    }
    if (item.is_active === false) continue;
    parts.push({ id: item.id, name_he: item.name_he, name_en: item.name_en, is_active: true });
  }
  return parts;
}

/** `GET /staff/parts/active`: the active parts, for the parts selection dialog of the Resolve form. */
export async function listActiveParts(signal?: AbortSignal): Promise<readonly ActivePart[]> {
  const init: RequestOptions = { method: 'GET', acceptStatuses: [...ERROR_STATUSES] };
  if (signal) init.signal = signal;
  let body: unknown;
  try {
    body = await apiFetch<unknown>(`${STAFF_PREFIX}${ACTIVE_PARTS_PATH}`, init);
  } catch (caught) {
    if (caught instanceof ApiError) throw new StaffApiError(caught.code ?? GENERIC_CODE);
    throw new StaffApiError(signal?.aborted ? ABORTED_CODE : NETWORK_CODE);
  }
  if (isErrorBody(body) || body === null || body === undefined) throw staffErrorFromBody(body);
  return activePartsFromBody(body);
}
