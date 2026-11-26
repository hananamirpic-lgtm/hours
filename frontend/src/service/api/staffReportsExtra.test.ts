// The requests run through the real `apiFetch` and `apiFetchBlob`; only `fetch`, the object URL functions and
// the anchor are replaced, so what is checked is the URL the client sends, the errors it raises and that the
// temporary object URL of a download is always revoked.
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  REPORT_EXPORT_FILE_NAME,
  exportReportExt,
  getReportExt,
  listPartOptions,
  saveBlob,
  type SaveEnvironment,
} from './staffReportsExtra';

function answer(status: number, body?: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
  });
}

function stubFetch(response: Response | Error) {
  const fetchMock = vi.fn(async () => {
    if (response instanceof Error) throw response;
    return response;
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function lastUrl(fetchMock: ReturnType<typeof stubFetch>): string {
  return (fetchMock.mock.calls.at(-1) as unknown as [string])[0];
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('the report requests with the settlement filter', () => {
  it('GETs the report with locality_id and the part filter', async () => {
    const fetchMock = stubFetch(answer(200, { total: 0 }));
    await getReportExt({ page: 2, localityId: 'l-1', partFilter: 'yes' });
    expect(lastUrl(fetchMock)).toBe('/api/service/staff/reports?page=2&locality_id=l-1&part_filter=yes');
  });

  it('reads a coded refusal of the report', async () => {
    stubFetch(answer(422, { detail: { error: { code: 'invalid_filter' } } }));
    await expect(getReportExt({ localityId: 'x' })).rejects.toMatchObject({ name: 'StaffApiError', code: 'invalid_filter' });
  });

  it('GETs the export with the filters, the settlement and the language, as a Blob', async () => {
    const bytes = new Uint8Array([0x50, 0x4b, 3, 4]);
    const fetchMock = stubFetch(new Response(bytes, { status: 200 }));
    const blob = await exportReportExt({ localityId: 'l-1', partFilter: 'no', city: 'Haifa' }, 'en');
    expect(lastUrl(fetchMock)).toBe('/api/service/staff/reports/export?city=Haifa&locality_id=l-1&part_filter=no&lang=en');
    expect(blob.size).toBe(bytes.byteLength);
  });

  it.each([
    [answer(422, { detail: { error: { code: 'export_too_large' } } }), 'export_too_large'],
    [new Response('Bad Gateway', { status: 502 }), 'generic'],
    [new TypeError('Failed to fetch'), 'network'],
  ])('turns a failed export into a StaffApiError with the code %#', async (response, code) => {
    stubFetch(response);
    await expect(exportReportExt({}, 'he')).rejects.toMatchObject({ name: 'StaffApiError', code });
  });
});

describe('the parts for the filter (R29.8)', () => {
  const items = [{ id: 'p1', name_he: 'משאבה', name_en: 'Pump', is_active: true }];

  it('reads every part for Config_Staff and the active ones for the other staff', async () => {
    const fetchMock = stubFetch(answer(200, { items }));
    await expect(listPartOptions(true)).resolves.toEqual(items);
    expect(lastUrl(fetchMock)).toBe('/api/service/staff/parts');
    stubFetch(answer(200, { items }));
    const active = stubFetch(answer(200, { items }));
    await expect(listPartOptions(false)).resolves.toEqual(items);
    expect(lastUrl(active)).toBe('/api/service/staff/parts/active');
  });

  it('raises the code of a refusal', async () => {
    stubFetch(answer(403, { detail: { error: { code: 'insufficient_role' } } }));
    await expect(listPartOptions(true)).rejects.toMatchObject({ code: 'insufficient_role' });
  });
});

describe('saveBlob', () => {
  function environment(click: () => void = () => undefined) {
    const calls: string[] = [];
    const anchor = {
      href: '',
      download: '',
      style: { display: '' } as CSSStyleDeclaration,
      click: () => {
        calls.push(`click:${anchor.href}:${anchor.download}`);
        click();
      },
      remove: () => {
        calls.push('remove');
      },
    };
    const env: SaveEnvironment = {
      createObjectURL: () => {
        calls.push('create');
        return 'blob:temp-1';
      },
      revokeObjectURL: (url) => {
        calls.push(`revoke:${url}`);
      },
      createAnchor: () => anchor,
      appendAnchor: () => {
        calls.push('append');
      },
    };
    return { env, calls };
  }

  it('clicks a temporary link to the object URL with the file name, then revokes the URL', () => {
    const { env, calls } = environment();
    saveBlob(new Blob(['x']), REPORT_EXPORT_FILE_NAME, env);
    expect(calls).toEqual([
      'create',
      'append',
      `click:blob:temp-1:service-tickets-report.xlsx`,
      'remove',
      'revoke:blob:temp-1',
    ]);
  });

  it('revokes the URL even when the click throws', () => {
    const { env, calls } = environment(() => {
      throw new Error('blocked');
    });
    expect(() => saveBlob(new Blob(['x']), 'a.xlsx', env)).toThrow('blocked');
    expect(calls.at(-1)).toBe('revoke:blob:temp-1');
  });
});
