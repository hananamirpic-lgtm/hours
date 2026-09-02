import { afterEach, describe, expect, it, vi } from 'vitest';

import { beginMyPhotoUpload, completeMyPhotoUpload, getMyPhotoUrl } from '@/api/mePhoto';

/**
 * The self-service photo client is a thin wrapper over `apiFetch`. What is worth pinning is the wire
 * contract each call makes: the path, the method, and the JSON body — and, critically, that no call
 * ever sends an employee identifier, since the endpoints are self-scoped server-side. `fetch` is
 * stubbed so nothing leaves the process.
 */

interface StubbedCall {
  url: string;
  method: string;
  body: unknown;
}

const stubFetch = (payload: unknown): (() => StubbedCall) => {
  const calls: StubbedCall[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      calls.push({
        url,
        method: init.method ?? 'GET',
        body: init.body ? JSON.parse(init.body as string) : undefined,
      });
      return Promise.resolve(
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }),
  );
  return () => calls[calls.length - 1];
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('getMyPhotoUrl', () => {
  it('GETs /api/me/photo and returns the url and ttl', async () => {
    const lastCall = stubFetch({ url: 'https://storage.local/download/k', expires_in_seconds: 300 });

    const result = await getMyPhotoUrl();

    expect(lastCall().url).toBe('/api/me/photo');
    expect(lastCall().method).toBe('GET');
    expect(result.url).toBe('https://storage.local/download/k');
    expect(result.expires_in_seconds).toBe(300);
  });

  it('surfaces a null url when the caller has no photo', async () => {
    stubFetch({ url: null, expires_in_seconds: 300 });

    const result = await getMyPhotoUrl();

    expect(result.url).toBeNull();
  });
});

describe('beginMyPhotoUpload', () => {
  it('POSTs the declared mime type and nothing that names an employee', async () => {
    const lastCall = stubFetch({
      file_key: 'employees/e/uuid/photo.png',
      upload_url: 'https://storage.local/upload/x',
      required_headers: { 'Content-Type': 'image/png' },
      expires_in_seconds: 300,
      max_bytes: 10485760,
    });

    const ticket = await beginMyPhotoUpload('image/png');

    expect(lastCall().url).toBe('/api/me/photo/upload-url');
    expect(lastCall().method).toBe('POST');
    expect(lastCall().body).toEqual({ mime_type: 'image/png' });
    // Self-scoped: the body carries no employee identifier of any kind.
    expect(JSON.stringify(lastCall().body)).not.toContain('employee');
    expect(ticket.required_headers['Content-Type']).toBe('image/png');
  });
});

describe('completeMyPhotoUpload', () => {
  it('POSTs the file key and mime type, and returns the new key and view url', async () => {
    const lastCall = stubFetch({
      photo_key: 'employees/e/uuid/photo.jpg',
      url: 'https://storage.local/download/employees/e/uuid/photo.jpg',
      expires_in_seconds: 300,
    });

    const result = await completeMyPhotoUpload('employees/e/uuid/photo.jpg', 'image/jpeg');

    expect(lastCall().url).toBe('/api/me/photo');
    expect(lastCall().method).toBe('POST');
    expect(lastCall().body).toEqual({
      file_key: 'employees/e/uuid/photo.jpg',
      mime_type: 'image/jpeg',
    });
    expect(result.photo_key).toBe('employees/e/uuid/photo.jpg');
    expect(result.url).toContain('storage.local/download');
  });
});
