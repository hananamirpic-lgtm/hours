/**
 * The employee "My photo" profile screen (Requirement 3.1, employee-facing).
 *
 * A signed-in employee sees their own profile photo and can replace it from their phone. The photo is
 * an object in private storage; the employee row holds its key, not the image, so the screen fetches
 * a short-lived signed URL (`GET /api/me/photo`) and renders it as an `<img>` — or a placeholder when
 * none is on file. Every request is scoped server-side to the caller's own employee; no id is sent
 * from the client.
 *
 * Uploading is the two-step, file-never-through-the-API flow the documents use, constrained to
 * images: pick a file, validate client-side that it is JPEG/PNG and within 10 MB (a friendly message
 * otherwise, before any request), ask for a presigned URL, PUT the file straight to private storage,
 * then report completion. On success the photo query is invalidated so the new image shows at once.
 *
 * Mobile-first and one-handed: the controls clear the 44px minimum, the layout is flow-relative
 * (logical properties only), it mirrors for Hebrew from the one stylesheet, and every string is
 * translated (no bare literals).
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { putToStorage } from '@/api/employees';
import {
  beginMyPhotoUpload,
  completeMyPhotoUpload,
  getMyPhotoUrl,
  mePhotoKeys,
} from '@/api/mePhoto';
import { toError } from '@/lib/apiError';
import { HindiHelper } from '@/components/HindiHelper';

/** The image types a profile photo may be, matching the server's image-only rule. */
const ALLOWED = ['image/jpeg', 'image/png'];
/** The 10 MB cap, matching `MAX_FILE_BYTES` on the server. */
const MAX_BYTES = 10 * 1024 * 1024;

export function MobileProfilePage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  // A translation key for the current error, or null. Client-side validation and server refusals
  // both funnel through here so the screen shows one message in the caller's language.
  const [errorKey, setErrorKey] = useState<string | null>(null);

  const photoQuery = useQuery({ queryKey: mePhotoKeys.photo, queryFn: getMyPhotoUrl });
  const photoUrl = photoQuery.data?.url ?? null;

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const ticket = await beginMyPhotoUpload(file.type);
      await putToStorage(ticket, file);
      return completeMyPhotoUpload(ticket.file_key, file.type);
    },
    onSuccess: async () => {
      // Refetch the signed URL so the new image replaces the old one on screen.
      await queryClient.invalidateQueries({ queryKey: mePhotoKeys.photo });
      if (fileRef.current) {
        fileRef.current.value = '';
      }
    },
    onError: (error) => setErrorKey(toError(error).key),
  });

  const onFile = (file: File | undefined) => {
    if (!file) {
      return;
    }
    setErrorKey(null);
    // Validate up front, before any request: a wrong type or an oversize file gets a friendly message
    // and never leaves the device.
    if (!ALLOWED.includes(file.type)) {
      setErrorKey('myPhoto.wrongType');
      return;
    }
    if (file.size > MAX_BYTES) {
      setErrorKey('myPhoto.tooLarge');
      return;
    }
    upload.mutate(file);
  };

  return (
    <section className="mobile-profile">
      <h1 className="mobile-profile__title">
        {t('myPhoto.title')}
        <HindiHelper textKey="myPhoto.title" />
      </h1>

      <div className="photo-row">
        {photoQuery.isPending ? (
          <div className="photo photo--placeholder" role="img" aria-label={t('myPhoto.current')}>
            {t('common.loading')}
            <HindiHelper textKey="common.loading" />
          </div>
        ) : photoUrl ? (
          <img className="photo" src={photoUrl} alt={t('myPhoto.current')} />
        ) : (
          <div className="photo photo--placeholder" role="img" aria-label={t('myPhoto.current')}>
            {t('myPhoto.none')}
            <HindiHelper textKey="myPhoto.none" />
          </div>
        )}
      </div>

      <label className="mobile-profile__field">
        <span className="mobile-profile__label">
          {t('myPhoto.choose')}
          <HindiHelper textKey="myPhoto.choose" />
        </span>
        <input
          ref={fileRef}
          type="file"
          className="input"
          accept="image/png,image/jpeg"
          aria-label={t('myPhoto.choose')}
          disabled={upload.isPending}
          onChange={(event) => onFile(event.target.files?.[0])}
        />
      </label>

      {upload.isPending ? (
        <p className="subtitle">
          {t('myPhoto.uploading')}
          <HindiHelper textKey="myPhoto.uploading" />
        </p>
      ) : null}

      {upload.isSuccess && !upload.isPending ? (
        <p className="feedback" role="status">
          {t('myPhoto.updated')}
          <HindiHelper textKey="myPhoto.updated" />
        </p>
      ) : null}

      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t([errorKey, 'myPhoto.error'])}
        </p>
      ) : null}
    </section>
  );
}
