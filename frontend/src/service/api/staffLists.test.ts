// Tests of the client of the settlement list, the parts list and the branding (task 30.7). The requests run through
// the real `apiFetch`; only the network (`fetch`) is replaced, so what is checked is the URL, method, headers and
// body the client sends and how it reads the answer, `fields` included.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { setAccessTokenGetter } from '@/api/client';
import { StaffApiError } from './staff';
import {
  brandingFormData,
  fetchBrandingLogo,
  getBranding,
  localitiesApi,
  namedListClient,
  namedListKey,
  partsApi,
  staffListKeys,
  updateBranding,
  type NamedListApi,
} from './staffLists';

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

function lastCall(fetchMock: ReturnType<typeof stubFetch>): { url: string; init: RequestInit } {
  const call = fetchMock.mock.calls.at(-1) as unknown as [string, RequestInit];
  return { url: call[0], init: call[1] };
}

function headerNames(init: RequestInit): string[] {
  return Object.keys((init.headers ?? {}) as Record<string, string>).map((name) => name.toLowerCase());
}

afterEach(() => {
  vi.unstubAllGlobals();
  setAccessTokenGetter(() => null);
});

const ITEM = { id: 'a-1', name_he: 'חיפה', name_en: 'Haifa', is_active: true };

describe.each([
  ['localities', localitiesApi, '/api/service/staff/localities'],
  ['parts', partsApi, '/api/service/staff/parts'],
] as const)('the %s list client', (kind, api: NamedListApi, base) => {
  it('lists every item from GET and unwraps items', async () => {
    const fetchMock = stubFetch(answer(200, { items: [ITEM] }));
    await expect(api.list()).resolves.toEqual([ITEM]);
    const { url, init } = lastCall(fetchMock);
    expect(url).toBe(base);
    expect(init.method).toBe('GET');
  });

  it('creates with POST and both names as typed, as JSON', async () => {
    const fetchMock = stubFetch(answer(201, ITEM));
    await expect(api.create({ name_he: ' חיפה ', name_en: ' Haifa ' })).resolves.toEqual(ITEM);
    const { url, init } = lastCall(fetchMock);
    expect(url).toBe(base);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ name_he: ' חיפה ', name_en: ' Haifa ' });
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
  });

  it('renames with PUT on the item and escapes the id', async () => {
    const fetchMock = stubFetch(answer(200, ITEM));
    await api.rename('a/1', { name_he: 'ב', name_en: 'B' });
    const { url, init } = lastCall(fetchMock);
    expect(url).toBe(`${base}/a%2F1`);
    expect(init.method).toBe('PUT');
    expect(JSON.parse(init.body as string)).toEqual({ name_he: 'ב', name_en: 'B' });
  });

  it('deactivates and reactivates with a POST that has no body', async () => {
    let fetchMock = stubFetch(answer(200, { ...ITEM, is_active: false }));
    await api.deactivate('a-1');
    expect(lastCall(fetchMock).url).toBe(`${base}/a-1/deactivate`);
    expect(lastCall(fetchMock).init.method).toBe('POST');
    expect(lastCall(fetchMock).init.body).toBeUndefined();
    // A response body can be read once, so the second call gets its own.
    fetchMock = stubFetch(answer(200, ITEM));
    await api.reactivate('a-1');
    expect(lastCall(fetchMock).url).toBe(`${base}/a-1/reactivate`);
    expect(lastCall(fetchMock).init.method).toBe('POST');
  });

  it.each(['removed', 'deactivated'] as const)('deletes with DELETE and resolves to %s', async (result) => {
    const fetchMock = stubFetch(answer(200, { result }));
    await expect(api.remove('a-1')).resolves.toBe(result);
    const { url, init } = lastCall(fetchMock);
    expect(url).toBe(`${base}/a-1`);
    expect(init.method).toBe('DELETE');
  });

  it('is the client of its kind', () => {
    expect(namedListClient(kind)).toBe(api);
    expect(namedListKey(kind)).toEqual(['service-staff', kind]);
  });
});

