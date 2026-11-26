// The table of a named list, settlements or replaceable parts (task 30.7; R30.1, R29.6, R23.12). The same as the
// category table: both names, the state as text (never colour alone) and the row actions. Every row button names
// the item in its accessible name, so a screen reader user can tell the "Edit" buttons apart. The Hebrew name is
// shown right to left and the English name left to right, whatever the language of the screen.
import { useTranslation } from 'react-i18next';
import type { NamedListKind, StaffNamedItem } from '../api/staffListsTypes';
import {
  availableNamedActions,
  listText,
  namedActionTexts,
  namedItemDisplayName,
  namedStateKey,
  type ConfigLanguage,
  type NamedAction,
} from './namedListModel';
import { SERVICE_NAMESPACE } from './namespace';
import './config.css';

export interface NamedListTableProps {
  readonly kind: NamedListKind;
  readonly rows: readonly StaffNamedItem[];
  readonly language: ConfigLanguage;
  /** True while a deactivate or reactivate request is running: the row buttons do nothing (and keep focus). */
  readonly busy: boolean;
  readonly onEdit: (item: StaffNamedItem) => void;
  readonly onAction: (item: StaffNamedItem, action: NamedAction) => void;
}

export function NamedListTable({ kind, rows, language, busy, onEdit, onAction }: NamedListTableProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const rowLabel = (actionLabel: string, name: string): string =>
    t(listText(kind, 'rowAction'), { action: actionLabel, name });
  return (
    <div className="table-wrap">
      <table className="table config-table">
        <caption className="config-sr-only">{t(listText(kind, 'title'))}</caption>
        <thead>
          <tr>
            <th scope="col">{t(listText(kind, 'nameHe'))}</th>
            <th scope="col">{t(listText(kind, 'nameEn'))}</th>
            <th scope="col">{t(listText(kind, 'state'))}</th>
            <th scope="col">{t('management.common.actions')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item) => {
            const name = namedItemDisplayName(item, language);
            return (
              <tr key={item.id}>
                <th scope="row" className="config-table__name" lang="he" dir="rtl">
                  {item.name_he}
                </th>
                <td lang="en" dir="ltr">
                  {item.name_en}
                </td>
                <td>
                  <span className={item.is_active ? 'pill pill--active' : 'pill pill--muted'}>
                    {t(namedStateKey(kind, item))}
                  </span>
                </td>
                <td>
                  <div className="config-table__actions">
                    <button
                      type="button"
                      className="button button--small"
                      aria-disabled={busy}
                      aria-label={rowLabel(t('management.common.edit'), name)}
                      onClick={() => {
                        if (!busy) onEdit(item);
                      }}
                    >
                      {t('management.common.edit')}
                    </button>
                    {/* Keyed by position, so the button that was deactivate keeps keyboard focus when it
                        becomes reactivate. */}
                    {availableNamedActions(item).map((action, index) => {
                      const label = t(namedActionTexts(kind, action).labelKey);
                      return (
                        <button
                          key={index}
                          type="button"
                          className="button button--small"
                          aria-disabled={busy}
                          aria-label={rowLabel(label, name)}
                          onClick={() => {
                            if (!busy) onAction(item, action);
                          }}
                        >
                          {label}
                        </button>
                      );
                    })}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
