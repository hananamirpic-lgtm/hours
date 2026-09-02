/**
 * Self-service profile photo endpoints (Requirement 3.1, employee-facing).
 *
 * The employee mobile app lets a signed-in employee replace their own profile photo. Like the
 * document flow it is a two-step, file-never-through-the-API upload (Requirements 4.3, 20.3): ask for
 * a presigned URL, PUT the image straight to private storage, then tell the API the upload finished
 * so it verifies the bytes and sets the photo. Every call is scoped to the caller's own employee
 * server-side — no employee id is ever sent — so there is no argument here that could name someone
 * else. A photo is images only; a PDF is refused by the server at both steps.
 *
 * The direct-to-storage PUT reuses `putToStorage` from `./employees`, which PUTs the file with the
 * ticket's required headers and no bearer token.
 */

import type { DocumentUploadTicket } from './types';

import { apiFetch } from './client';

export const mePhotoKeys = {
  /** The caller's own photo URL (GET /api/me/photo). One key: it is always "my own". */
  photo: ['me', 'photo'] as const,
};

/** The caller's own photo as a short-lived signed URL, or null when none (GET /api/me/photo). */
export interface MyPhotoUrl {
  url: string | null;
  expires_in_seconds: number;
}

/** The result of setting a photo (POST /api/me/photo): the stored key and a fresh view URL. */
export interface MyPhotoResult {
  photo_key: string;
  url: string;
  expires_in_seconds: number;
}

/** GET /api/me/photo — the caller's own photo URL, or `{ url: null }`. */
export const getMyPhotoUrl = (): Promise<MyPhotoUrl> => apiFetch<MyPhotoUrl>('/me/photo');

/**
 * POST /api/me/photo/upload-url — a short-lived, image- and size-constrained presigned URL for the
 * caller's own photo. The declared type must be image/jpeg or image/png; anything else is refused.
 */
export const beginMyPhotoUpload = (mimeType: string): Promise<DocumentUploadTicket> =>
  apiFetch<DocumentUploadTicket>('/me/photo/upload-url', {
    method: 'POST',
    body: JSON.stringify({ mime_type: mimeType }),
  });

/**
 * POST /api/me/photo — report the upload finished. The server verifies the stored bytes are the
 * declared image type and within the 10 MB cap, sets the caller's `photo_key`, and returns the key
 * with a fresh view URL so the screen can show the new image at once.
 */
export const completeMyPhotoUpload = (
  fileKey: string,
  mimeType: string,
): Promise<MyPhotoResult> =>
  apiFetch<MyPhotoResult>('/me/photo', {
    method: 'POST',
    body: JSON.stringify({ file_key: fileKey, mime_type: mimeType }),
  });