describe('error mapping of the list clients', () => {
  it('keeps the code and the fields of a 422', async () => {
    stubFetch(
      answer(422, {
        detail: {
          error: {
            code: 'invalid_settlement_name_he',
            fields: { name_he: 'invalid_settlement_name_he', name_en: 'invalid_settlement_name_en' },
          },
        },
      }),
    );
    const error = await localitiesApi.create({ name_he: '', name_en: '' }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(StaffApiError);
    expect(error).toMatchObject({
      code: 'invalid_settlement_name_he',
      fields: { name_he: 'invalid_settlement_name_he', name_en: 'invalid_settlement_name_en' },
    });
  });

  it('keeps the clashing language of a 409 duplicate', async () => {
    stubFetch(answer(409, { detail: { error: { code: 'duplicate_part', fields: { name_en: 'duplicate_part' } } } }));
    await expect(partsApi.rename('a-1', { name_he: 'ב', name_en: 'B' })).rejects.toMatchObject({
      code: 'duplicate_part',
      fields: { name_en: 'duplicate_part' },
    });
  });

  it('gives a not-found code, generic for an unreadable body and network when nothing answers', async () => {
    stubFetch(answer(404, { detail: { error: { code: 'part_not_found' } } }));
    await expect(partsApi.remove('x')).rejects.toMatchObject({ code: 'part_not_found' });
    stubFetch(new Response('Bad Gateway', { status: 502 }));
    await expect(localitiesApi.list()).rejects.toMatchObject({ code: 'generic' });
    stubFetch(new TypeError('Failed to fetch'));
    await expect(localitiesApi.list()).rejects.toMatchObject({ code: 'network' });
  });

  it('treats an answer without a body as an error rather than as a list', async () => {
    stubFetch(answer(200));
    await expect(localitiesApi.list()).rejects.toMatchObject({ code: 'generic' });
  });
});

describe('the branding read', () => {
  it('GETs /staff/branding with the staff bearer token', async () => {
    setAccessTokenGetter(() => 'staff-token');
    const fetchMock = stubFetch(answer(200, { company_name: 'Acme', has_logo: true }));
    await expect(getBranding()).resolves.toEqual({ company_name: 'Acme', has_logo: true });
    const { url, init } = lastCall(fetchMock);
    expect(url).toBe('/api/service/staff/branding');
    expect(init.method).toBe('GET');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer staff-token');
  });

  it('reads an empty branding', async () => {
    stubFetch(answer(200, { company_name: null, has_logo: false }));
    await expect(getBranding()).resolves.toEqual({ company_name: null, has_logo: false });
  });
});

describe('the branding form', () => {
  const png = new File([new Uint8Array([137, 80, 78, 71])], 'my-private-folder.png', { type: 'image/png' });

  it('holds only what changed, in the order of the server form', () => {
    const entries = (change: Parameters<typeof brandingFormData>[0]): [string, string][] =>
      [...brandingFormData(change).entries()].map(([name, value]) => [name, typeof value === 'string' ? value : 'file']);
    expect(entries({})).toEqual([]);
    expect(entries({ companyName: ' Acme ' })).toEqual([['company_name', ' Acme ']]);
    expect(entries({ logo: png })).toEqual([['logo', 'file']]);
    expect(entries({ removeName: true, removeLogo: true })).toEqual([
      ['remove_name', 'true'],
      ['remove_logo', 'true'],
    ]);
    expect(entries({ companyName: 'Acme', logo: png, removeLogo: false, removeName: false })).toEqual([
      ['company_name', 'Acme'],
      ['logo', 'file'],
    ]);
  });

  it('sends the file under a fixed name, so a local file name never leaves the browser', () => {
    const file = brandingFormData({ logo: png }).get('logo');
    expect(file).toBeInstanceOf(File);
    expect((file as File).name).toBe('logo');
    expect((file as File).type).toBe('image/png');
  });
});

describe('the branding save', () => {
  const png = new File([new Uint8Array([137, 80, 78, 71])], 'logo.png', { type: 'image/png' });

  it('PUTs multipart with a FormData body and no hand-set Content-Type', async () => {
    setAccessTokenGetter(() => 'staff-token');
    const fetchMock = stubFetch(answer(200, { company_name: 'Acme', has_logo: true }));
    await expect(updateBranding({ companyName: 'Acme', logo: png })).resolves.toEqual({
      company_name: 'Acme',
      has_logo: true,
    });
    const { url, init } = lastCall(fetchMock);
    expect(url).toBe('/api/service/staff/branding');
    expect(init.method).toBe('PUT');
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get('company_name')).toBe('Acme');
    expect(headerNames(init)).not.toContain('content-type');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer staff-token');
  });

  it('sends the removals as the text true', async () => {
    const fetchMock = stubFetch(answer(200, { company_name: null, has_logo: false }));
    await updateBranding({ removeName: true, removeLogo: true });
    const body = lastCall(fetchMock).init.body as FormData;
    expect(body.get('remove_name')).toBe('true');
    expect(body.get('remove_logo')).toBe('true');
    expect(body.has('company_name')).toBe(false);
    expect(body.has('logo')).toBe(false);
  });

  it('keeps the fields of a 422 (company_name, logo, body)', async () => {
    stubFetch(
      answer(422, {
        detail: {
          error: {
            code: 'invalid_company_name',
            fields: { company_name: 'invalid_company_name', logo: 'invalid_logo_type' },
          },
        },
      }),
    );
    await expect(updateBranding({ companyName: '', logo: png })).rejects.toMatchObject({
      code: 'invalid_company_name',
      fields: { company_name: 'invalid_company_name', logo: 'invalid_logo_type' },
    });
    stubFetch(answer(422, { detail: { error: { code: 'invalid_request', fields: { body: 'invalid_request' } } } }));
    await expect(updateBranding({})).rejects.toMatchObject({ fields: { body: 'invalid_request' } });
  });

  it('gives the 413 code logo_too_large, and storage down as photo_storage_unavailable', async () => {
    stubFetch(answer(413, { detail: { error: { code: 'logo_too_large' } } }));
    await expect(updateBranding({ logo: png })).rejects.toMatchObject({ code: 'logo_too_large', fields: {} });
    stubFetch(answer(503, { detail: { error: { code: 'photo_storage_unavailable' } } }));
    await expect(updateBranding({ logo: png })).rejects.toMatchObject({ code: 'photo_storage_unavailable' });
  });
});

