/**
 * Client additions of task 30.10, kept apart from `staff.ts` and `staffTypes.ts`: the settlement filter of the
 * report and its export (`locality_id`), the read of the parts for the "Part replaced" filter, and the helper
 * that saves a downloaded file.
 *
 * `getReport` and `exportReport` of `staff.ts` do not know `locality_id`, so the two functions here build the
 * same request with it. They fail the way the rest of the staff client does: a `StaffApiError` with a code.
 */
import { ApiError, apiFetch, apiFetchBlob, type RequestOptions } from '@/api/client';
import { ABORTED_CODE, GENERIC_CODE, NETWORK_CODE } from './portalFetch';
import {
  STAFF_PREFIX,
  StaffApiError,
  buildQuery,
  isErrorBody,
  staffErrorFromBody,
  type ReportExportLang,
  type ReportQuery,
  type StaffReport,
} from './staff';

// --- the report with the settlement filter (R30.7) -------------------------------------------------

/** The query of `GET /staff/reports`, with the optional settlement filter. */
export interface ReportQueryExt extends ReportQuery {
  /** The id of one settlement (`locality_id`); absent or blank is no restriction. */
  readonly localityId?: string;
}

/** The filters of `GET /staff/reports/export`: those of the report, without the page. */
export type ReportExportQueryExt = Omit<ReportQueryExt, 'page'>;

/** `GET /staff/reports` query string. A blank value is left out. */
export function reportQueryExt(query: ReportQueryExt = {}): string {
  return buildQuery([
    ['page', query.page],
    ['date_from', query.dateFrom],
    ['date_to', query.dateTo],
    ['city', query.city],
    ['locality_id', query.localityId],
    ['category_id', query.categoryId],
    ['part_filter', query.partFilter],
  ]);
}

/** `GET /staff/reports/export` query string: the report's filters in the same order, no page, then `lang`. */
export function exportReportQueryExt(query: ReportExportQueryExt = {}, lang: ReportExportLang = 'he'): string {
  return buildQuery([
    ['date_from', query.dateFrom],
    ['date_to', query.dateTo],
    ['city', query.city],
    ['locality_id', query.localityId],
    ['category_id', query.categoryId],
    ['part_filter', query.partFilter],
    ['lang', lang],
  ]);
}

/** Every 4xx and 5xx status, so `apiFetch` returns the body instead of throwing without a code. */
const ERROR_STATUSES: readonly number[] = Array.from({ length: 200 }, (_, index) => 400 + index);

async function staffGetJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const init: RequestOptions = { method: 'GET', acceptStatuses: [...ERROR_STATUSES] };
  if (signal) init.signal = signal;
  let body: unknown;
  try {
    body = await apiFetch<unknown>(`${STAFF_PREFIX}${path}`, init);
  } catch (caught) {
    if (caught instanceof ApiError) throw new StaffApiError(caught.code ?? GENERIC_CODE);
    throw new StaffApiError(signal?.aborted ? ABORTED_CODE : NETWORK_CODE);
  }
  if (isErrorBody(body) || body === null || body === undefined) throw staffErrorFromBody(body);
  return body as T;
}

/** `GET /staff/reports` with every filter of the screen, the settlement included. */
export function getReportExt(query: ReportQueryExt = {}, signal?: AbortSignal): Promise<StaffReport> {
  return staffGetJson<StaffReport>(`/reports${reportQueryExt(query)}`, signal);
}

/**
 * `GET /staff/reports/export`: the `.xlsx` of every ticket the filters match (R31.1), as a Blob, fetched with
 * the staff bearer token. A refusal is a `StaffApiError` (`export_too_large`, `invalid_filter`, ...).
 */
export async function exportReportExt(
  query: ReportExportQueryExt = {},
  lang: ReportExportLang = 'he',
  signal?: AbortSignal,
): Promise<Blob> {
  try {
    const path = `${STAFF_PREFIX}/reports/export${exportReportQueryExt(query, lang)}`;
    return await apiFetchBlob(path, signal ? { signal } : {});
  } catch (caught) {
    if (caught instanceof ApiError) throw new StaffApiError(caught.code ?? GENERIC_CODE);
    throw new StaffApiError(signal?.aborted ? ABORTED_CODE : NETWORK_CODE);
  }
}

// --- the parts for the "Part replaced" filter (R29.8) ----------------------------------------------

/** A replaceable part as the filter lists it. */
export interface PartOption {
  readonly id: string;
  readonly name_he: string;
  readonly name_en: string;
  readonly is_active: boolean;
}

/** Query keys of the part lists; both sit under `['service-staff']` like the other staff keys. */
export const partKeys = {
  all: ['service-staff', 'parts', 'all'] as const,
  active: ['service-staff', 'parts', 'active'] as const,
};

/**
 * The parts to offer in the report filter. A report may be filtered by an inactive part, so Config_Staff read
 * every part (`GET /staff/parts`); the other ticket staff read the active ones (`GET /staff/parts/active`).
 */
export async function listPartOptions(includeInactive: boolean, signal?: AbortSignal): Promise<readonly PartOption[]> {
  const body = await staffGetJson<{ items: readonly PartOption[] }>(
    includeInactive ? '/parts' : '/parts/active',
    signal,
  );
  return Array.isArray(body.items) ? body.items : [];
}

// --- saving the downloaded file (R31.6) ------------------------------------------------------------

/** The file name used when the client does not hand over the `Content-Disposition` header. */
export const REPORT_EXPORT_FILE_NAME = 'service-tickets-report.xlsx';

/** What `saveBlob` needs from the browser; the tests pass stand-ins. */
export interface SaveEnvironment {
  readonly createObjectURL: (blob: Blob) => string;
  readonly revokeObjectURL: (url: string) => void;
  readonly createAnchor: () => Pick<HTMLAnchorElement, 'href' | 'download' | 'click' | 'remove' | 'style'>;
  readonly appendAnchor: (anchor: Pick<HTMLAnchorElement, 'href' | 'download' | 'click' | 'remove' | 'style'>) => void;
}

function browserEnvironment(): SaveEnvironment {
  return {
    createObjectURL: (blob) => URL.createObjectURL(blob),
    revokeObjectURL: (url) => URL.revokeObjectURL(url),
    createAnchor: () => document.createElement('a'),
    appendAnchor: (anchor) => document.body.appendChild(anchor as HTMLAnchorElement),
  };
}

/**
 * Saves `blob` as `fileName`: a temporary object URL on a temporary anchor that is clicked. The URL is revoked
 * and the anchor removed afterwards, also when the click throws.
 */
export function saveBlob(blob: Blob, fileName: string, env: SaveEnvironment = browserEnvironment()): void {
  const url = env.createObjectURL(blob);
  const anchor = env.createAnchor();
  try {
    anchor.href = url;
    anchor.download = fileName;
    anchor.style.display = 'none';
    env.appendAnchor(anchor);
    anchor.click();
  } finally {
    anchor.remove();
    env.revokeObjectURL(url);
  }
}
