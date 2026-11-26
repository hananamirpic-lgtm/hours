// Object URLs of a Blob for a preview (task 30.7; R28.1). A Blob shown in an `<img>` needs a `blob:` URL, and the
// URL keeps the Blob in memory until it is revoked. `createManagedUrl` is the pure core (the URL API is a
// parameter, so a test can watch the calls); `useObjectUrl` is the hook the branding screen uses: it makes the
// URL when the Blob changes and revokes it when the Blob changes again and on unmount.
import { useEffect, useState } from 'react';

/** The two functions of the `URL` API that a preview needs. */
export interface ObjectUrlApi {
  readonly createObjectURL: (blob: Blob) => string;
  readonly revokeObjectURL: (url: string) => void;
}

/** An object URL that can be revoked once; a second `revoke` does nothing. */
export interface ManagedUrl {
  readonly url: string;
  readonly revoke: () => void;
}

/** The `URL` API of the environment, or null where it has none (a server render). */
export function browserObjectUrlApi(): ObjectUrlApi | null {
  if (typeof URL === 'undefined') return null;
  if (typeof URL.createObjectURL !== 'function' || typeof URL.revokeObjectURL !== 'function') return null;
  return { createObjectURL: (blob) => URL.createObjectURL(blob), revokeObjectURL: (url) => URL.revokeObjectURL(url) };
}

/** Makes an object URL for `blob`. Null when there is no blob or no URL API. */
export function createManagedUrl(
  blob: Blob | null,
  api: ObjectUrlApi | null = browserObjectUrlApi(),
): ManagedUrl | null {
  if (blob === null || api === null) return null;
  const url = api.createObjectURL(blob);
  let revoked = false;
  return {
    url,
    revoke: () => {
      if (revoked) return;
      revoked = true;
      api.revokeObjectURL(url);
    },
  };
}

/**
 * The object URL of `blob`, or null while there is none. The URL of the previous blob is revoked as soon as the
 * blob changes, and the last one on unmount.
 */
export function useObjectUrl(blob: Blob | null): string | null {
  const [made, setMade] = useState<{ readonly blob: Blob | null; readonly url: string | null }>({
    blob: null,
    url: null,
  });
  useEffect(() => {
    const managed = createManagedUrl(blob);
    setMade({ blob, url: managed === null ? null : managed.url });
    return () => {
      managed?.revoke();
    };
  }, [blob]);
  // A URL made for an earlier blob is already revoked, so it is never handed out for a later one.
  return made.blob === blob ? made.url : null;
}