describe('the logo fetch', () => {
  const png = new Uint8Array([137, 80, 78, 71]);

  it('GETs /service/branding/logo with the bearer header and resolves to the image as a Blob', async () => {
    setAccessTokenGetter(() => 'staff-token');
    const fetchMock = stubFetch(new Response(png, { status: 200, headers: { 'Content-Type': 'image/png' } }));
    const blob = await fetchBrandingLogo();
    const { url, init } = lastCall(fetchMock);
    expect(url).toBe('/api/service/branding/logo');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer staff-token');
    expect(blob).toBeInstanceOf(Blob);
    expect(blob?.size).toBe(png.byteLength);
    expect(blob?.type).toBe('image/png');
  });

  it('resolves to null for the 404 with an empty body (no logo stored)', async () => {
    stubFetch(new Response(null, { status: 404 }));
    await expect(fetchBrandingLogo()).resolves.toBeNull();
  });

  it('refuses a body that is not an image, so a proxy page never reaches an object URL', async () => {
    stubFetch(new Response('<html>proxy</html>', { status: 200, headers: { 'Content-Type': 'text/html' } }));
    await expect(fetchBrandingLogo()).rejects.toMatchObject({ code: 'generic' });
  });

  it('gives the error code of a refusal, generic without one, network and aborted', async () => {
    stubFetch(answer(503, { detail: { error: { code: 'photo_storage_unavailable' } } }));
    await expect(fetchBrandingLogo()).rejects.toMatchObject({ code: 'photo_storage_unavailable' });
    stubFetch(new Response('Bad Gateway', { status: 502 }));
    await expect(fetchBrandingLogo()).rejects.toMatchObject({ code: 'generic' });
    stubFetch(new TypeError('Failed to fetch'));
    await expect(fetchBrandingLogo()).rejects.toMatchObject({ code: 'network' });
    const controller = new AbortController();
    controller.abort();
    stubFetch(new TypeError('aborted'));
    await expect(fetchBrandingLogo(controller.signal)).rejects.toMatchObject({ code: 'aborted' });
  });
});

describe('the query keys', () => {
  it('sit under the staff root, so one invalidation of it refreshes them all', () => {
    for (const key of Object.values(staffListKeys)) expect(key[0]).toBe('service-staff');
    expect(staffListKeys.brandingLogo.slice(0, 2)).toEqual(staffListKeys.branding);
  });
});
