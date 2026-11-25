// The screen of a named list of the Management_Interface: the settlements and the replaceable parts (task 30.7;
// R30.1, R29.6, R23.12). Config staff only: `RequireConfigStaff` in the route tree keeps a technician from ever
// mounting it. It is the Issue categories screen (`CategoriesPage`) for another list.
//
// The list is every item, active and inactive, oldest first. Create and rename open the form in a dialog;
// delete opens a confirmation whose answer is `removed` or `deactivated`, and the page says which. Deactivate and
// reactivate act at once (both are undone by the other); there is no "last active" refusal. A refusal is shown in
// an alert above the table. Each success ends with a status message in a live region, the list is reloaded, and
// focus returns to the control that opened a dialog (or to the heading when that control no longer exists).
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { isStaffApiError } from '../../api/staff';
import { namedListClient, namedListKey } from '../../api/staffLists';
import type { NamedListKind, StaffNamedItem } from '../../api/staffListsTypes';
import { NamedListDeleteDialog } from '../namedListDelete';
import { NamedListForm } from '../namedListForm';
import {
  configLanguage,
  errorTextKey,
  isNamedItemGone,
  isolateName,
  listText,
  namedActionTexts,
  namedDeleteResultKey,
  namedItemDisplayName,
  type ErrorTextKey,
  type NamedAction,
} from '../namedListModel';
import { NamedListTable } from '../namedListTable';
import { SERVICE_NAMESPACE } from '../namespace';
import '../config.css';

type DialogState =
  | { readonly kind: 'create' }
  | { readonly kind: 'edit'; readonly item: StaffNamedItem }
  | { readonly kind: 'delete'; readonly item: StaffNamedItem };

interface RowError {
  readonly name: string;
  readonly key: ErrorTextKey;
}

export interface NamedListPageProps {
  readonly kind: NamedListKind;
}

export function NamedListPage({ kind }: NamedListPageProps) {
  const { t, i18n } = useTranslation(SERVICE_NAMESPACE);
  const language = configLanguage(i18n.language);
  const queryClient = useQueryClient();
  const client = namedListClient(kind);
  const [dialog, setDialog] = useState<DialogState | null>(null);
  // The text key of the last success, shown in the live region. Null when nothing happened yet.
  const [notice, setNotice] = useState<string | null>(null);
  // The refusal of the last deactivate or reactivate, shown as an alert above the table.
  const [rowError, setRowError] = useState<RowError | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const dialogWasOpen = useRef(false);

  const list = useQuery({
    queryKey: namedListKey(kind),
    queryFn: ({ signal }) => client.list(signal),
  });

  const toggle = useMutation({
    mutationFn: ({ item, action }: { item: StaffNamedItem; action: NamedAction }) =>
      action === 'reactivate' ? client.reactivate(item.id) : client.deactivate(item.id),
    onSuccess: async (_saved, { action }) => {
      setRowError(null);
      setNotice(namedActionTexts(kind, action).doneKey);
      await queryClient.invalidateQueries({ queryKey: namedListKey(kind) });
    },
    onError: (error, { item }) => {
      const code = isStaffApiError(error) ? error.code : 'generic';
      setNotice(null);
      setRowError({ name: namedItemDisplayName(item, language), key: errorTextKey(code) });
      if (isNamedItemGone(kind, code)) void queryClient.invalidateQueries({ queryKey: namedListKey(kind) });
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

  const onAction = (item: StaffNamedItem, action: NamedAction): void => {
    if (action === 'delete') {
      open({ kind: 'delete', item });
      return;
    }
    if (toggle.isPending) return;
    setNotice(null);
    setRowError(null);
    toggle.mutate({ item, action });
  };

  const rows = list.data ?? [];
  const loadFailed = list.isError;
  const rowErrorText =
    rowError === null
      ? null
      : `${t('a11y.errorPrefix')} ${t(listText(kind, 'rowError'), {
          name: isolateName(rowError.name),
          message: t(rowError.key),
        })}`;

  return (
    <section className="page-body">
      <div className="page-header">
        <h1 ref={headingRef} tabIndex={-1}>
          {t(listText(kind, 'title'))}
        </h1>
        <div className="page-header__actions">
          <button type="button" className="button button--primary" onClick={() => open({ kind: 'create' })}>
            {t(listText(kind, 'add'))}
          </button>
        </div>
      </div>
      <p className="subtitle">{t(listText(kind, 'subtitle'))}</p>

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
          <p>{t(listText(kind, 'loadError'))}</p>
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
        <NamedListTable
          kind={kind}
          rows={rows}
          language={language}
          busy={toggle.isPending}
          onEdit={(item) => open({ kind: 'edit', item })}
          onAction={onAction}
        />
      ) : null}

      {list.data !== undefined && rows.length === 0 ? (
        <p className="state">{t(listText(kind, 'empty'))}</p>
      ) : null}

      {dialog?.kind === 'create' || dialog?.kind === 'edit' ? (
        <NamedListForm
          key={dialog.kind === 'edit' ? dialog.item.id : 'new'}
          kind={kind}
          {...(dialog.kind === 'edit' ? { item: dialog.item } : {})}
          onSaved={(_saved, mode) => {
            setNotice(listText(kind, mode === 'created' ? 'created' : 'updated'));
            setDialog(null);
          }}
          onCancel={() => setDialog(null)}
        />
      ) : null}

      {dialog?.kind === 'delete' ? (
        <NamedListDeleteDialog
          key={dialog.item.id}
          kind={kind}
          item={dialog.item}
          name={namedItemDisplayName(dialog.item, language)}
          onDone={(result) => {
            setNotice(namedDeleteResultKey(kind, result));
            setDialog(null);
          }}
          onDismiss={() => setDialog(null)}
        />
      ) : null}
    </section>
  );
}
