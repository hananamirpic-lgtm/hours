// The Issue categories screen of the Management_Interface (task 24.7; R19.1, R19.5 to R19.13, R23.12). Config
// staff only: `RequireConfigStaff` in the route tree keeps a technician from ever mounting it.
//
// The list is every category, active and inactive, oldest first. Create and rename open the form in a dialog;
// delete opens a confirmation whose answer is `removed` or `deactivated`, and the page says which. Deactivate and
// reactivate act at once (both are undone by the other) and a refusal such as `last_active_category` is shown in an
// alert above the table. Each success ends with a status message in a live region, the list is reloaded, and focus
// returns to the control that opened a dialog (or to the heading when that control no longer exists).
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { deactivateCategory, isStaffApiError, listCategories, reactivateCategory, staffKeys } from '../../api/staff';
import type { StaffCategory } from '../../api/staffTypes';
import { CategoriesTable } from '../configCategoriesTable';
import { CategoryDeleteDialog } from '../configCategoryDelete';
import { CategoryForm } from '../configCategoryForm';
import {
  categoryActionTexts,
  categoryDisplayName,
  configLanguage,
  deleteResultKey,
  errorTextKey,
  isCategoryGone,
  isolateName,
  type CategoryAction,
  type ErrorTextKey,
} from '../configModel';
import { SERVICE_NAMESPACE } from '../namespace';
import '../config.css';

type DialogState =
  | { readonly kind: 'create' }
  | { readonly kind: 'edit'; readonly category: StaffCategory }
  | { readonly kind: 'delete'; readonly category: StaffCategory };

interface RowError {
  readonly name: string;
  readonly key: ErrorTextKey;
}

export function CategoriesPage() {
  const { t, i18n } = useTranslation(SERVICE_NAMESPACE);
  const language = configLanguage(i18n.language);
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<DialogState | null>(null);
  // The text key of the last success, shown in the live region. Null when nothing happened yet.
  const [notice, setNotice] = useState<string | null>(null);
  // The refusal of the last deactivate or reactivate, shown as an alert above the table.
  const [rowError, setRowError] = useState<RowError | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const dialogWasOpen = useRef(false);

  const list = useQuery({
    queryKey: staffKeys.categories,
    queryFn: ({ signal }) => listCategories(signal),
  });

  const toggle = useMutation({
    mutationFn: ({ category, action }: { category: StaffCategory; action: CategoryAction }) =>
      action === 'reactivate' ? reactivateCategory(category.id) : deactivateCategory(category.id),
    onSuccess: async (_saved, { action }) => {
      setRowError(null);
      setNotice(categoryActionTexts(action).doneKey);
      await queryClient.invalidateQueries({ queryKey: staffKeys.categories });
    },
    onError: (error, { category }) => {
      const code = isStaffApiError(error) ? error.code : 'generic';
      setNotice(null);
      setRowError({ name: categoryDisplayName(category, language), key: errorTextKey(code) });
      if (isCategoryGone(code)) void queryClient.invalidateQueries({ queryKey: staffKeys.categories });
    },
  });

  // When a dialog closes, focus goes back to the control that opened it, or to the heading when it is gone.
  useEffect(() => {
    if (dialog !== null) {
      dialogWasOpen.current = true;
      return;
    }
    if (!dialogWasOpen.current) return;
    dialogWasOpen.current = false;
    const opener = openerRef.current;
    openerRef.current = null;
    if (opener !== null && opener.isConnected) opener.focus();
    else headingRef.current?.focus();
  }, [dialog]);

  const open = (next: DialogState): void => {
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setNotice(null);
    setRowError(null);
    setDialog(next);
  };

  const onAction = (category: StaffCategory, action: CategoryAction): void => {
    if (action === 'delete') {
      open({ kind: 'delete', category });
      return;
    }
    if (toggle.isPending) return;
    setNotice(null);
    setRowError(null);
    toggle.mutate({ category, action });
  };

  const rows = list.data ?? [];
  const loadFailed = list.isError;
  const rowErrorText =
    rowError === null
      ? null
      : `${t('a11y.errorPrefix')} ${t('management.settings.categories.rowError', {
          name: isolateName(rowError.name),
          message: t(rowError.key),
        })}`;

  return (
    <section className="page-body">
      <div className="page-header">
        <h1 ref={headingRef} tabIndex={-1}>
          {t('management.settings.categories.title')}
        </h1>
        <div className="page-header__actions">
          <button type="button" className="button button--primary" onClick={() => open({ kind: 'create' })}>
            {t('management.settings.categories.add')}
          </button>
        </div>
      </div>
      <p className="subtitle">{t('management.settings.categories.subtitle')}</p>

      <div role="status" aria-live="polite" className="config-notice">
        {notice === null ? null : <p className="feedback feedback--success">{t(notice)}</p>}
      </div>

      {rowErrorText === null ? null : (
        <p className="feedback feedback--error" role="alert">
          {rowErrorText}
        </p>
      )}

      {loadFailed ? (
        <div className="feedback feedback--error config-load-error" role="alert">
          <p>{t('management.settings.categories.loadError')}</p>
          <button type="button" className="button button--small" onClick={() => void list.refetch()}>
            {t('management.common.retry')}
          </button>
        </div>
      ) : null}

      {list.data === undefined && !loadFailed ? (
        <p className="state" aria-busy="true">
          {t('management.common.loading')}
        </p>
      ) : null}

      {list.data !== undefined && rows.length > 0 ? (
        <CategoriesTable
          rows={rows}
          language={language}
          busy={toggle.isPending}
          onEdit={(category) => open({ kind: 'edit', category })}
          onAction={onAction}
        />
      ) : null}

      {list.data !== undefined && rows.length === 0 ? (
        <p className="state">{t('management.settings.categories.empty')}</p>
      ) : null}

      {dialog?.kind === 'create' || dialog?.kind === 'edit' ? (
        <CategoryForm
          key={dialog.kind === 'edit' ? dialog.category.id : 'new'}
          {...(dialog.kind === 'edit' ? { category: dialog.category } : {})}
          onSaved={(_saved, mode) => {
            setNotice(
              mode === 'created'
                ? 'management.settings.categories.created'
                : 'management.settings.categories.updated',
            );
            setDialog(null);
          }}
          onCancel={() => setDialog(null)}
        />
      ) : null}

      {dialog?.kind === 'delete' ? (
        <CategoryDeleteDialog
          key={dialog.category.id}
          category={dialog.category}
          name={categoryDisplayName(dialog.category, language)}
          onDone={(result) => {
            setNotice(deleteResultKey(result));
            setDialog(null);
          }}
          onDismiss={() => setDialog(null)}
        />
      ) : null}
    </section>
  );
}
