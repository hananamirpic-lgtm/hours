// Tests of the staff client (task 24.1): the query builders, the error reading, and the requests it sends.
// The requests run through the real `apiFetch`; only the network (`fetch`) is replaced, so what is checked is
// the URL, method and body the client sends and how it reads the answer, `fields` included.
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  StaffApiError,
  changeTicketStatus,
  createCustomer,
  customerListQuery,
  deleteCategory,
  getDashboard,
  isErrorBody,
  isStaffUnauthenticated,
  listTickets,
  readLabels,
  reportQuery,
  resetCustomerPassword,
  staffErrorFromBody,
  ticketListQuery,
} from './staff';

describe('query builders', () => {
  it('send nothing for an empty query', () => {
    expect(ticketListQuery()).toBe('');
    expect(customerListQuery({})).toBe('');
    expect(reportQuery({})).toBe('');
  });

  it('build the ticket list query with the names the API reads', () => {
    const query = ticketListQuery({
      page: 2,
      search: ' 12345 ',
      status: ['received', 'in_progress'],
      categoryId: 'c-1',
      city: 'Haifa',
      urgentOnly: true,
      dateFrom: '2025-01-01',
      dateTo: '2025-01-31',
      urgentFirst: true,
    });
    const params = new URLSearchParams(query);
    expect(query.startsWith('?')).toBe(true);
    expect(params.get('page')).toBe('2');
    expect(params.get('search')).toBe(' 12345 ');
    expect(params.getAll('status')).toEqual(['received', 'in_progress']);
    expect(params.get('category_id')).toBe('c-1');
    expect(params.get('city')).toBe('Haifa');
    expect(params.get('urgent_only')).toBe('true');
    expect(params.get('date_from')).toBe('2025-01-01');
    expect(params.get('date_to')).toBe('2025-01-31');
    expect(params.get('urgent_first')).toBe('true');
  });

  it('leave out an unset flag, a blank text and an empty status list', () => {
    expect(
      ticketListQuery({ search: '  ', city: '', status: [], urgentOnly: false, urgentFirst: false }),
    ).toBe('');
  });

  it('send the customer state filter as true or false and nothing for all', () => {
    expect(new URLSearchParams(customerListQuery({ active: true })).get('active')).toBe('true');
    expect(new URLSearchParams(customerListQuery({ active: false })).get('active')).toBe('false');
    expect(customerListQuery({ search: 'dana' })).toBe('?search=dana');
  });

  it('build the report query', () => {
    const params = new URLSearchParams(reportQuery({ dateFrom: '2025-02-01', city: 'Eilat', categoryId: 'x', page: 3 }));
    expect([...params.keys()].sort()).toEqual(['category_id', 'city', 'date_from', 'page']);
  });
});

describe('error reading', () => {
  it('tells an error body from a success body', () => {
    expect(isErrorBody({ detail: { error: { code: 'x' } } })).toBe(true);
    expect(isErrorBody({ error: { code: 'x' } })).toBe(true);
    expect(isErrorBody({ items: [], total: 0 })).toBe(false);
    expect(isErrorBody(null)).toBe(false);
    expect(isErrorBody([])).toBe(false);
  });

  it('reads detail.error.code and fields', () => {
    const error = staffErrorFromBody({
      detail: { error: { code: 'invalid_phone', fields: { phone: 'invalid_phone', full_name: 'invalid_full_name' } } },
    });
    expect(error).toBeInstanceOf(StaffApiError);
    expect(error.code).toBe('invalid_phone');
    expect(error.fields).toEqual({ phone: 'invalid_phone', full_name: 'invalid_full_name' });
  });

  it.each([null, undefined, 'Bad Gateway', { detail: 'Not Found' }, { detail: { error: {} } }])(
    'gives the code generic and no fields for %j',
    (body) => {
      const error = staffErrorFromBody(body);
      expect(error.code).toBe('generic');
      expect(error.fields).toEqual({});
    },
  );

  it('recognises the two codes of an ended staff session', () => {
    expect(isStaffUnauthenticated(new StaffApiError('not_authenticated'))).toBe(true);
    expect(isStaffUnauthenticated(new StaffApiError('invalid_token'))).toBe(true);
    expect(isStaffUnauthenticated(new StaffApiError('insufficient_role'))).toBe(false);
    expect(isStaffUnauthenticated(new Error('x'))).toBe(false);
  });
});

