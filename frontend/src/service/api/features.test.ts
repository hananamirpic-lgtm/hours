import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  fetchFeatures,
  interpretFeatures,
  toFlagGateState,
  type FeaturesFetchResult,
} from '@/service/api/features';

/** A 200-style result with a parsed body. */
const parsed = (status: number, value: unknown): FeaturesFetchResult => ({
  kind: 'response',
  status,
  body: { ok: true, value },
});

/** A response whose body could not be parsed as JSON. */
const unparsed = (status: number): FeaturesFetchResult => ({ kind: 'response', status, body: { ok: false } });

describe('interpretFeatures', () => {
  it('200 with service_tickets true proceeds', () => {
    expect(interpretFeatures(parsed(200, { service_tickets: true }))).toBe('on');
  });

  it('200 with the accessibility contact beside true still proceeds', () => {
    expect(interpretFeatures(parsed(200, { service_tickets: true, accessibility_contact: 'a@b.c' }))).toBe('on');
  });

  it('200 with service_tickets false is off', () => {
    expect(interpretFeatures(parsed(200, { service_tickets: false }))).toBe('off');
  });

  it.each([
    ['the string "true"', 'true'],
    ['the number 1', 1],
    ['the string "1"', '1'],
    ['null', null],
    ['an object', {}],
    ['an array', [true]],
  ])('200 with service_tickets as %s is off', (_label, flag) => {
    expect(interpretFeatures(parsed(200, { service_tickets: flag }))).toBe('off');
  });

  it('200 with the key missing is off', () => {
    expect(interpretFeatures(parsed(200, {}))).toBe('off');
    expect(interpretFeatures(parsed(200, { accessibility_contact: 'a@b.c' }))).toBe('off');
  });

  it.each([
    ['null', null],
    ['an array', [{ service_tickets: true }]],
    ['a string', 'true'],
    ['a number', 1],
    ['the boolean true', true],
  ])('200 with a body that is %s is off', (_label, body) => {
    expect(interpretFeatures(parsed(200, body))).toBe('off');
  });

  it('200 with a body that cannot be parsed is unknown', () => {
    expect(interpretFeatures(unparsed(200))).toBe('unknown');
  });

  it('404 is off, whatever the body', () => {
    expect(interpretFeatures(unparsed(404))).toBe('off');
    expect(interpretFeatures(parsed(404, { detail: 'Not Found' }))).toBe('off');
    expect(interpretFeatures(parsed(404, { service_tickets: true }))).toBe('off');
  });

  it.each([500, 502, 503, 504])('%i is unknown', (status) => {
    expect(interpretFeatures(unparsed(status))).toBe('unknown');
    expect(interpretFeatures(parsed(status, { service_tickets: false }))).toBe('unknown');
  });

  // The endpoint needs no authentication, so 401 and 403 come from a proxy or firewall and say
  // nothing about the flag: unknown, never off and never on.
  it.each([401, 403])('%i is unknown', (status) => {
    expect(interpretFeatures(unparsed(status))).toBe('unknown');
    expect(interpretFeatures(parsed(status, { service_tickets: true }))).toBe('unknown');
  });

  it.each([204, 301, 400, 429])('any other status (%i) is unknown, never on', (status) => {
    expect(interpretFeatures(parsed(status, { service_tickets: true }))).toBe('unknown');
  });

  it('a network error is unknown', () => {
    expect(interpretFeatures({ kind: 'network-error' })).toBe('unknown');
  });

  it('a timeout is unknown', () => {
    expect(interpretFeatures({ kind: 'timeout' })).toBe('unknown');
  });
});

describe('toFlagGateState', () => {
  it('maps query status and data to the gate state', () => {
    expect(toFlagGateState('pending', undefined)).toBe('loading');
    expect(toFlagGateState('success', 'on')).toBe('on');
    expect(toFlagGateState('success', 'off')).toBe('off');
    expect(toFlagGateState('error', undefined)).toBe('unknown');
  });
});

describe('fetchFeatures', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const stubFetch = (impl: (url: string, init: RequestInit) => Promise<Response>) => {
    const spy = vi.fn(impl);
    vi.stubGlobal('fetch', spy);
    return spy;
  };

  it('calls the unauthenticated features path without credentials or an Authorization header', async () => {
    const spy = stubFetch(() =>
      Promise.resolve(new Response(JSON.stringify({ service_tickets: true }), { status: 200 })),
    );

    const result = await fetchFeatures();

    expect(result).toEqual({ kind: 'response', status: 200, body: { ok: true, value: { service_tickets: true } } });
    const [url, init] = spy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/features');
    expect(init.method).toBe('GET');
    expect(init.credentials).toBe('omit');
    expect(JSON.stringify(init.headers)).not.toMatch(/authorization/i);
  });

  it('reports a 404 without reading a body', async () => {
    stubFetch(() => Promise.resolve(new Response('{"detail":"Not Found"}', { status: 404 })));
    expect(interpretFeatures(await fetchFeatures())).toBe('off');
  });

  it('reports an unparseable 200 body as unknown', async () => {
    stubFetch(() => Promise.resolve(new Response('<html>not json</html>', { status: 200 })));
    const result = await fetchFeatures();
    expect(result).toEqual({ kind: 'response', status: 200, body: { ok: false } });
    expect(interpretFeatures(result)).toBe('unknown');
  });

  it('reports a rejected fetch as a network error', async () => {
    stubFetch(() => Promise.reject(new TypeError('Failed to fetch')));
    const result = await fetchFeatures();
    expect(result).toEqual({ kind: 'network-error' });
    expect(interpretFeatures(result)).toBe('unknown');
  });

  it('aborts after the timeout and reports a timeout', async () => {
    vi.useFakeTimers();
    stubFetch(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        }),
    );

    const pending = fetchFeatures(10_000);
    await vi.advanceTimersByTimeAsync(9_999);
    let settled = false;
    void pending.then(() => {
      settled = true;
    });
    await Promise.resolve();
    expect(settled).toBe(false);

    await vi.advanceTimersByTimeAsync(1);
    const result = await pending;
    expect(result).toEqual({ kind: 'timeout' });
    expect(interpretFeatures(result)).toBe('unknown');
  });

  it('uses a 10 second timeout by default', async () => {
    vi.useFakeTimers();
    stubFetch(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        }),
    );

    const pending = fetchFeatures();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(await pending).toEqual({ kind: 'timeout' });
  });
});
