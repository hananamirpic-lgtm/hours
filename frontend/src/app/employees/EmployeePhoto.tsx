/**
 * The employee photo (Requirement 3.1).
 *
 * A photo is an object in private storage; the employee row holds its key, not the image. Uploading
 * reuses the same presigned-URL path the documents use — ask for a URL, PUT the file straight to
 * private storage — and then records the returned key on the employee through a normal update. The
 * bytes never pass through the API. Since the key names a private object rather than a public URL,
 * the card shows that a photo is on file rather than rendering it inline; a signed view URL is a later
 * concern. Upload is administrator-only, gated by `canManage` and enforced by the server.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { createUploadUrl, employeeKeys, putToStorage, updateEmployee } from '@/api/employees';
import type { EmployeeResponse } from '@/api/types';
import { toError } from '@/lib/apiError';

const ALLOWED = ['image/jpeg', 'image/png'];
const MAX_BYTES = 10 * 1024 * 1024;

export function EmployeePhoto({
  employee,
  canManage,
}: {
  employee: EmployeeResponse;
  canManage: boolean;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [errorKey, setErrorKey] = useState<string | null>(null);

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const ticket = await createUploadUrl(employee.id, {
        type: 'other',
        file_name: file.name,
        mime_type: file.type,
        size_bytes: file.size,
      });
      await putToStorage(ticket, file);
      return updateEmployee(employee.id, { photo_key: ticket.file_key });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: employeeKeys.detail(employee.id) });
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
    if (!ALLOWED.includes(file.type)) {
      setErrorKey('apiError.unsupported_file_type');
      return;
    }
    if (file.size > MAX_BYTES) {
      setErrorKey('apiError.file_too_large');
      return;
    }
    upload.mutate(file);
  };

  return (
    <div>
      <div className="photo photo--placeholder" role="img" aria-label={t('employee.photo')}>
        {employee.photo_key ? t('employee.photoOnFile') : t('employee.noPhoto')}
      </div>
      {canManage ? (
        <div style={{ marginBlockStart: 'var(--space)' }}>
          <input
            ref={fileRef}
            type="file"
            accept=".jpg,.jpeg,.png"
            aria-label={t('employee.uploadPhoto')}
            disabled={upload.isPending}
            onChange={(event) => onFile(event.target.files?.[0])}
          />
          {upload.isPending ? <p className="state">{t('document.uploading')}</p> : null}
          {errorKey ? (
            <p className="feedback feedback--error" role="alert">
              {t(errorKey)}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
