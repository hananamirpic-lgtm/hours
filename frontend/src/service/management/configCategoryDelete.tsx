// The confirmation of deleting an issue category (task 24.7; R19.11, R19.12, R19.13, R23.12).
//
// The text says what delete does: a category no ticket uses is removed for good, one that tickets use is
// deactivated instead. The result (`removed` or `deactivated`) comes back to the page, which shows it. Focus
// starts on the safe choice, Cancel. A refusal stays in the dialog as an alert: `last_active_category` (R19.13)
// leaves the dialog open with the message; `category_not_found` also refreshes the list behind it and leaves
// only Close.
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { deleteCategory, isStaffApiError, staffKeys, type CategoryDeleteResult, type StaffCategory } from '../api/staff';
import { ConfigDialog } from './configDialog';
import { errorTextKey, isCategoryGone, isolateName, type ErrorTextKey } from './configModel';
import { SERVICE_NAMESPACE } from './namespace';
import './config.css';

export const DELETE_IDS = {
  title: 'config-delete-title',
  prompt: 'config-delete-prompt',
  confirm: 'config-delete-yes',
  dismiss: 'config-delete-no',
} as const;

export interface CategoryDeleteViewProps {
  /** The category name in the language of the screen. */
  readonly name: string;
  readonly pending: boolean;
  /** The text key of the refusal, when the last attempt failed. */
  readonly error: ErrorTextKey | null;
  /** True after `category_not_found`: there is nothing left to confirm. */
  readonly gone: boolean;
  readonly onConfirm: () => void;
  readonly onDismiss: () => void;
}

/** The body of the dialog. Presentational: no request, no cache. */
export function CategoryDeleteView({ name, pending, error, gone, onConfirm, onDismiss }: CategoryDeleteViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const errorText = error === null ? null : `${t('a11y.errorPrefix')} ${t(error)}`;
  return (
    <>
      <p id={DELETE_IDS.prompt}>{t('management.settings.categories.deletePrompt', { name: isolateName(name) })}</p>
      {errorText === null ? null : (
        <p className="feedback feedback--error" role="alert">
          {errorText}
        </p>
      )}
      <div className="form__actions">
        {gone ? null : (
          <button type="button" id={DELETE_IDS.confirm} className="button button--danger" disabled={pending} onClick={onConfirm}>
            {pending ? t('management.settings.categories.working') : t('management.settings.categories.delete')}
          </button>
        )}
        <button type="button" id={DELETE_IDS.dismiss} className="button" disabled={pending} onClick={onDismiss}>
          {gone ? t('management.common.close') : t('management.common.cancel')}
        </button>
      </div>
    </>
  );
}

export interface CategoryDeleteDialogProps {
  readonly category: StaffCategory;
  /** The category name in the language of the screen. */
  readonly name: string;
  /** Called after the Ticket_API answered `removed` or `deactivated`. The page closes the dialog and says which. */
  readonly onDone: (result: CategoryDeleteResult) => void;
  readonly onDismiss: () => void;
}

export function CategoryDeleteDialog({ category, name, onDone, onDismiss }: CategoryDeleteDialogProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const queryClient = useQueryClient();
  const [error, setError] = useState<ErrorTextKey | null>(null);
  const [gone, setGone] = useState(false);

  const mutation = useMutation({
    mutationFn: () => deleteCategory(category.id),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: staffKeys.categories });
      onDone(result);
    },
    onError: (caught) => {
      const code = isStaffApiError(caught) ? caught.code : 'generic';
      setError(errorTextKey(code));
      if (isCategoryGone(code)) {
        setGone(true);
        void queryClient.invalidateQueries({ queryKey: staffKeys.categories });
      }
    },
  });

  return (
    <ConfigDialog
      titleId={DELETE_IDS.title}
      title={t('management.settings.categories.deleteTitle')}
      initialFocusId={DELETE_IDS.dismiss}
      describedById={DELETE_IDS.prompt}
      busy={mutation.isPending}
      onClose={onDismiss}
    >
      <CategoryDeleteView
        name={name}
        pending={mutation.isPending}
        error={error}
        gone={gone}
        onConfirm={() => {
          if (mutation.isPending) return;
          setError(null);
          mutation.mutate();
        }}
        onDismiss={onDismiss}
      />
    </ConfigDialog>
  );
}
