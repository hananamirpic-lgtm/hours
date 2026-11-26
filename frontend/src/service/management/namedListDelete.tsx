// The confirmation of deleting a settlement or a replaceable part (task 30.7; R30.1, R29.6, R23.12). The same as
// the category confirmation, without the "last active" refusal.
//
// The text says what delete does: an item nothing uses is removed for good, one that is used is deactivated
// instead. The result (`removed` or `deactivated`) comes back to the page, which shows it. Focus starts on the
// safe choice, Cancel. A refusal stays in the dialog as an alert; a "not found" refusal also refreshes the list
// behind it and leaves only Close.
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { isStaffApiError } from '../api/staff';
import { namedListClient, namedListKey } from '../api/staffLists';
import type { NamedItemDeleteResult, NamedListKind, StaffNamedItem } from '../api/staffListsTypes';
import { ConfigDialog } from './configDialog';
import { NAMED_LISTS, errorTextKey, isNamedItemGone, isolateName, listText, type ErrorTextKey } from './namedListModel';
import { SERVICE_NAMESPACE } from './namespace';
import './config.css';

/** The ids of the confirmation's elements for a list. */
export function namedDeleteIds(kind: NamedListKind): {
  readonly title: string;
  readonly prompt: string;
  readonly confirm: string;
  readonly dismiss: string;
} {
  const prefix = NAMED_LISTS[kind].idPrefix;
  return {
    title: `${prefix}-delete-title`,
    prompt: `${prefix}-delete-prompt`,
    confirm: `${prefix}-delete-yes`,
    dismiss: `${prefix}-delete-no`,
  };
}

export interface NamedListDeleteViewProps {
  readonly kind: NamedListKind;
  /** The item name in the language of the screen. */
  readonly name: string;
  readonly pending: boolean;
  /** The text key of the refusal, when the last attempt failed. */
  readonly error: ErrorTextKey | null;
  /** True after "not found": there is nothing left to confirm. */
  readonly gone: boolean;
  readonly onConfirm: () => void;
  readonly onDismiss: () => void;
}

/** The body of the dialog. Presentational: no request, no cache. */
export function NamedListDeleteView({ kind, name, pending, error, gone, onConfirm, onDismiss }: NamedListDeleteViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const ids = namedDeleteIds(kind);
  const errorText = error === null ? null : `${t('a11y.errorPrefix')} ${t(error)}`;
  return (
    <>
      <p id={ids.prompt}>{t(listText(kind, 'deletePrompt'), { name: isolateName(name) })}</p>
      {errorText === null ? null : (
        <p className="feedback feedback--error" role="alert">
          {errorText}
        </p>
      )}
      <div className="form__actions">
        {gone ? null : (
          <button type="button" id={ids.confirm} className="button button--danger" disabled={pending} onClick={onConfirm}>
            {pending ? t(listText(kind, 'working')) : t(listText(kind, 'delete'))}
          </button>
        )}
        <button type="button" id={ids.dismiss} className="button" disabled={pending} onClick={onDismiss}>
          {gone ? t('management.common.close') : t('management.common.cancel')}
        </button>
      </div>
    </>
  );
}

export interface NamedListDeleteDialogProps {
  readonly kind: NamedListKind;
  readonly item: StaffNamedItem;
  /** The item name in the language of the screen. */
  readonly name: string;
  /** Called after the Ticket_API answered `removed` or `deactivated`. The page closes the dialog and says which. */
  readonly onDone: (result: NamedItemDeleteResult) => void;
  readonly onDismiss: () => void;
}

export function NamedListDeleteDialog({ kind, item, name, onDone, onDismiss }: NamedListDeleteDialogProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const queryClient = useQueryClient();
  const ids = namedDeleteIds(kind);
  const [error, setError] = useState<ErrorTextKey | null>(null);
  const [gone, setGone] = useState(false);

  const mutation = useMutation({
    mutationFn: () => namedListClient(kind).remove(item.id),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: namedListKey(kind) });
      onDone(result);
    },
    onError: (caught) => {
      const code = isStaffApiError(caught) ? caught.code : 'generic';
      setError(errorTextKey(code));
      if (isNamedItemGone(kind, code)) {
        setGone(true);
        void queryClient.invalidateQueries({ queryKey: namedListKey(kind) });
      }
    },
  });

  return (
    <ConfigDialog
      titleId={ids.title}
      title={t(listText(kind, 'deleteTitle'))}
      initialFocusId={ids.dismiss}
      describedById={ids.prompt}
      busy={mutation.isPending}
      onClose={onDismiss}
    >
      <NamedListDeleteView
        kind={kind}
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