// --- requests ----------------------------------------------------------------------------------------------

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

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('staff requests', () => {
  it('GET the dashboard under /api/service/staff and returns the body', async () => {
    const body = { open: 3, in_progress: 1, urgent: 1, ticket_number_warning: null };
    const fetchMock = stubFetch(answer(200, body));
    await expect(getDashboard()).resolves.toEqual(body);
    const { url, init } = lastCall(fetchMock);
    expect(url).toBe('/api/service/staff/dashboard');
    expect(init.method).toBe('GET');
  });

  it('puts the filters in the ticket list URL', async () => {
    const fetchMock = stubFetch(answer(200, { items: [], page: 1, page_size: 25, total: 0 }));
    await listTickets({ status: ['closed'], urgentFirst: true, page: 2 });
    expect(lastCall(fetchMock).url).toBe('/api/service/staff/tickets?page=2&status=closed&urgent_first=true');
  });

  it('POST a status change with the status last displayed', async () => {
    const fetchMock = stubFetch(answer(200, { ticket_number: 12345 }));
    await changeTicketStatus(12345, { expected_status: 'received', target_status: 'in_progress' });
    const { url, init } = lastCall(fetchMock);
    expect(url).toBe('/api/service/staff/tickets/12345/status');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ expected_status: 'received', target_status: 'in_progress' });
  });

  it('throws the code and the fields of a 422', async () => {
    stubFetch(
      answer(422, {
        detail: { error: { code: 'invalid_full_name', fields: { full_name: 'invalid_full_name', phone: 'invalid_phone' } } },
      }),
    );
    const error = await createCustomer({ phone: '1', full_name: '' }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(StaffApiError);
    expect((error as StaffApiError).code).toBe('invalid_full_name');
    expect((error as StaffApiError).fields).toEqual({ full_name: 'invalid_full_name', phone: 'invalid_phone' });
  });

  it('reads the code of a 403 for a technician on a configuration route', async () => {
    stubFetch(answer(403, { detail: { error: { code: 'insufficient_role' } } }));
    await expect(deleteCategory('c-1')).rejects.toMatchObject({ code: 'insufficient_role', fields: {} });
  });

  it('treats an error answer with no readable body as generic', async () => {
    stubFetch(new Response('<html>Bad Gateway</html>', { status: 502 }));
    await expect(getDashboard()).rejects.toMatchObject({ code: 'generic' });
  });

  it('treats a request that got no answer as network', async () => {
    stubFetch(new TypeError('Failed to fetch'));
    await expect(getDashboard()).rejects.toMatchObject({ code: 'network' });
  });

  it('treats a success answer that is not JSON as an error, not as data', async () => {
    stubFetch(new Response('<html>proxy</html>', { status: 200 }));
    await expect(getDashboard()).rejects.toMatchObject({ code: 'generic' });
  });

  it('resets a customer password with a 204 and no body', async () => {
    const fetchMock = stubFetch(new Response(null, { status: 204 }));
    await expect(resetCustomerPassword('id/1')).resolves.toBeUndefined();
    expect(lastCall(fetchMock).url).toBe('/api/service/staff/customers/id%2F1/reset-password');
  });

  it('reads the current labels with an empty PUT, which stores nothing', async () => {
    const labels = { city: { he: 'עיר', en: 'City' } };
    const fetchMock = stubFetch(answer(200, { labels }));
    await expect(readLabels()).resolves.toEqual(labels);
    const { url, init } = lastCall(fetchMock);
    expect(url).toBe('/api/service/staff/labels');
    expect(init.method).toBe('PUT');
    expect(JSON.parse(init.body as string)).toEqual({ labels: {} });
  });

  it('returns the result of a category delete', async () => {
    stubFetch(answer(200, { result: 'deactivated' }));
    await expect(deleteCategory('c-1')).resolves.toBe('deactivated');
  });
});
