/**
 * Employee documents (Requirement 4): list, upload, download and delete. The file never travels
 * through the API — the upload is two steps: ask for a short-lived presigned URL, PUT the file
 * straight to private storage, then tell the API the upload finished so it can verify the bytes and
 * record the row (Requirement 4.2, 4.3). Type and size are constrained to PDF/JPEG/PNG and 10 MB, and
 * the server verifies both again. Administrator only, which the card gates and the server enforces.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  completeUpload,
  createUploadUrl,
  deleteDocument,
  employeeKeys,
  getDocumentDownloadUrl,
  listEmployeeDocuments,
  putToStorage,
} from '@/api/employees';
import type { DocumentType } from '@/api/types';
import { toError } from '@/lib/apiError';
import { useLanguage } from '@/lib/useLanguage';
import { formatDate } from '@/lib/format';
import { ErrorState, LoadingState } from '@/components/management/QueryState';

const ALLOWED = ['application/pdf', 'image/jpeg', 'image/png'];
const MAX_BYTES = 10 * 1024 * 1024;
const TYPES: DocumentType[] = ['passport', 'work_permit', 'other'];

export function EmployeeDocumentsPanel({ employeeId }: { employeeId: string }) {
  const { t } = useTranslation();
  const language = useLanguage();
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [docType, setDocType] = useState<DocumentType>('passport');
  const [expiry, setExpiry] = useState('');
  const [errorKey, setErrorKey] = useState<string | null>(null);

  const query = useQuery({
    queryKey: employeeKeys.documents(employeeId),
    queryFn: () => listEmployeeDocuments(employeeId),
  });

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const ticket = await createUploadUrl(employeeId, {
        type: docType,
        file_name: file.name,
        mime_type: file.type,
        size_bytes: file.size,
        expiry_date: expiry || null,
      });
      await putToStorage(ticket, file);
      return completeUpload(employeeId, {
        file_key: ticket.file_key,
        type: docType,
        file_name: file.name,
        mime_type: file.type,
        expiry_date: expiry || null,
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: employeeKeys.documents(employeeId) });
      await queryClient.invalidateQueries({ queryKey: employeeKeys.detail(employeeId) });
      setExpiry('');
      if (fileRef.current) {
        fileRef.current.value = '';
      }
    },
    onError: (error) => setErrorKey(toError(error).key),
  });

  const remove = useMutation({
    mutationFn: (documentId: string) => deleteDocument(documentId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: employeeKeys.documents(employeeId) });
      await queryClient.invalidateQueries({ queryKey: employeeKeys.detail(employeeId) });
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

  const onDownload = async (documentId: string) => {
    try {
      const { url } = await getDocumentDownloadUrl(documentId);
      window.open(url, '_blank', 'noopener');
    } catch (error) {
      setErrorKey(toError(error).key);
    }
  };

  return (
    <div className="drawer__section">
      <div className="form-row">
        <label className="field">
          <span className="field__label">{t('document.type')}</span>
          <select
            className="select"
            value={docType}
            onChange={(event) => setDocType(event.target.value as DocumentType)}
          >
            {TYPES.map((value) => (
              <option key={value} value={value}>
                {t(`documentType.${value}`)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="field__label">{t('document.expiry')}</span>
          <input className="input" type="date" value={expiry} onChange={(event) => setExpiry(event.target.value)} />
        </label>
      </div>

      <label className="field">
        <span className="field__label">{t('document.file')}</span>
        <input
          ref={fileRef}
          className="input"
          type="file"
          accept=".pdf,.jpg,.jpeg,.png"
          disabled={upload.isPending}
          onChange={(event) => onFile(event.target.files?.[0])}
        />
      </label>
      {upload.isPending ? <p className="state">{t('document.uploading')}</p> : null}

      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey)}
        </p>
      ) : null}

      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState />
      ) : query.data.items.length === 0 ? (
        <p className="state">{t('document.none')}</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{t('document.type')}</th>
                <th>{t('document.name')}</th>
                <th>{t('document.expiry')}</th>
                <th>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {query.data.items.map((doc) => (
                <tr key={doc.id}>
                  <td>{t(`documentType.${doc.type}`)}</td>
                  <td>{doc.file_name}</td>
                  <td>
                    {doc.expiry_date ? formatDate(language, doc.expiry_date) : '—'}
                    {doc.is_expired ? <span className="pill pill--danger">{t('document.expired')}</span> : null}
                  </td>
                  <td>
                    <div className="inline-actions">
                      <button
                        type="button"
                        className="button button--small"
                        onClick={() => {
                          void onDownload(doc.id);
                        }}
                      >
                        {t('document.download')}
                      </button>
                      <button
                        type="button"
                        className="button button--small button--danger"
                        disabled={remove.isPending}
                        onClick={() => remove.mutate(doc.id)}
                      >
                        {t('common.delete')}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
