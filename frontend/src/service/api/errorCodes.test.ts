import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { ERROR_CODES, isErrorCode } from './errorCodes';

// Cross-check with backend/app/service_tickets/error_codes.py. docker-compose mounts only ./frontend into
// the web container, so inside it the backend file is not there and the comparison is skipped. To run it,
// use a checkout where both directories exist: from the repository root, `cd frontend && npm run test --
// src/service/api/errorCodes.test.ts` on the host, or set SERVICE_ERROR_CODES_PY to the file's path.
const here = dirname(fileURLToPath(import.meta.url));
const backendFile =
  process.env['SERVICE_ERROR_CODES_PY'] ??
  resolve(here, '../../../../backend/app/service_tickets/error_codes.py');
const reachable = existsSync(backendFile);

/** Reads `ERROR_CODES` of the Python module: `CODE_X = "value"` lines, then the names listed in the tuple. */
function parseBackendCodes(source: string): string[] {
  const values = new Map<string, string>();
  for (const m of source.matchAll(/^(CODE_[A-Z0-9_]+)\s*=\s*"([^"]+)"\s*$/gm)) {
    values.set(m[1] as string, m[2] as string);
  }
  const start = source.indexOf('ERROR_CODES');
  const open = source.indexOf('(', source.indexOf('=', start));
  const close = source.indexOf('\n)', open);
  const body = source
    .slice(open + 1, close)
    .split('\n')
    .map((line) => line.replace(/#.*$/, '').trim())
    .join(' ');
  return body
    .split(',')
    .map((name) => name.trim())
    .filter((name) => name !== '')
    .map((name) => {
      const value = values.get(name);
      if (value === undefined) throw new Error(`error_codes.py: ${name} has no string value`);
      return value;
    });
}

describe('frontend error code list', () => {
  it('has no duplicates and only snake_case codes', () => {
    expect(new Set(ERROR_CODES).size).toBe(ERROR_CODES.length);
    for (const code of ERROR_CODES) expect(code).toMatch(/^[a-z][a-z0-9_]*$/);
  });

  it('isErrorCode accepts listed codes only', () => {
    expect(isErrorCode('rate_limited')).toBe(true);
    expect(isErrorCode('generic')).toBe(false);
    expect(isErrorCode(42)).toBe(false);
    expect(isErrorCode(null)).toBe(false);
  });

  // Skipped (and reported as skipped) when backend/app/service_tickets/error_codes.py is not reachable,
  // as in the web container. Run it on the host or set SERVICE_ERROR_CODES_PY, see the top of this file.
  it.skipIf(!reachable)('equals ERROR_CODES of backend/app/service_tickets/error_codes.py, in order', () => {
    expect([...ERROR_CODES]).toEqual(parseBackendCodes(readFileSync(backendFile, 'utf8')));
  });
});
